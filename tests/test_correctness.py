"""Correctness tests for bdbackup internals."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from bdbackup.filebackup import FileBackup
from bdbackup.xtrabackup import XtraBackup


def test_xtrabackup_atomic_target_cleanup_on_failure(tmp_path: Path):
    """A failed full backup must not leave a dir that looks successful."""

    def fake_run(cmd, **kwargs):
        # Don't create tmp_target so the failure path looks like xtrabackup died.
        raise subprocess.CalledProcessError(1, cmd, output="disk full")

    xb = XtraBackup(backup_root=tmp_path, user="xtrabackup")
    with patch("subprocess.run", side_effect=fake_run):
        try:
            xb.full_backup()
        except subprocess.CalledProcessError:
            pass

    date_dir = tmp_path / xb.date_dir.name
    successful = [
        p.name for p in date_dir.iterdir()
        if p.name.startswith("Full_") and not p.name.endswith(".tmp")
    ]
    assert not successful


def test_xtrabackup_valid_base_rejects_empty_checkpoints(tmp_path: Path):
    xb = XtraBackup(backup_root=tmp_path, user="xtrabackup")
    base = tmp_path / "base"
    base.mkdir()
    (base / "xtrabackup_checkpoints").write_text("to_lsn = \n")
    assert not xb._valid_base(base)


def test_xtrabackup_valid_base_accepts_to_lsn(tmp_path: Path):
    xb = XtraBackup(backup_root=tmp_path, user="xtrabackup")
    base = tmp_path / "base"
    base.mkdir()
    (base / "xtrabackup_checkpoints").write_text("to_lsn = 1234567890\n")
    assert xb._valid_base(base)


def test_file_backup_preserves_relative_arcnames(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "data").mkdir()
    (base / "data" / "a.txt").write_text("a")
    (base / "other").mkdir()
    (base / "other" / "a.txt").write_text("b")

    template = tmp_path / "template.txt"
    template.write_text("data/a.txt\nother/a.txt\n")

    fb = FileBackup(
        backup_dst=tmp_path / "backup",
        template_filename=template,
        chdir=base,
    )
    fb.backup()
    fb.close_archive()

    import tarfile

    with tarfile.open(fb.tar_filename, "r") as tar:
        names = tar.getnames()

    assert "data/a.txt" in names
    assert "other/a.txt" in names


def test_file_backup_no_os_chdir(tmp_path: Path):
    original_cwd = Path.cwd()
    base = tmp_path / "base"
    base.mkdir()
    (base / "f.txt").write_text("x")

    template = tmp_path / "template.txt"
    template.write_text("f.txt\n")

    fb = FileBackup(
        backup_dst=tmp_path / "backup",
        template_filename=template,
        chdir=base,
    )
    fb.backup()
    fb.close_archive()

    assert Path.cwd() == original_cwd


def test_file_backup_absolute_path_arcname_strips_leading_slash(tmp_path: Path):
    src = tmp_path / "etc" / "config.txt"
    src.parent.mkdir(parents=True)
    src.write_text("cfg")

    template = tmp_path / "template.txt"
    template.write_text(str(src) + "\n")

    fb = FileBackup(backup_dst=tmp_path / "backup", template_filename=template)
    fb.backup()
    fb.close_archive()

    import tarfile

    with tarfile.open(fb.tar_filename, "r") as tar:
        names = tar.getnames()

    assert str(src).lstrip("/") in names
