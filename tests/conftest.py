"""Synthetic physical-backup process fixtures; never connect to a database."""

import subprocess
from pathlib import Path

import pytest


def write_checkpoints(path, start=0, end=100, kind=None):
    path.mkdir(parents=True, exist_ok=True)
    kind = kind or ("incremental" if start else "full-backuped")
    (path / "xtrabackup_checkpoints").write_text(
        f"backup_type = {kind}\nfrom_lsn = {start}\nto_lsn = {end}\n"
    )


@pytest.fixture
def physical_runner():
    def run(cmd, **kwargs):
        assert cmd[1].startswith("--defaults-extra-file=")
        target = Path(next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--target-dir=")))
        if "--backup" in cmd:
            from bdbackup.mysql import XtraBackup

            parent = next(
                (
                    Path(arg.split("=", 1)[1])
                    for arg in cmd
                    if arg.startswith("--incremental-basedir=")
                ),
                None,
            )
            start = XtraBackup._checkpoints(parent)["to_lsn"] if parent else 0
            write_checkpoints(target, start, start + 100)
            (target / "data.ibd").write_bytes(b"database pages")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok")

    return run
