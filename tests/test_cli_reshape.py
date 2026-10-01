"""Spec 10: one command, everything else an option; dry run, active, removed forms."""

import logging
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.filebackup import FileBackup
from bdbackup.gfs import runner
from bdbackup.mysql import XtraBackup
from tests.conftest import write_checkpoints
from tests.test_gfs_run import Env


def invoke(*args):
    return CliRunner().invoke(main, list(map(str, args)))


def flat(output):
    return " ".join(output.split())


@pytest.fixture
def file_config(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/hello").write_text("recover me")
    (tmp_path / "paths").write_text("hello\n")
    path = tmp_path / "config.toml"
    path.write_text(
        '[history]\ndatabase="state/history.sqlite3"\nrestore_root="recovery"\n'
        '[daily]\ntype="file"\ntemplate_filename="paths"\n'
        'chdir="source"\nbackup_dst="archives/daily"\nformat="tar.gz"\n'
    )
    return path


def archive_of(config):
    return config.parent / "archives/daily.tar.gz"


def history_file(config):
    return config.parent / "state/history.sqlite3"


def snapshot(root):
    if not root.exists():
        return []
    return [
        (p.relative_to(root).as_posix(),
         "link" if p.is_symlink() else "dir" if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    ]


def test_top_level_lists_three_commands():
    result = invoke("--help")
    assert result.exit_code == 0, result.output
    commands = result.output.split("Commands:", 1)[1]
    assert set(re.findall(r"^  (\S+)", commands, re.MULTILINE)) == {"history", "restore", "run"}


@pytest.mark.parametrize("option", ["config", "validate", "cron"])
def test_top_level_options_removed(file_config, option):
    args = {
        "config": ["--config", file_config, "run", "-c", file_config, "--validate"],
        "validate": ["--validate", "--config", file_config],
        "cron": ["--cron", "--config", file_config],
    }[option]
    with patch("shutil.which", return_value=None):
        result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert f"No such option '--{option}'" in result.output


@pytest.mark.parametrize("flag", ["-j", "--job"], ids=["short", "long"])
def test_run_job_option_runs_job(file_config, flag):
    result = invoke("run", "-c", file_config, flag, "daily")
    assert result.exit_code == 0, result.output
    assert "Job daily:" in result.output
    assert archive_of(file_config).is_file()


@pytest.mark.parametrize("args,message", [
    (["daily"], "Got unexpected extra argument (daily)"),
    (["--all"], "No such option '--all'"),
    (["--cron"], "No such option '--cron'"),
    ([], "Provide --job NAME or --validate"),
], ids=["positional", "all", "cron", "neither"])
def test_run_old_forms_exit_2(file_config, args, message):
    with patch("shutil.which", return_value=None):
        result = invoke("run", "-c", file_config, *args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert not archive_of(file_config).exists()


def test_validate_skips_inactive_jobs(file_config):
    file_config.write_text(file_config.read_text() + (
        '[paused]\ntype="file"\nactive=false\ntemplate_filename="missing"\n'
        'backup_dst="archives/paused"\n'
    ))
    result = invoke("run", "-c", file_config, "--validate")
    assert result.exit_code == 0, result.output
    assert "Job 'daily' (file)" in result.output
    assert "Job 'paused' (file): inactive (active = false), not checked" in result.output


@pytest.mark.parametrize("flag", ["--full", "--incremental", "--verify", "--no-verify",
                                  "--dry-run"],
                         ids=["full", "incremental", "verify", "no-verify", "dry-run"])
def test_validate_rejects_run_options(file_config, flag):
    result = invoke("run", "-c", file_config, "--validate", flag)
    assert result.exit_code == 2, result.output
    assert f"--validate cannot be combined with {flag}" in result.output


@pytest.mark.parametrize("flag", ["--verify", "--no-verify"], ids=["verify", "no-verify"])
def test_dry_run_rejects_verify(file_config, flag):
    result = invoke("run", "-c", file_config, "-j", "daily", "--dry-run", flag)
    assert result.exit_code == 2, result.output
    assert "--dry-run cannot be combined with --verify or --no-verify" in result.output
    assert not archive_of(file_config).exists()


@pytest.mark.parametrize("form", ["subcommand", "with-successful"])
def test_history_checksum_forms(file_config, form):
    if form == "subcommand":
        result = invoke("history", "checksum", "--config", file_config)
        message = "Got unexpected extra argument (checksum)"
    else:
        result = invoke("history", "-c", file_config, "--create-checksum", "--successful")
        message = "--create-checksum cannot be combined with --successful"
    assert result.exit_code == 2, result.output
    assert message in result.output


def make_archive(tmp_path):
    (tmp_path / "source").mkdir(exist_ok=True)
    (tmp_path / "source/hello").write_text("recover me")
    (tmp_path / "paths").write_text("hello\n")
    return FileBackup(tmp_path / "made", tmp_path / "paths", chdir=tmp_path / "source",
                      format="tar.gz").backup().path


def test_restore_archive_mode(tmp_path):
    archive = make_archive(tmp_path)
    destination = tmp_path / "out"
    result = invoke("restore", "--archive", archive, "-d", destination)
    assert result.exit_code == 0, result.output
    assert (destination / "hello").read_text() == "recover me"
    assert f"Restored to: {destination}" in result.output


def test_restore_backup_mode_prepares(tmp_path, physical_runner):
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        if "--prepare" not in cmd:
            return physical_runner(cmd, **kwargs)
        target = Path(next(a.split("=", 1)[1] for a in cmd if a.startswith("--target-dir=")))
        end = XtraBackup._checkpoints(target)["to_lsn"]
        write_checkpoints(target, end=end, kind="full-prepared")
        return subprocess.CompletedProcess(cmd, 0)

    xb = XtraBackup(tmp_path / "physical", binary="mariadb-backup", retention_days=0)
    destination = tmp_path / "recovered"
    with patch("subprocess.run", side_effect=run):
        full = xb.full_backup()
        before = snapshot(xb.backup_root)
        commands.clear()
        result = invoke("restore", "--backup", full.path, "--root", xb.backup_root,
                        "-d", destination, "--binary", "mariadb-backup")
    assert result.exit_code == 0, result.output
    assert f"Prepared recovery directory: {destination}" in result.output
    assert (destination / "data.ibd").read_bytes() == b"database pages"
    assert XtraBackup._checkpoints(destination)["backup_type"] == "full-prepared"
    assert snapshot(xb.backup_root) == before
    assert any("--prepare" in cmd for cmd in commands)
    assert all(cmd[0] == "mariadb-backup" for cmd in commands)


RESTORE_RULES = {
    "archive-and-backup": (["--archive", "A", "--backup", "B", "--root", "B", "-d", "D"],
                           "--archive and --backup cannot be combined"),
    "backup-without-root": (["--backup", "B", "-d", "D"], "--backup requires --root"),
    "root-without-backup": (["--root", "B", "-d", "D"], "--root requires --backup"),
    "config-with-archive": (["-c", "C", "--archive", "A", "-d", "D"],
                            "--config applies only to history restores"),
    "config-with-backup": (["-c", "C", "--backup", "B", "--root", "B", "-d", "D"],
                           "--config applies only to history restores"),
    "history-option-with-archive": (["--archive", "A", "--backup-id", "1", "-d", "D"],
                                    "--backup-id, --job and --yes apply only to history "
                                    "restores"),
    "binary-with-archive": (["--archive", "A", "--binary", "xtrabackup", "-d", "D"],
                            "--binary applies only to --backup"),
    "key-with-archive": (["--archive", "A", "--encrypt-key-file", "K", "-d", "D"],
                         "--encrypt-key-file does not apply to --archive"),
    "archive-without-dst": (["--archive", "A"], "Provide --dst for --archive and --backup"),
    "positional-archive": (["A", "-d", "D"], "Got unexpected extra argument"),
    "chdir": (["--chdir", "B", "--archive", "A", "-d", "D"], "No such option '--chdir'"),
}


@pytest.mark.parametrize("case", list(RESTORE_RULES))
def test_restore_mode_rules(tmp_path, file_config, case):
    archive = make_archive(tmp_path)
    (tmp_path / "physical").mkdir()
    (tmp_path / "key").write_bytes(b"k" * 32)
    paths = {"A": archive, "B": tmp_path / "physical", "C": file_config,
             "D": tmp_path / "out", "K": tmp_path / "key"}
    args, message = RESTORE_RULES[case]
    result = invoke("restore", *[paths.get(arg, arg) for arg in args])
    assert result.exit_code == 2, result.output
    assert message in flat(result.output)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("command", ["file", "mysqldump", "xtrabackup", "retention", "init"])
def test_removed_commands_unknown(command):
    result = invoke(command, "--help")
    assert result.exit_code == 2, result.output
    assert f"No such command '{command}'" in result.output


REMOVED_CONFIG = {
    "retention-type": ('[old]\ntype="retention"\nfull_dir="flat"\n',
                       "Job 'old' has unsupported type 'retention'"),
    "schedule": ('[daily]\nschedule="0 2 * * *"\n',
                 "Job 'daily': key 'schedule' was removed in 0.7.0"),
    "apply": ('[gfs-main]\ntype="gfs"\napply=false\n'
              '[[gfs-main.stage]]\npaths=["BACKUP"]\nperiod="daily"\nkeep="5d"\n'
              '[[gfs-main.stage]]\npaths=["NFS/daily"]\nperiod="daily"\nkeep="20d"\n',
              "Job 'gfs-main': key 'apply' was removed in 0.7.0"),
    "jobs": ('[sql]\ntype="mysqldump"\ndatabase="app"\njobs=2\n',
             "Job 'sql': key 'jobs' was removed in 0.7.0"),
    "active-not-bool": ('[daily]\nactive="yes"\n', "Job 'daily': active must be true or false"),
}


@pytest.mark.parametrize("case", list(REMOVED_CONFIG))
def test_removed_config_fails_to_load(file_config, case):
    for name in ("flat", "BACKUP", "NFS/daily"):
        (file_config.parent / name).mkdir(parents=True, exist_ok=True)
    text, message = REMOVED_CONFIG[case]
    content = file_config.read_text()
    if text.startswith("[daily]\n"):
        content = content.replace("[daily]\n", text)
    else:
        content += text
    file_config.write_text(content)
    with patch("shutil.which", return_value=None):
        result = invoke("run", "-c", file_config, "--validate")
    assert result.exit_code == 2, result.output
    assert message in flat(result.output)


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--validate"]],
                         ids=["run", "dry-run", "validate"])
def test_inactive_job_refused(file_config, flags):
    file_config.write_text(file_config.read_text().replace("[daily]\n", "[daily]\nactive=false\n"))
    result = invoke("run", "-c", file_config, "-j", "daily", *flags)
    assert result.exit_code == 2, result.output
    assert "Job 'daily' is inactive (active = false)" in result.output
    assert not archive_of(file_config).exists()
    assert not history_file(file_config).exists()


def test_dry_run_file_job_writes_nothing(file_config, caplog):
    caplog.set_level(logging.INFO)
    result = invoke("run", "-c", file_config, "-j", "daily", "--dry-run")
    assert result.exit_code == 0, result.output
    archive = archive_of(file_config)
    assert f"Dry run; would create: {archive}" in result.output
    assert any("Would back up:" in line and "hello" in line
               for line in caplog.text.splitlines())
    assert not archive.exists()
    assert not history_file(file_config).exists()


def test_dry_run_mysqldump_runs_nothing(file_config):
    file_config.write_text(file_config.read_text() +
                           '[sql]\ntype="mysqldump"\ndatabase="app"\nout_dir="dumps"\n')
    dumps = (file_config.parent / "dumps").resolve()
    with patch("subprocess.run", side_effect=AssertionError("a tool ran")) as run:
        result = invoke("run", "-c", file_config, "-j", "sql", "--dry-run")
    assert result.exit_code == 0, result.output
    assert f"Dry run; would dump app into {dumps}" in result.output
    run.assert_not_called()
    assert not dumps.exists()
    assert not history_file(file_config).exists()


@pytest.mark.parametrize("case", ["full", "incremental", "incremental-without-full"])
def test_dry_run_xtrabackup(file_config, physical_runner, caplog, case):
    file_config.write_text(file_config.read_text() + (
        '[xb]\ntype="xtrabackup"\nbackup_root="physical"\nbinary="mariadb-backup"\n'
        "retention_days=0\n"
    ))
    xb = XtraBackup(file_config.parent / "physical", binary="mariadb-backup", retention_days=0)
    full = None
    if case != "incremental-without-full":
        with patch("subprocess.run", side_effect=physical_runner):
            full = xb.full_backup()
    before = snapshot(xb.backup_root)
    flags = ["--incremental"] if case.startswith("incremental") else []
    with patch("subprocess.run", side_effect=AssertionError("a tool ran")):
        result = invoke("run", "-c", file_config, "-j", "xb", "--dry-run", *flags)
    if case == "full":
        assert result.exit_code == 0, result.output
        assert f"Dry run; would take a full backup under {xb.backup_root}" in result.output
    elif case == "incremental":
        assert result.exit_code == 0, result.output
        assert (f"Dry run; would take an incremental of {full.path} under {xb.backup_root}"
                in result.output)
    else:
        assert result.exit_code == 1, result.output
        assert "No successful full backup found" in caplog.text
    assert snapshot(xb.backup_root) == before
    assert not history_file(file_config).exists()


def test_gfs_applies_without_dry_run(tmp_path):
    env = Env(tmp_path)
    old = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    config = Config(env.config_path)
    lines = []
    code = runner.run_job(config, config.get("gfs-main"), out=lines.append)
    assert code == 0, "\n".join(lines)
    assert (env.root / "NFS/daily/files/a.tar.gz").is_file(), "\n".join(lines)
    assert not old.exists()
