"""Usage/performance improvement tests for bdbackup."""

from __future__ import annotations

from pathlib import Path

from bdbackup.filebackup import FileBackup
from bdbackup.mysql import MySQLBackup, XtraBackup


def test_file_backup_format_option(tmp_path: Path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    template = tmp_path / "t.txt"
    template.write_text("a.txt\n")

    fb = FileBackup(
        backup_dst=tmp_path / "backup",
        template_filename=template,
        format="tar.gz",
        chdir=tmp_path,
    )
    fb.backup()
    fb.close_archive()
    assert fb.tar_filename.suffixes == [".tar", ".gz"]


def test_file_backup_dry_run(tmp_path: Path):
    src = tmp_path / "a.txt"
    src.write_text("a")
    template = tmp_path / "t.txt"
    template.write_text("a.txt\n")

    fb = FileBackup(
        backup_dst=tmp_path / "backup",
        template_filename=template,
        chdir=tmp_path,
    )
    result = fb.backup(dry_run=True)
    assert not fb.tar_filename.exists()
    assert result.size_bytes > 0


def test_file_backup_pattern_exclude(tmp_path: Path):
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "skip.log").write_text("skip")
    template = tmp_path / "t.txt"
    template.write_text("keep.txt\nskip.log\n")

    fb = FileBackup(
        backup_dst=tmp_path / "backup",
        template_filename=template,
        chdir=tmp_path,
        exclude_pattern=["*.log"],
    )
    fb.backup()
    fb.close_archive()

    import tarfile

    with tarfile.open(fb.tar_filename, "r") as tar:
        names = tar.getnames()
    assert "keep.txt" in names
    assert "skip.log" not in names


def test_mysqlbackup_defaults_include_routines():
    mb = MySQLBackup()
    assert "--routines" in mb.options
    assert "--events" in mb.options
    assert "--triggers" in mb.options


def test_xtrabackup_passes_parallel_and_throttle():
    xb = XtraBackup(
        backup_root="/tmp/ignored",
        compress="zstd",
        compress_threads=4,
        parallel=8,
        throttle=100,
    )
    cmd = xb._base_cmd(Path("/tmp/target"), Path("/tmp/cnf"))
    assert "--parallel=8" in cmd
    assert "--throttle=100" in cmd
