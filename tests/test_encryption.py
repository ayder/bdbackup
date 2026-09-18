"""Encrypted physical backups, history migration, and recovery ordering."""

import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupError, BackupResult
from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.history import History, HistorySettings
from bdbackup.mysql import XtraBackup
from bdbackup.recovery import restore_record
from tests.conftest import write_checkpoints


@pytest.fixture
def key_file(tmp_path):
    key = tmp_path / "xtrabackup.key"
    key.write_bytes(b"0123456789abcdef0123456789abcdef")
    key.chmod(0o600)
    return key


@pytest.fixture
def encrypted_runner(physical_runner):
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        target = Path(next(a.split("=", 1)[1] for a in cmd if a.startswith("--target-dir=")))
        if "--backup" in cmd:
            physical_runner(cmd, **kwargs)
            if "--encrypt=AES256" in cmd:
                data = target / "data.ibd"
                extension = ".zst.xbcrypt" if "--compress=zstd" in cmd else ".xbcrypt"
                data.rename(data.with_name(data.name + extension))
        elif "--decrypt=AES256" in cmd:
            for encrypted in target.rglob("*.xbcrypt"):
                encrypted.with_suffix("").write_bytes(encrypted.read_bytes())
                if "--remove-original" in cmd:
                    encrypted.unlink()
        elif "--decompress" in cmd:
            assert not list(target.rglob("*.xbcrypt"))
            for compressed in target.rglob("*.zst"):
                compressed.with_suffix("").write_bytes(compressed.read_bytes())
        elif "--prepare" in cmd:
            assert (target / "data.ibd").exists()
            incremental = next(
                (Path(a.split("=", 1)[1]) for a in cmd if a.startswith("--incremental-dir=")),
                None,
            )
            end = XtraBackup._checkpoints(incremental or target)["to_lsn"]
            write_checkpoints(target, end=end, kind="full-prepared")
        return subprocess.CompletedProcess(cmd, 0)

    return run, commands


def configured_job(tmp_path, encryption):
    config = tmp_path / "config.toml"
    config.write_text(
        '[history]\ndatabase="history.db"\n'
        '[mysql-prod]\ntype="xtrabackup"\nbackup_root="backups"\n'
        'compress="zstd"\n' + encryption
    )
    return config


def test_encrypted_config_history_and_relocated_key_restore(tmp_path, key_file, encrypted_runner):
    config = configured_job(tmp_path, 'encrypt=true\nencrypt_key_file="xtrabackup.key"\n')
    assert Config(config).get("mysql-prod").params["encrypt_key_file"] == key_file
    runner, commands = encrypted_runner
    with patch("subprocess.run", side_effect=runner):
        result = CliRunner().invoke(main, ["run", "--config", str(config), "mysql-prod"])
    assert result.exit_code == 0, result.output
    history = History(Config(config).history)
    record, = history.records()
    assert record.encrypted and record.status == "success"
    assert json.loads(record.restore_info)["encrypt_key_file"] == str(key_file)
    assert json.loads(record.restore_info)["encryption"] == "AES256"
    assert "--encrypt=AES256" in commands[0]
    assert f"--encrypt-key-file={key_file}" in commands[0]
    with closing(sqlite3.connect(history.settings.database)) as db:
        assert db.execute("SELECT encrypted FROM backup_runs").fetchone() == (1,)
    assert key_file.read_bytes() not in history.settings.database.read_bytes()
    assert key_file.read_text() not in result.output
    listing = CliRunner().invoke(main, ["history", "--config", str(config)])
    assert "encrypted" in listing.output
    moved = key_file.with_name("moved.key")
    key_file.rename(moved)
    destination = tmp_path / "recovery"
    with patch("subprocess.run", side_effect=runner):
        restored = CliRunner().invoke(main, [
            "restore", "--config", str(config), "--backup-id", str(record.id), "--yes",
            "--dst", str(destination), "--encrypt-key-file", str(moved),
        ])
    assert restored.exit_code == 0, restored.output
    assert (destination / "data.ibd").read_bytes() == b"database pages"
    assert (Path(record.path) / "data.ibd.zst.xbcrypt").exists()
    assert not (Path(record.path) / "data.ibd").exists()
    assert "--decrypt=AES256" in commands[1]
    assert "--decompress" in commands[2]
    assert "--prepare" in commands[3]
    assert all(moved.read_text() not in " ".join(cmd) for cmd in commands)


def test_encrypted_full_incremental_cli_and_history_restore(tmp_path, key_file, encrypted_runner):
    config = configured_job(tmp_path, "")
    runner, commands = encrypted_runner
    with patch("subprocess.run", side_effect=runner):
        for kind in ("full", "incremental", "incremental"):
            result = CliRunner().invoke(main, [
                "--config", str(config), "xtrabackup", kind, "--database", "prod",
                "--root", str(tmp_path / "backups"), "--encrypt",
                "--encrypt-key-file", str(key_file), "--compress", "zstd",
            ])
            assert result.exit_code == 0, result.output
        records = History(Config(config).history).records()
        assert len(records) == 3 and all(r.encrypted for r in records)
        destination = restore_record(records[0], tmp_path / "recovery")
    assert XtraBackup._checkpoints(destination)["to_lsn"] == 300
    operations = [next(a for a in cmd if a in {
        "--backup", "--decrypt=AES256", "--decompress", "--prepare",
    }) for cmd in commands]
    assert operations == (
        ["--backup"] * 3 + ["--decrypt=AES256", "--decompress"] * 3 + ["--prepare"] * 3
    )
    assert all(r.available for r in records)


@pytest.mark.parametrize("value", ["yes", "false", 1, None])
def test_encrypt_requires_toml_boolean(tmp_path, value):
    with pytest.raises(ValueError, match="boolean"):
        XtraBackup(tmp_path, encrypt=value)


@pytest.mark.parametrize("key_kind", [
    "missing_setting", "missing_file", "directory", "short", "trailing_newline",
])
def test_encrypted_backup_requires_valid_key_before_execution(tmp_path, key_kind):
    key = tmp_path / "key"
    if key_kind == "directory":
        key.mkdir()
    elif key_kind == "short":
        key.write_bytes(b"too short")
    elif key_kind == "trailing_newline":
        key.write_bytes(b"a" * 32 + b"\n")
    with patch("subprocess.run") as run, pytest.raises((ValueError, BackupError)):
        XtraBackup(tmp_path / "backups", encrypt=True,
                   encrypt_key_file=None if key_kind == "missing_setting" else key)
    run.assert_not_called()
    assert not (tmp_path / "backups").exists()


def test_missing_key_failure_is_recorded_as_encrypted_attempt(tmp_path):
    config = configured_job(tmp_path, "encrypt=true\n")
    with patch("subprocess.run") as run:
        result = CliRunner().invoke(main, ["run", "--config", str(config), "mysql-prod"])
    assert result.exit_code == 1
    run.assert_not_called()
    record, = History(Config(config).history).records()
    assert record.encrypted and record.status == "failed"


def test_unreadable_key_fails_without_exposing_contents(tmp_path, key_file):
    with patch.object(Path, "open", side_effect=PermissionError("denied")):
        with pytest.raises(BackupError, match="Cannot read encryption key file"):
            XtraBackup(tmp_path / "backups", encrypt=True, encrypt_key_file=key_file)


def test_uncompressed_encrypted_prepare_cli_requires_key(tmp_path, key_file, encrypted_runner):
    xb = XtraBackup(tmp_path / "backups", encrypt=True, encrypt_key_file=key_file)
    runner, commands = encrypted_runner
    with patch("subprocess.run", side_effect=runner):
        full = xb.full_backup()
        args = ["xtrabackup", "prepare", str(full.path), "--root", str(xb.backup_root),
                "--dst", str(tmp_path / "recovery")]
        failed = CliRunner().invoke(main, args)
        assert failed.exit_code == 2
        assert "requires encrypt_key_file" in failed.output
        assert not (tmp_path / "recovery").exists()
        assert len(commands) == 1
        result = CliRunner().invoke(main, [*args, "--encrypt-key-file", str(key_file)])
    assert result.exit_code == 0, result.output
    assert "--decrypt=AES256" in commands[1]
    assert "--prepare" in commands[2]
    assert (tmp_path / "recovery/data.ibd").read_bytes() == b"database pages"


@pytest.mark.parametrize("binary", ["mariabackup", "mariadb-backup"])
def test_mariadb_encryption_rejected(tmp_path, key_file, binary):
    with pytest.raises(ValueError, match="Percona"):
        XtraBackup(tmp_path, binary=binary, encrypt=True, encrypt_key_file=key_file)


@pytest.mark.parametrize("change", ["disable", "key"])
def test_incremental_rejects_changed_encryption(tmp_path, key_file, encrypted_runner, change):
    xb = XtraBackup(tmp_path / "backups", encrypt=True, encrypt_key_file=key_file)
    runner, commands = encrypted_runner
    with patch("subprocess.run", side_effect=runner):
        xb.full_backup()
        if change == "disable":
            xb.encrypt = False
        else:
            key_file.write_bytes(b"a" * 32)
        with pytest.raises(BackupError, match="new full backup"):
            xb.incremental_backup()
    assert len(commands) == 1


@pytest.mark.parametrize("failure", ["wrong_key", "decrypt_error", "decrypt_noop"])
def test_failed_decryption_preserves_source_and_does_not_publish(
    tmp_path, key_file, encrypted_runner, failure,
):
    xb = XtraBackup(tmp_path / "backups", encrypt=True, encrypt_key_file=key_file)
    runner, commands = encrypted_runner
    with patch("subprocess.run", side_effect=runner):
        full = xb.full_backup()
    original = {p.name: p.read_bytes() for p in full.path.iterdir()}
    if failure == "wrong_key":
        key_file.write_bytes(b"b" * 32)
    exit_code = 1 if failure == "decrypt_error" else 0
    destination = tmp_path / "recovery"
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], exit_code)):
        with pytest.raises(BackupError):
            xb.prepare(full.path, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".bdbackup-prepare-*"))
    assert {p.name: p.read_bytes() for p in full.path.iterdir()} == original


def test_encryption_flag_does_not_claim_plaintext_backup(tmp_path, key_file, physical_runner):
    xb = XtraBackup(tmp_path / "backups", encrypt=True, encrypt_key_file=key_file)
    with patch("subprocess.run", side_effect=physical_runner):
        with pytest.raises(BackupError, match="no encrypted files"):
            xb.full_backup()
    assert not list(xb.backup_root.rglob("*.tmp"))
    assert not list(xb.backup_root.rglob(".full_success"))


def test_old_history_reads_without_mutation_and_migrates_on_write(tmp_path):
    path = tmp_path / "history.db"
    artifact = tmp_path / "backup"
    artifact.touch()
    history = History(HistorySettings(path, tmp_path / "restore"))
    history.run("old", "file", lambda: BackupResult(artifact, success=True))
    # Reconstruct the previous schema, retaining an actual previous run.
    with closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE backup_runs DROP COLUMN encrypted")
        db.execute("PRAGMA user_version=1")
        db.commit()
    before = path.read_bytes()
    assert not history.records()[0].encrypted
    assert not history.get(1).encrypted
    assert path.read_bytes() == before
    history.run("new", "file", lambda: BackupResult(artifact, success=True), encrypted=True)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        rows = db.execute("SELECT encrypted FROM backup_runs ORDER BY id").fetchall()
        assert rows == [(0,), (1,)]
