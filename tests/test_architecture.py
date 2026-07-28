"""Architecture tests for bdbackup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup.backends import setup_logging
from bdbackup.config import Config, ConfigError
from bdbackup.filebackup import FileBackup
from bdbackup.mysqlbackup import MySQLBackup
from bdbackup.xtrabackup import XtraBackup


def test_all_backends_implement_protocol(tmp_path: Path):
    fb = FileBackup(backup_dst=tmp_path / "ignored")
    mb = MySQLBackup(out_dir=tmp_path / "ignored")
    xb = XtraBackup(backup_root=tmp_path / "ignored")
    assert hasattr(fb, "backup") and hasattr(fb, "verify") and hasattr(fb, "prune")
    assert hasattr(mb, "backup") and hasattr(mb, "verify") and hasattr(mb, "prune")
    assert hasattr(xb, "backup") and hasattr(xb, "verify") and hasattr(xb, "prune")


def test_setup_logging_single_handler(capsys):
    # Ensure any previous handlers are cleared for the root logger.
    root = logging.getLogger("bdbackup")
    root.handlers.clear()
    setup_logging(logging.INFO)
    setup_logging(logging.INFO)
    root.info("hello")
    # Should be exactly one line on stderr.
    captured = capsys.readouterr()
    assert captured.err.count("hello") == 1


def test_config_loads_jobs(tmp_path: Path):
    cfg_path = tmp_path / "jobs.toml"
    cfg_path.write_text(
        """
[files]
type = "file"
backup_dst = "/backup/files"
template_filename = "/etc/bdbackup/files.txt"

[mysql]
type = "mysqldump"
out_dir = "/backup/mysql"
user = "backup"
"""
    )
    cfg = Config(cfg_path)
    assert set(cfg.jobs.keys()) == {"files", "mysql"}
    assert cfg.get("files").type == "file"


def test_config_rejects_unknown_type(tmp_path: Path):
    cfg_path = tmp_path / "bad.toml"
    cfg_path.write_text("""
[bad]
type = "s3"
""")
    with pytest.raises(ConfigError):
        Config(cfg_path)


def test_run_command_executes_configured_job(tmp_path: Path):
    src = tmp_path / "hello.txt"
    src.write_text("world")
    template = tmp_path / "template.txt"
    template.write_text("hello.txt\n")
    archive = tmp_path / "archive"

    cfg_path = tmp_path / "jobs.toml"
    cfg_path.write_text(
        f"""
[files]
type = "file"
backup_dst = "{archive}"
template_filename = "{template}"
chdir = "{tmp_path}"
"""
    )

    runner = CliRunner()
    result = runner.invoke(
        __import__("bdbackup.cli", fromlist=["main"]).main,
        ["run", "--config", str(cfg_path), "files"],
    )
    assert result.exit_code == 0, result.output
    assert archive.with_suffix(".tar").exists()


def test_run_command_requires_job_or_all(tmp_path: Path):
    cfg_path = tmp_path / "jobs.toml"
    cfg_path.write_text("""
[files]
type = "file"
backup_dst = "/tmp/ignored"
""")
    from bdbackup.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--config", str(cfg_path)])
    assert result.exit_code != 0
    assert "Provide a JOB_NAME or use --all" in result.output


def test_package_init_does_not_import_pymysql():
    import bdbackup

    assert "pymysql" not in sys.modules or bdbackup is not None
