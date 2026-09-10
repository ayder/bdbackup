"""Safety-net tests for bdbackup."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from bdbackup.backends import BackupError
from bdbackup.filebackup import FileBackup
from bdbackup.mysql import XtraBackup
from bdbackup.utils import process_lock
from tests.conftest import write_checkpoints


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
    with pytest.raises(BackupError, match="contains no members"):
        fb.close_archive()
    assert not fb.tar_filename.exists()


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
    for day in (old, new):
        write_checkpoints(day / "Full_000000")
        (day / "Full_Latest").symlink_to("Full_000000")
        (day / ".full_success").touch()

    removed = xb.prune()
    assert old in removed
    assert new not in removed


def test_xtrabackup_prune_ignores_non_date_dirs(tmp_path: Path):
    xb = XtraBackup(backup_root=tmp_path, retention_days=1)
    other = tmp_path / "not-a-date"
    other.mkdir()
    with pytest.raises(BackupError, match="No successful full"):
        xb.prune()
    assert other.exists()


def test_xtrabackup_incremental_uses_latest_full_across_midnight(tmp_path, physical_runner):
    from datetime import UTC

    xb = XtraBackup(backup_root=tmp_path)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    with patch("subprocess.run", side_effect=physical_runner):
        with patch.object(xb, "_utc_now", return_value=yesterday):
            full = xb.full_backup()
        result = xb.incremental_backup()
    assert result.path.parent.name == full.path.name
    assert result.path.parent.parent.name == "Incremental"
    assert result.path.parent.parent.parent == full.path.parent
