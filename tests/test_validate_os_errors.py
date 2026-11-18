"""``run --validate`` reports any OS error on a checked path as a FAIL line, never a traceback.

On NFS the realistic ones are ``ESTALE``, ``EIO`` and ``ETIMEDOUT``. Python 3.12 raised them out
of ``Path.exists()``; 3.14 hid them as "does not exist". The errno is injected into the ``os`` that
``bdbackup.validation`` sees, so the file job's own walk and every other module stay untouched.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from bdbackup import validation
from bdbackup.gfs.stages import MARKER
from tests.test_validate_unreachable import GFS_JOB, assert_report, site, validate

CODES = [
    pytest.param(errno.ESTALE, id="ESTALE"),
    pytest.param(errno.EIO, id="EIO"),
    pytest.param(errno.ETIMEDOUT, id="ETIMEDOUT"),
]
HISTORY = '[history]\ndatabase = "state/h.sqlite3"\n'
SOURCE_OK = "[OK] Checked access to 1 selected source entries."


class FlakyOs:
    """What ``bdbackup.validation`` sees as ``os``: ``stat`` and ``lstat`` fail for one path."""

    def __init__(self, path: Path, code: int, calls: tuple[str, ...]):
        self.path, self.code, self.calls = path, code, calls

    def __getattr__(self, name):
        return getattr(os, name)

    def _call(self, name, target, args, kwargs):
        if name in self.calls and Path(target) == self.path:
            raise OSError(self.code, os.strerror(self.code), str(target))
        return getattr(os, name)(target, *args, **kwargs)

    def stat(self, target, *args, **kwargs):
        return self._call("stat", target, args, kwargs)

    def lstat(self, target, *args, **kwargs):
        return self._call("lstat", target, args, kwargs)


def fail(monkeypatch, path: Path, code: int, *calls: str) -> None:
    monkeypatch.setattr(validation, "os", FlakyOs(path, code, calls or ("stat", "lstat")))


def cannot_check(label: str, path: Path, code: int) -> str:
    return f"[FAIL] {label}: cannot check {path}: {os.strerror(code)} ({errno.errorcode[code]})"


@pytest.mark.parametrize("code", CODES)
def test_an_os_error_on_the_metrics_directory_is_a_fail_line(tmp_path, monkeypatch, code):
    root = tmp_path.resolve()
    (root / "metrics").mkdir()
    config = site(root, HISTORY + '[metrics]\npath = "metrics/bdbackup.prom"\n')
    fail(monkeypatch, root / "metrics", code)

    result = validate(config)

    assert_report(result, cannot_check("Metrics directory", root / "metrics", code), SOURCE_OK)


def test_an_os_error_on_an_ancestor_of_a_missing_directory_is_a_fail_line(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    config = site(root, HISTORY + '[metrics]\npath = "gone/metrics/bdbackup.prom"\n')
    fail(monkeypatch, root / "gone", errno.ESTALE)

    result = validate(config)

    assert_report(result, cannot_check("Metrics directory", root / "gone", errno.ESTALE), SOURCE_OK)


def test_an_os_error_on_the_history_database_is_a_fail_line(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "state").mkdir()
    (root / "state" / "h.sqlite3").write_text("")
    config = site(root, HISTORY)
    fail(monkeypatch, root / "state" / "h.sqlite3", errno.EIO)

    result = validate(config)

    assert_report(
        result, cannot_check("SQLite history", root / "state" / "h.sqlite3", errno.EIO), SOURCE_OK
    )


def test_an_os_error_on_the_metrics_file_is_a_fail_line(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "metrics").mkdir()
    (root / "metrics" / "bdbackup.prom").write_text("# old\n")
    config = site(root, HISTORY + '[metrics]\npath = "metrics/bdbackup.prom"\n')
    fail(monkeypatch, root / "metrics" / "bdbackup.prom", errno.ETIMEDOUT)

    result = validate(config)

    assert_report(
        result,
        cannot_check("Metrics file", root / "metrics" / "bdbackup.prom", errno.ETIMEDOUT),
        SOURCE_OK,
    )


def gfs_site(root: Path) -> Path:
    (root / "BACKUP").mkdir()
    (root / "locked" / "NFS" / "daily").mkdir(parents=True)
    (root / "locked" / "NFS" / "daily" / MARKER).write_text("")
    return site(root, HISTORY + GFS_JOB)


def test_an_os_error_on_a_gfs_stage_path_is_a_fail_line(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    config = gfs_site(root)
    stage = root / "locked" / "NFS" / "daily"
    fail(monkeypatch, stage, errno.ESTALE)

    result = validate(config)

    assert_report(result, cannot_check("Stage path", stage, errno.ESTALE), SOURCE_OK)


def test_an_os_error_on_a_gfs_marker_is_a_fail_line(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    config = gfs_site(root)
    marker = root / "locked" / "NFS" / "daily" / MARKER
    fail(monkeypatch, marker, errno.EIO)

    result = validate(config)

    assert_report(result, cannot_check("Stage path", marker, errno.EIO), SOURCE_OK)


@pytest.mark.parametrize("call", ["lstat", "stat"])
def test_an_os_error_on_one_source_entry_keeps_the_walk_going(tmp_path, monkeypatch, call):
    root = tmp_path.resolve()
    config = site(root, HISTORY, chdir=".")
    (root / "paths").write_text("source\n")
    (root / "source" / "second").write_text("2")
    fail(monkeypatch, root / "source" / "hello", errno.ESTALE, call)

    result = validate(config)

    # the directory, hello and second are all walked; only hello cannot be checked
    assert_report(
        result,
        cannot_check("Backup source", root / "source" / "hello", errno.ESTALE),
        "[OK] Checked access to 3 selected source entries.",
    )


def test_an_os_error_on_a_source_root_is_a_fail_line_and_the_other_roots_are_walked(
    tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    (root / "other").mkdir()
    (root / "other" / "data").write_text("data")
    config = site(root, HISTORY, chdir=".")
    (root / "paths").write_text("source/hello\nother/data\n")
    fail(monkeypatch, root / "source" / "hello", errno.EIO)

    result = validate(config)

    assert_report(
        result,
        cannot_check("Backup source", root / "source" / "hello", errno.EIO),
        SOURCE_OK,
    )


def test_eloop_is_still_a_missing_path(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "metrics").mkdir()
    config = site(root, HISTORY + '[metrics]\npath = "metrics/bdbackup.prom"\n')
    fail(monkeypatch, root / "metrics", errno.ELOOP)

    result = validate(config)

    lines = result.output.splitlines()
    assert (
        f"[FAIL] Metrics directory: {root / 'metrics'} does not exist; directory is required."
    ) in lines, result.output
    assert "cannot check" not in result.output
