"""Safety-net tests for bdbackup."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from bdbackup.backends import BackupError
from bdbackup.filebackup import FileBackup
from bdbackup.mysql import XtraBackup
from bdbackup.utils import process_lock


def test_process_lock_blocks_second_acquirer(tmp_path: Path):
    lock = tmp_path / "test.lock"
    with process_lock(lock):
        try:
            with process_lock(lock):
                raise AssertionError("Second lock acquisition should have failed")
        except BlockingIOError:
            pass


def test_file_backup_verify_rejects_empty_archive(tmp_path: Path):
    fb = FileBackup(backup_dst=tmp_path / "empty")
    fb.open_archive()
    fb.close_archive()
    try:
        fb.verify()
        raise AssertionError("verify should fail for empty archive")
    except BackupError as exc:
        assert "contains no members" in str(exc)


def test_file_backup_verify_passes_for_non_empty(tmp_path: Path):
    src = tmp_path / "file.txt"
    src.write_text("hello")
    template = tmp_path / "t.txt"
    template.write_text("file.txt\n")

    fb = FileBackup(
        backup_dst=tmp_path / "backup",
        template_filename=template,
        chdir=tmp_path,
    )
    result = fb.backup()
    fb.close_archive()
    verified = fb.verify(result)
    assert verified.success


def test_file_backup_restore(tmp_path: Path):
    src = tmp_path / "file.txt"
    src.write_text("hello")
    template = tmp_path / "t.txt"
    template.write_text("file.txt\n")

    fb = FileBackup(
        backup_dst=tmp_path / "backup",
        template_filename=template,
        chdir=tmp_path,
    )
    result = fb.backup()
    fb.close_archive()

    out = tmp_path / "out"
    fb.restore(out, result.path)
    assert (out / "file.txt").read_text() == "hello"


def test_xtrabackup_prune_uses_directory_name_date(tmp_path: Path):
    xb = XtraBackup(backup_root=tmp_path, retention_days=5)
    old = tmp_path / (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    new = tmp_path / (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    old.mkdir()
    new.mkdir()

    removed = xb.prune()
    assert old in removed
    assert new not in removed


def test_xtrabackup_prune_ignores_non_date_dirs(tmp_path: Path):
    xb = XtraBackup(backup_root=tmp_path, retention_days=1)
    other = tmp_path / "not-a-date"
    other.mkdir()
    removed = xb.prune()
    assert other not in removed
    assert other.exists()


def test_xtrabackup_incremental_uses_latest_full_across_midnight(tmp_path: Path):
    xb = XtraBackup(backup_root=tmp_path, user="xtrabackup")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    date_dir = tmp_path / yesterday
    date_dir.mkdir(parents=True)
    full = date_dir / "Full_000000"
    full.mkdir()
    (full / "xtrabackup_checkpoints").write_text("to_lsn = 1\n")
    latest = date_dir / "Full_Latest"
    latest.symlink_to(full, target_is_directory=True)
    (date_dir / ".full_success").touch()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        for part in cmd:
            if part.startswith("--target-dir="):
                Path(part.split("=", 1)[1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok")

    with patch("subprocess.run", side_effect=fake_run):
        result = xb.incremental_backup(acquire_lock=False)

    assert result.path.parent.name == "Incremental"
    assert (result.path.parent.parent / ".full_success").exists()
