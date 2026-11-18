"""A reader that closes the pipe (``bdbackup history | head``) must not turn a finished command
into a traceback or a changed exit code: the rest of the output is discarded and the run goes on.

Each test starts the CLI as a real process whose stdout is the write end of a pipe with no
reader, so the first line it prints fails with a broken pipe, the way it does under ``head``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from bdbackup.backends import BackupResult
from bdbackup.history import History, HistorySettings

ROOT = Path(__file__).resolve().parents[1]
NOISE = ("Traceback", "Broken pipe", "BrokenPipeError", "Exception ignored")

CONFIG = """[history]
database = "state/history.sqlite3"
restore_root = "recovery"

[daily]
type = "file"
template_filename = "paths"
chdir = "source"
backup_dst = "archives/daily"
format = "tar.gz"

[gfs-main]
type = "gfs"
[[gfs-main.stage]]
paths = ["STAGE1"]
period = "daily"
keep = "5d"
[[gfs-main.stage]]
paths = ["STAGE2"]
period = "daily"
keep = "20d"
"""


def make_site(root: Path) -> Path:
    """A config with a file job and a gfs job over two existing stages; returns the config."""
    (root / "source").mkdir(parents=True)
    (root / "source" / "hello").write_text("recover me")
    (root / "paths").write_text("hello\n")
    (root / "STAGE1").mkdir()
    (root / "STAGE2").mkdir()
    (root / "STAGE2" / ".bdbackup-destination").write_text("")
    config = root / "config.toml"
    config.write_text(CONFIG)
    return config


def add_record(root: Path) -> None:
    """One successful history record, as a run of the file job leaves it."""
    artifact = root / "unit.tar"
    artifact.write_bytes(b"unit")
    History(HistorySettings(root / "state" / "history.sqlite3", root / "recovery")).run(
        "daily", "file", lambda: BackupResult(artifact, 4, True), {"kind": "file"}
    )


def add_record_without_checksum(root: Path) -> None:
    """A record older than the ledger: ``history --create-checksum`` has a line to print for it."""
    add_record(root)
    with closing(sqlite3.connect(root / "state" / "history.sqlite3")) as db, db:
        db.execute("UPDATE backup_runs SET unit=NULL, checksum=NULL")


def remove_the_source(root: Path) -> None:
    (root / "source" / "hello").unlink()


def nothing(root: Path) -> None:
    """The site as ``make_site`` leaves it."""


def bdbackup(args: list[str], root: Path, *, reader_gone: bool) -> subprocess.CompletedProcess:
    """Run ``python -m bdbackup.cli args`` from this checkout; stdout is a pipe or a dead pipe."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = str(ROOT)
    env["BDBACKUP_CONFIG_DIR"] = str(root / "cfg")
    command = [sys.executable, "-m", "bdbackup.cli", *args]
    if not reader_gone:
        return subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, env=env, cwd=ROOT, timeout=120
        )
    read, write = os.pipe()
    os.close(read)  # before the start, so the child's first write fails at once
    try:
        return subprocess.run(  # noqa: S603
            command, stdout=write, stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT,
            timeout=120,
        )
    finally:
        os.close(write)


CASES = [
    pytest.param(["history", "-c", "{config}"], 0, add_record, id="history-listing"),
    pytest.param(
        ["history", "-c", "{config}", "--create-checksum"], 0, add_record_without_checksum,
        id="history-create-checksum",
    ),
    pytest.param(
        ["run", "-c", "{config}", "-j", "daily", "--validate"], 0, nothing, id="validate-passes"
    ),
    pytest.param(
        ["run", "-c", "{config}", "-j", "daily", "--validate"], 1, remove_the_source,
        id="validate-fails",
    ),
    pytest.param(
        ["run", "-c", "{config}", "-j", "gfs-main", "--dry-run"], 0, nothing, id="gfs-report"
    ),
    pytest.param(
        ["run", "-c", "{config}", "-j", "gfs-main", "--initialize-structure"], 0, nothing,
        id="initialize-structure",
    ),
]


@pytest.mark.parametrize(("args", "code", "prepare"), CASES)
def test_a_closed_stdout_keeps_the_exit_code_and_prints_no_error(tmp_path, args, code, prepare):
    results = {}
    # Two sites, because a command such as --create-checksum changes what it prints next time.
    for name, reader_gone in (("with-reader", False), ("reader-gone", True)):
        root = tmp_path / name
        config = make_site(root)
        prepare(root)
        results[name] = bdbackup(
            [arg.format(config=config) for arg in args], root, reader_gone=reader_gone
        )

    reference = results["with-reader"]
    assert reference.returncode == code, reference.stderr
    assert reference.stdout, "the command prints nothing, so there is no write to fail"

    broken = results["reader-gone"]
    assert broken.returncode == code, broken.stderr
    for noise in NOISE:
        assert noise not in broken.stderr, broken.stderr
