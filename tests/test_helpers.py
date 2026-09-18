"""Configuration preflight and cron suggestions never execute configured jobs."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config, ConfigError, build_backend
from bdbackup.scheduling import _command, _scheduled_jobs, recommend_cron
from bdbackup.validation import _has_grant, validate_config


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[history]\ndatabase="state/history.db"\n'
        '[mysql-prod]\ntype="xtrabackup"\nbackup_root="backups"\n'
        'user="xtrabackup_user"\npassword="private-password"\n'
        'schedule="30 1 * * *"\n'
        '[cleanup]\ntype="retention"\nfull_dir="flat/full"\napply=false\n'
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "flat/full").mkdir(parents=True)
    return path


@pytest.fixture
def mysql_client(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    calls = []
    credentials = []

    def run(args, **kwargs):
        calls.append(args)
        if args[-1] == "-l":
            return subprocess.CompletedProcess(args, 1, "", "no crontab for testuser")
        assert args[1].startswith("--defaults-extra-file=")
        defaults = Path(args[1].split("=", 1)[1])
        credentials.append(defaults)
        assert defaults.stat().st_mode & 0o777 == 0o600
        assert "private-password" in defaults.read_text()
        assert all("private-password" not in arg for arg in args)
        assert args[-1] == "--execute=SELECT CURRENT_USER(), VERSION(), @@datadir; SHOW GRANTS;"
        assert kwargs["timeout"] == 15
        return subprocess.CompletedProcess(
            args,
            0,
            f"xtrabackup_user@localhost\t8.4.0\t{tmp_path / 'data'}\n"
            "GRANT RELOAD, BACKUP_ADMIN, REPLICATION CLIENT, PROCESS, LOCK TABLES "
            "ON *.* TO 'xtrabackup_user'@'localhost'\n"
            "GRANT SELECT ON `performance_schema`.* TO 'xtrabackup_user'@'localhost'\n",
            "",
        )

    monkeypatch.setattr("subprocess.run", run)
    return calls, credentials


@pytest.mark.parametrize(
    "args",
    [
        ["--validate"],
        ["run", "--validate"],
        ["run", "mysql-prod", "--validate"],
        ["run", "--all", "--validate"],
    ],
)
def test_validate_is_read_only_and_checks_mysql(config_path, mysql_client, args):
    before = set(config_path.parent.rglob("*"))
    with (
        patch("bdbackup.mysql.XtraBackup.full_backup") as backup,
        patch("bdbackup.retention.run_job", autospec=True) as prune,
    ):
        result = CliRunner().invoke(main, ["--config", str(config_path), *args])
    assert result.exit_code == 0, result.output
    assert "Validation passed" in result.output
    assert "Required direct MySQL grants are present" in result.output
    assert "OS user" in result.output
    assert "private-password" not in result.output
    assert "mkdir -p" in result.output
    backup.assert_not_called()
    prune.assert_not_called()
    assert set(config_path.parent.rglob("*")) == before
    calls, credentials = mysql_client
    assert len(calls) == 1
    assert all(not path.exists() for path in credentials)


def test_missing_grants_prints_sql_for_actual_mysql_account(config_path, mysql_client):
    response = subprocess.CompletedProcess(
        [],
        0,
        f"matched_user@%\t8.4.0\t{config_path.parent / 'data'}\n"
        "GRANT USAGE ON *.* TO 'matched_user'@'%'\n",
        "",
    )
    with patch("subprocess.run", return_value=response):
        result = CliRunner().invoke(main, ["run", "--config", str(config_path), "--validate"])
    assert result.exit_code == 1
    assert (
        "GRANT RELOAD, BACKUP_ADMIN, REPLICATION CLIENT, PROCESS, LOCK TABLES ON *.*"
        in result.output
    )
    assert "TO 'matched_user'@'%'" in result.output
    assert "GRANT CREATE TABLESPACE" in result.output
    assert "Optional, for importing individual tables" in result.output
    assert "GRANT SELECT ON `performance_schema`.`log_status`" in result.output
    assert "private-password" not in result.output


@pytest.mark.parametrize("failure", ["auth", "timeout", "missing_client"])
def test_unverified_mysql_login_fails_and_prints_grants(config_path, mysql_client, failure):
    config = Config(config_path)
    with patch("subprocess.run") as run:
        if failure == "auth":
            run.return_value = subprocess.CompletedProcess([], 1, "", "private-password denied")
        elif failure == "timeout":
            run.side_effect = subprocess.TimeoutExpired("mysql", 15)
        else:
            with patch("shutil.which", return_value=None):
                lines, ok = validate_config(config, [config.get("mysql-prod")])
        if failure != "missing_client":
            lines, ok = validate_config(config, [config.get("mysql-prod")])
    assert not ok
    assert "GRANT RELOAD" in "\n".join(lines)
    assert "private-password" not in "\n".join(lines)


def test_database_all_privileges_does_not_imply_dynamic_global_grants():
    grants = ["GRANT ALL PRIVILEGES ON *.* TO 'u'@'localhost'"]
    assert _has_grant(grants, "RELOAD")
    assert _has_grant(grants, "SELECT", "performance_schema", "log_status")
    assert not _has_grant(grants, "BACKUP_ADMIN")
    assert not _has_grant(["GRANT RELOAD ON `db`.* TO 'u'@'localhost'"], "RELOAD")


def test_directory_permissions_identify_os_user_and_destination(config_path, mysql_client):
    config = Config(config_path)
    with patch("bdbackup.validation._access", return_value=False):
        lines, ok = validate_config(config, [config.get("mysql-prod")])
    output = "\n".join(lines)
    assert not ok
    assert "Required directory: mkdir -p" in output
    assert str(config_path.parent / "backups") in output
    assert "needs read/write/search permissions" in output
    assert "MySQL datadir" in output


@pytest.mark.parametrize(
    "setting",
    [
        'parallel="bad"',
        "parallel=true",
        "compress_threads=0",
        "encrypt=true",
        'user=""',
        'unknown="value"',
    ],
)
def test_invalid_settings_do_not_run_mysql_or_backup(config_path, mysql_client, setting):
    content = config_path.read_text()
    if setting.startswith("user="):
        content = content.replace('user="xtrabackup_user"', setting)
    else:
        content = content.replace("[cleanup]", setting + "\n[cleanup]")
    config_path.write_text(content)
    result = CliRunner().invoke(main, ["--config", str(config_path), "--validate"])
    assert result.exit_code == 1
    assert "[FAIL]" in result.output
    assert not mysql_client[0]
    assert not (config_path.parent / "backups").exists()


def test_retention_apply_validation_does_not_delete(config_path):
    config_path.write_text(config_path.read_text().replace("apply=false", "apply=true"))
    backup = config_path.parent / "flat/full/old-full.sql.gz"
    backup.write_bytes(b"must survive")
    with patch("bdbackup.retention.run_job", autospec=True) as run:
        result = CliRunner().invoke(
            main, ["run", "--config", str(config_path), "cleanup", "--validate"]
        )
    assert result.exit_code == 0, result.output
    assert "Retention mode: APPLY" in result.output
    assert backup.read_bytes() == b"must survive"
    run.assert_not_called()


def test_file_validation_checks_template_and_sources_without_archive(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[files]\ntype="file"\nbackup_dst="backup"\ntemplate_filename="paths"\nchdir="."\n'
    )
    (tmp_path / "paths").write_text("source.txt\n")
    (tmp_path / "source.txt").write_text("keep me")
    result = CliRunner().invoke(main, ["--config", str(config), "--validate"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "backup.tar").exists()
    (tmp_path / "source.txt").unlink()
    result = CliRunner().invoke(main, ["--config", str(config), "--validate"])
    assert result.exit_code == 1


def test_schedule_is_metadata_not_a_backend_parameter(config_path):
    config = Config(config_path)
    job = config.get("mysql-prod")
    assert job.schedule == "30 1 * * *"
    assert "schedule" not in job.params
    assert build_backend(job).user == "xtrabackup_user"


@pytest.mark.parametrize(
    "schedule",
    [
        "60 2 * * *",
        "0 24 * * *",
        "* * * *",
        "0 0 0 * *",
        "*/0 * * * *",
        "0 0 * * 7-1",
        "0 0 * * *;whoami",
        12,
    ],
)
def test_invalid_schedules_rejected(config_path, schedule):
    text = f'"{schedule}"' if isinstance(schedule, str) else str(schedule)
    config_path.write_text(config_path.read_text().replace('"30 1 * * *"', text))
    with pytest.raises(ConfigError, match="cron|schedule|Cron"):
        Config(config_path)


def test_cron_defaults_overrides_and_no_mutations(config_path, mysql_client):
    before = set(config_path.parent.rglob("*"))
    with patch("bdbackup.mysql.XtraBackup.full_backup") as backup:
        result = CliRunner().invoke(main, ["--config", str(config_path), "--cron"])
    assert result.exit_code == 0, result.output
    assert "30 1 * * * cd " in result.output
    assert "0 4 * * * cd " in result.output
    assert "dry-run retention" in result.output
    assert "automatic XtraBackup pruning" in result.output
    assert "private-password" not in result.output
    assert mysql_client[0] == [["/usr/bin/crontab", "-l"]]
    backup.assert_not_called()
    assert set(config_path.parent.rglob("*")) == before


def test_cron_backup_default_and_job_selection_are_stable(config_path, mysql_client):
    config_path.write_text(
        config_path.read_text().replace('schedule="30 1 * * *"\n', "")
        + '[second]\ntype="xtrabackup"\nbackup_root="second"\n'
    )
    config = Config(config_path)
    lines, ok = recommend_cron(config, [config.get("second")])
    assert ok
    assert any(line.startswith("15 2 * * * ") for line in lines)


@pytest.mark.parametrize("style", ["generated", "global_config", "all", "different_schedule"])
def test_existing_cron_jobs_are_not_duplicated(config_path, mysql_client, style):
    config = Config(config_path)
    job = config.get("mysql-prod")
    if style == "generated":
        command = _command(config, job)
    elif style == "global_config":
        command = f"/usr/bin/bdbackup --config {config.path} run mysql-prod"
    elif style == "all":
        command = f"bdbackup run --config {config.path} --all"
    else:
        command = f"bdbackup run --config={config.path} mysql-prod"
    with patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, f"5 9 * * * {command}\n", ""),
    ):
        lines, ok = recommend_cron(config, [job])
    assert ok
    assert any("already scheduled at 5 9 * * *" in line for line in lines)
    assert not any(line.startswith("30 1 * * * ") for line in lines)


def test_cron_matching_ignores_comments_other_configs_and_echo(config_path, mysql_client):
    config = Config(config_path)
    command = _command(config, config.get("mysql-prod"))
    crontab = (
        f"# 0 2 * * * {command}\n"
        f"0 2 * * * echo {command}\n"
        "0 2 * * * bdbackup run --config /different.toml mysql-prod\n"
        f"0 2 * * * bdbackup run --config {config.path} mysql-prod --validate\n"
    )
    assert not _scheduled_jobs(crontab, config)


@pytest.mark.parametrize("failure", ["denied", "timeout", "missing"])
def test_cron_read_failures_are_not_reported_as_empty(config_path, mysql_client, failure):
    config = Config(config_path)
    with patch("subprocess.run") as run:
        if failure == "denied":
            run.return_value = subprocess.CompletedProcess([], 1, "", "access denied")
        elif failure == "timeout":
            run.side_effect = subprocess.TimeoutExpired("crontab", 10)
        else:
            with patch("shutil.which", return_value=None):
                lines, ok = recommend_cron(config, list(config.jobs.values()))
        if failure != "missing":
            lines, ok = recommend_cron(config, list(config.jobs.values()))
    assert not ok
    assert "[FAIL]" in "\n".join(lines)
    assert any(line.startswith("30 1 * * * ") for line in lines)


def test_cron_quotes_shell_metacharacters_and_percent(tmp_path, mysql_client):
    config_file = tmp_path / "space % $(touch sentinel).toml"
    config_file.write_text('["job % ; $(touch sentinel)"]\ntype="xtrabackup"\nbackup_root="backup"')
    config = Config(config_file)
    (job,) = config.jobs.values()
    command = _command(config, job)
    assert r"\%" in command
    assert "'job" in command
    assert _scheduled_jobs("0 2 * * * " + command, config) == {job.name: ["0 2 * * *"]}
    assert not (tmp_path / "sentinel").exists()


def test_combined_helpers_and_usage_guards(config_path, mysql_client):
    result = CliRunner().invoke(main, ["run", "--config", str(config_path), "--validate", "--cron"])
    assert result.exit_code == 0, result.output
    assert "Validation passed" in result.output and "Suggested user crontab" in result.output
    assert CliRunner().invoke(main, ["--validate"]).exit_code == 2
    assert (
        CliRunner().invoke(main, ["--config", str(config_path), "--validate", "run"]).exit_code == 2
    )


def test_mariadb_grants_use_binlog_monitor(config_path, mysql_client):
    config_path.write_text(
        config_path.read_text().replace(
            'type="xtrabackup"', 'type="xtrabackup"\nbinary="mariadb-backup"'
        )
    )
    response = subprocess.CompletedProcess(
        [],
        0,
        f"xtrabackup_user@localhost\t11.4.0-MariaDB\t{config_path.parent / 'data'}\n"
        "GRANT USAGE ON *.* TO 'xtrabackup_user'@'localhost'\n",
        "",
    )
    with patch("subprocess.run", return_value=response):
        result = CliRunner().invoke(main, ["--config", str(config_path), "--validate"])
    assert result.exit_code == 1
    assert "GRANT RELOAD, BINLOG MONITOR, PROCESS, LOCK TABLES" in result.output
    assert "BACKUP_ADMIN" not in result.output


def test_mysqldump_scoped_grants(config_path, mysql_client):
    config_path.write_text(
        config_path.read_text().replace(
            'type="xtrabackup"\nbackup_root="backups"',
            'type="mysqldump"\nout_dir="backups"\ndatabase="app"\noptions=["--no-tablespaces"]',
        )
    )
    response = subprocess.CompletedProcess(
        [],
        0,
        f"xtrabackup_user@localhost\t8.4.0\t{config_path.parent / 'data'}\n"
        "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `app`.* "
        "TO 'xtrabackup_user'@'localhost'\n",
        "",
    )
    with patch("subprocess.run", return_value=response):
        result = CliRunner().invoke(main, ["--config", str(config_path), "--validate"])
    assert result.exit_code == 0, result.output
    assert "Required direct MySQL grants are present" in result.output
    assert "BACKUP_ADMIN" not in result.output


def test_encryption_validation_reports_missing_key_without_history_write(config_path, mysql_client):
    config_path.write_text(
        config_path.read_text().replace(
            "[cleanup]",
            'encrypt=true\nencrypt_key_file="missing.key"\n[cleanup]',
        )
    )
    result = CliRunner().invoke(main, ["--config", str(config_path), "--validate"])
    assert result.exit_code == 1
    assert "Cannot read encryption key file" in result.output
    assert not (config_path.parent / "state").exists()


def test_partial_revokes_never_claim_effective_access(config_path, mysql_client):
    response = subprocess.CompletedProcess(
        [],
        0,
        f"xtrabackup_user@localhost\t8.4.0\t{config_path.parent / 'data'}\n"  # noqa: S608
        "GRANT ALL PRIVILEGES ON *.* TO 'xtrabackup_user'@'localhost'\n"
        "GRANT BACKUP_ADMIN ON *.* TO 'xtrabackup_user'@'localhost'\n"
        "REVOKE SELECT ON `performance_schema`.* FROM 'xtrabackup_user'@'localhost'\n",
        "",
    )
    with patch("subprocess.run", return_value=response):
        result = CliRunner().invoke(main, ["--config", str(config_path), "--validate"])
    assert result.exit_code == 1
    assert "Partial revokes require manual privilege review" in result.output
