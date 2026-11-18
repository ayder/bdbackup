"""``run --validate`` reports a path under a directory the OS user cannot search as a FAIL line.

A directory without the search (x) bit makes every ``stat`` below it fail with ``EACCES``.
Python 3.12 raises ``PermissionError`` from ``Path.exists()`` then; 3.14 returns ``False``. The
report must read alike on both, and must never end in a traceback.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.gfs.stages import MARKER
from bdbackup.validation import os_user

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root searches every directory")

FAILED = "Validation failed; see requirements above."

FILE_JOB = (
    '[daily]\ntype = "file"\ntemplate_filename = "paths"\nchdir = "{chdir}"\n'
    'backup_dst = "archives/daily"\nformat = "tar.gz"\n'
)
GFS_JOB = (
    '[gfs-main]\ntype = "gfs"\n'
    '[[gfs-main.stage]]\npaths = ["BACKUP"]\nperiod = "daily"\nkeep = "5d"\n'
    '[[gfs-main.stage]]\npaths = ["locked/NFS/daily"]\nperiod = "daily"\nkeep = "20d"\n'
)


@pytest.fixture
def lock():
    """Take the search bit off a directory; every one is restored so tmp cleanup works."""
    locked: list[Path] = []

    def apply(directory: Path) -> Path:
        directory.chmod(0o640)
        locked.append(directory)
        return directory

    yield apply
    for directory in locked:
        directory.chmod(0o700)


def site(root: Path, text: str, *, chdir: str = "source") -> Path:
    """A config at ``root`` with a readable file job ``daily`` after ``text``."""
    (root / "source").mkdir(exist_ok=True)
    (root / "source" / "hello").write_text("hello")
    (root / "archives").mkdir(exist_ok=True)
    (root / "paths").write_text("hello\n")
    config = root / "config.toml"
    config.write_text(text + FILE_JOB.format(chdir=chdir))
    return config


def validate(config: Path):
    return CliRunner().invoke(main, ["run", "-c", str(config), "--validate"])


def unreachable(label: str, path: Path, ancestor: Path) -> str:
    return (
        f"[FAIL] {label}: {path} is not reachable by OS user {os_user()}: "
        f"{ancestor} lacks search (x) permission; see namei -l {shlex.quote(str(path))}"
    )


def assert_report(result, *expected: str) -> None:
    """The FAIL lines are in the report, the report ends, the command exits 1, nothing crashed."""
    lines = result.output.splitlines()
    for line in expected:
        assert line in lines, result.output
    assert lines[-1] == FAILED, result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert result.exit_code == 1, result.output


# ---- the four places an operator puts a path --------------------------------------------------


def test_an_unsearchable_parent_of_the_metrics_directory_is_a_fail_line(tmp_path, lock):
    root = tmp_path.resolve()
    (root / "locked" / "metrics").mkdir(parents=True)
    config = site(root, '[history]\ndatabase = "state/h.sqlite3"\n'
                        '[metrics]\npath = "locked/metrics/bdbackup.prom"\n')
    lock(root / "locked")

    result = validate(config)

    assert_report(
        result,
        unreachable("Metrics directory", root / "locked" / "metrics", root / "locked"),
        "[OK] Checked access to 1 selected source entries.",
    )
    assert "Metrics file" not in result.output


def test_an_unsearchable_parent_of_a_gfs_stage_path_is_a_fail_line(tmp_path, lock):
    root = tmp_path.resolve()
    (root / "BACKUP").mkdir()
    (root / "locked" / "NFS" / "daily").mkdir(parents=True)
    config = site(root, '[history]\ndatabase = "state/h.sqlite3"\n' + GFS_JOB)
    lock(root / "locked")

    result = validate(config)

    assert_report(
        result,
        unreachable("Stage path", root / "locked" / "NFS" / "daily", root / "locked"),
        "[OK] Checked access to 1 selected source entries.",
    )
    assert "Job 'gfs-main' (gfs)" in result.output


def test_an_unsearchable_parent_of_the_history_directory_is_a_fail_line(tmp_path, lock):
    root = tmp_path.resolve()
    (root / "locked" / "state").mkdir(parents=True)
    (root / "locked" / "state" / "h.sqlite3").write_text("")
    config = site(root, '[history]\ndatabase = "locked/state/h.sqlite3"\n')
    lock(root / "locked")

    result = validate(config)

    assert_report(
        result,
        unreachable("SQLite history directory", root / "locked" / "state", root / "locked"),
        "[OK] Checked access to 1 selected source entries.",
    )
    # the directory line says it all; the database file below it is not reported a second time
    assert "SQLite history:" not in result.output


def test_an_unsearchable_parent_of_a_file_job_source_is_a_fail_line(tmp_path, lock):
    root = tmp_path.resolve()
    (root / "locked" / "source").mkdir(parents=True)
    (root / "locked" / "source" / "hello").write_text("hello")
    config = site(root, '[history]\ndatabase = "state/h.sqlite3"\n', chdir="locked/source")
    lock(root / "locked")

    result = validate(config)

    assert_report(
        result,
        unreachable("Backup source", root / "locked" / "source" / "hello", root / "locked"),
    )
    assert "Invalid settings or inaccessible path" not in result.output


def test_a_reachable_source_root_is_still_walked_beside_an_unreachable_one(tmp_path, lock):
    root = tmp_path.resolve()
    (root / "locked" / "source").mkdir(parents=True)
    (root / "locked" / "source" / "hello").write_text("hello")
    config = site(root, '[history]\ndatabase = "state/h.sqlite3"\n', chdir=".")
    (root / "paths").write_text("source/hello\nlocked/source/hello\n")
    lock(root / "locked")

    result = validate(config)

    assert_report(
        result,
        unreachable("Backup source", root / "locked" / "source" / "hello", root / "locked"),
        "[OK] Checked access to 1 selected source entries.",
    )


# ---- what must not change ---------------------------------------------------------------------


def test_the_first_ancestor_without_search_is_named_not_the_parent(tmp_path, lock):
    root = tmp_path.resolve()
    (root / "outer" / "inner" / "deep").mkdir(parents=True)
    config = site(root, '[history]\ndatabase = "state/h.sqlite3"\n'
                        '[metrics]\npath = "outer/inner/deep/bdbackup.prom"\n')
    lock(root / "outer")

    result = validate(config)

    assert_report(
        result,
        unreachable("Metrics directory", root / "outer" / "inner" / "deep", root / "outer"),
    )


def test_a_path_below_a_regular_file_is_missing_not_unreachable(tmp_path):
    root = tmp_path.resolve()
    (root / "file").write_text("x")  # ENOTDIR below it, as before
    config = site(root, '[history]\ndatabase = "state/h.sqlite3"\n'
                        '[metrics]\npath = "file/metrics/bdbackup.prom"\n')

    result = validate(config)

    lines = result.output.splitlines()
    assert (
        f"[FAIL] Metrics directory: {root / 'file' / 'metrics'} does not exist; "
        "directory is required."
    ) in lines, result.output
    assert not [line for line in lines if "is not reachable" in line]
    assert result.exit_code == 1, result.output


def test_a_searchable_tree_reports_no_unreachable_line(tmp_path):
    root = tmp_path.resolve()
    (root / "metrics").mkdir()
    (root / "BACKUP").mkdir()
    (root / "locked" / "NFS" / "daily").mkdir(parents=True)
    (root / "locked" / "NFS" / "daily" / MARKER).write_text("")
    config = site(root, '[history]\ndatabase = "state/h.sqlite3"\n'
                        '[metrics]\npath = "metrics/bdbackup.prom"\n' + GFS_JOB)

    result = validate(config)

    assert "is not reachable" not in result.output
    stage = root / "locked" / "NFS" / "daily"
    assert f"[OK] Stage path {stage}: writable, marker present and writable" in result.output
