"""Configured physical backup selectors preserve each job's settings and history."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.history import History


@pytest.fixture
def xb_config(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/hello").write_text("recover me")
    (tmp_path / "paths").write_text("hello\n")
    config = tmp_path / "config.toml"
    config.write_text(
        '[history]\ndatabase="history.db"\n'
        '[mysql-prod]\ntype="xtrabackup"\nbackup_root="backups"\n'
        'user="bk_user"\npassword="bk-secret"\nbinary="mariadb-backup"\n'
        '[files]\ntype="file"\ntemplate_filename="paths"\n'
        'chdir="source"\nbackup_dst="archives/files"\nformat="tar.gz"\n'
    )
    return config


@pytest.fixture
def recording_runner(physical_runner, monkeypatch):
    commands, defaults = [], []

    def run(cmd, **kwargs):
        commands.append(list(cmd))
        defaults.append(Path(cmd[1].split("=", 1)[1]).read_text())
        return physical_runner(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", run)
    return commands, defaults


def invoke(config, *args):
    return CliRunner().invoke(main, ["--config", str(config), "run", *args])


def records(config):
    return History(Config(config).history).records()


def fulls(root):
    return sorted(p for p in root.glob("*/Full_*") if not p.is_symlink())


def test_run_incremental_chains_in_job_root(xb_config, recording_runner):
    for flags in ((), ("--incremental",), ("--incremental",)):
        result = invoke(xb_config, "mysql-prod", *flags)
        assert result.exit_code == 0, result.output
    root = (xb_config.parent / "backups").resolve()
    chains = list(root.glob("*/Incremental/Full_*"))
    increments = sorted(p for chain in chains for p in chain.iterdir() if p.is_dir())
    assert len(increments) == 2
    assert len(chains) == 1
    assert json.loads((increments[1] / "bdbackup.json").read_text())["parent"] == str(
        increments[0].relative_to(root)
    )
    commands, defaults = recording_runner
    for command, parent in zip(commands[1:], [fulls(root)[0], increments[0]], strict=True):
        assert command[0] == "mariadb-backup"
        assert f"--incremental-basedir={parent}" in command
    assert all('user="bk_user"' in text and 'password="bk-secret"' in text for text in defaults)
    assert all("bk-secret" not in arg for command in commands for arg in command)


def test_run_incremental_uses_job_encryption(xb_config, physical_runner, monkeypatch):
    key = xb_config.parent / "key"
    key.write_bytes(b"0123456789abcdef0123456789abcdef")
    xb_config.write_text(xb_config.read_text().replace(
        'binary="mariadb-backup"',
        'binary="xtrabackup"\nencrypt=true\nencrypt_key_file="key"',
    ))
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        result = physical_runner(cmd, **kwargs)
        target = Path(next(a.split("=", 1)[1] for a in cmd if a.startswith("--target-dir=")))
        if "--encrypt=AES256" in cmd:
            (target / "data.ibd").rename(target / "data.ibd.xbcrypt")
        return result

    monkeypatch.setattr("subprocess.run", run)
    for flags in ((), ("--incremental",)):
        result = invoke(xb_config, "mysql-prod", *flags)
        assert result.exit_code == 0, result.output
    assert any(a.startswith("--incremental-basedir=") for a in commands[1]), (
        "incremental-basedir missing from second command"
    )
    assert "--encrypt=AES256" in commands[1]
    assert f"--encrypt-key-file={key}" in commands[1]
    metadata, = (xb_config.parent / "backups").glob("*/Incremental/Full_*/*/bdbackup.json")
    assert json.loads(metadata.read_text())["encrypted"] is True


def test_run_full_flag_and_default_take_full(xb_config, recording_runner):
    for flags in ((), ("--full",)):
        result = invoke(xb_config, "mysql-prod", *flags)
        assert result.exit_code == 0, result.output
    root = xb_config.parent / "backups"
    assert len(fulls(root)) == 2
    assert not list(root.glob("*/Incremental"))
    assert not any(a.startswith("--incremental-basedir") for c in recording_runner[0] for a in c)


@pytest.mark.parametrize("args,message", [
    (("mysql-prod", "--full", "--incremental"), "Choose --full or --incremental, not both"),
    (("files", "--incremental"), "--incremental applies only to xtrabackup jobs: files"),
    (("--all", "--incremental"), "--incremental applies only to xtrabackup jobs: files"),
], ids=["both-flags", "file", "all"])
def test_run_selector_usage_errors(xb_config, args, message):
    with (
        patch("bdbackup.mysql.XtraBackup.full_backup") as full,
        patch("bdbackup.mysql.XtraBackup.incremental_backup") as incremental,
        patch("bdbackup.filebackup.FileBackup.backup") as file,
    ):
        result = invoke(xb_config, *args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    full.assert_not_called()
    incremental.assert_not_called()
    file.assert_not_called()
    assert records(xb_config) == []


def test_run_incremental_without_full_fails(xb_config, recording_runner, caplog):
    result = invoke(xb_config, "mysql-prod", "--incremental")
    assert result.exit_code == 1, result.output
    assert "No successful full backup found; run full backup first" in caplog.text
    assert not fulls(xb_config.parent / "backups")
    record, = records(xb_config)
    assert record.status == "failed"


def test_run_records_full_and_incremental_types(xb_config, recording_runner):
    result = invoke(xb_config, "mysql-prod")
    assert result.exit_code == 0, result.output
    record, = records(xb_config)
    assert record.job_name == "mysql-prod"
    assert record.backup_type == "xtrabackup-full"
    result = invoke(xb_config, "mysql-prod", "--incremental")
    assert result.exit_code == 0, result.output
    assert [(r.job_name, r.backup_type) for r in reversed(records(xb_config))] == [
        ("mysql-prod", "xtrabackup-full"), ("mysql-prod", "xtrabackup-incremental"),
    ]
