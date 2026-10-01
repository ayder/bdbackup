"""Configuration preflight never executes configured jobs."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.validation import _has_grant, validate_config


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[history]\ndatabase="state/history.db"\n'
        '[mysql-prod]\ntype="xtrabackup"\nbackup_root="backups"\n'
        'user="xtrabackup_user"\npassword="private-password"\n'
    )
    (tmp_path / "data").mkdir()
    return path


def validate(config_path, *args):
    return CliRunner().invoke(main, ["run", "-c", str(config_path), "--validate", *args])


@pytest.fixture
def mysql_client(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    calls = []
    credentials = []

    def run(args, **kwargs):
        calls.append(args)
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


@pytest.mark.parametrize("args", [[], ["-j", "mysql-prod"]], ids=["all", "one-job"])
def test_validate_is_read_only_and_checks_mysql(config_path, mysql_client, args):
    before = set(config_path.parent.rglob("*"))
    with patch("bdbackup.mysql.XtraBackup.full_backup") as backup:
        result = validate(config_path, *args)
    assert result.exit_code == 0, result.output
    assert "Validation passed" in result.output
    assert "Required direct MySQL grants are present" in result.output
    assert "OS user" in result.output
    assert "private-password" not in result.output
    assert "mkdir -p" in result.output
    backup.assert_not_called()
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
        result = validate(config_path)
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
        content += setting + "\n"
    config_path.write_text(content)
    result = validate(config_path)
    assert result.exit_code == 1
    assert "[FAIL]" in result.output
    assert not mysql_client[0]
    assert not (config_path.parent / "backups").exists()



def test_file_validation_checks_template_and_sources_without_archive(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[files]\ntype="file"\nbackup_dst="backup"\ntemplate_filename="paths"\nchdir="."\n'
    )
    (tmp_path / "paths").write_text("source.txt\n")
    (tmp_path / "source.txt").write_text("keep me")
    result = validate(config)
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "backup.tar").exists()
    (tmp_path / "source.txt").unlink()
    result = validate(config)
    assert result.exit_code == 1




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
        result = validate(config_path)
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
        result = validate(config_path)
    assert result.exit_code == 0, result.output
    assert "Required direct MySQL grants are present" in result.output
    assert "BACKUP_ADMIN" not in result.output


def test_encryption_validation_reports_missing_key_without_history_write(config_path, mysql_client):
    config_path.write_text(
        config_path.read_text() + 'encrypt=true\nencrypt_key_file="missing.key"\n'
    )
    result = validate(config_path)
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
        result = validate(config_path)
    assert result.exit_code == 1
    assert "Partial revokes require manual privilege review" in result.output



@pytest.mark.parametrize("creatable", [True, False], ids=["creatable", "blocked"])
def test_validate_reports_job_restore_root(tmp_path, creatable):
    (tmp_path / "source.txt").write_text("source")
    (tmp_path / "paths").write_text("source.txt\n")
    root = tmp_path / "new/rr"
    if not creatable:
        (tmp_path / "blocker").write_text("not a directory")
        root = tmp_path / "blocker/rr"
    config = tmp_path / "config.toml"
    config.write_text(
        '[history]\ndatabase="state/history.db"\n'
        '[files]\ntype="file"\ntemplate_filename="paths"\n'
        'chdir="."\nbackup_dst="backup.tar"\n'
        f'restore_root="{root}"\n'
    )
    result = validate(config, "-j", "files")
    section = result.output.split("Job 'files' (file)", 1)[1]
    status = "OK" if creatable else "FAIL"
    assert f"[{status}] Recovery root: {root.resolve()}" in section
    assert result.exit_code == (0 if creatable else 1), result.output
    assert not root.exists()
