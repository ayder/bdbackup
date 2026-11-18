"""Writing the textfile: a temporary file in the same directory, synced, then renamed."""

from __future__ import annotations

import errno
import os
import re
import stat

import pytest

from bdbackup.metrics.write import write

TEXT = "# TYPE bdbackup_metrics_success gauge\nbdbackup_metrics_success 1\n"


def mode_of(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def listing(directory) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


@pytest.fixture
def umask_077():
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def test_write_creates_the_file(tmp_path):
    target = tmp_path / "bdbackup.prom"

    write(target, TEXT)

    assert target.is_file()
    assert target.read_text() == TEXT
    assert listing(tmp_path) == ["bdbackup.prom"]


def test_write_replaces_an_existing_file(tmp_path):
    target = tmp_path / "bdbackup.prom"
    target.write_text("old\n")

    write(target, TEXT)

    assert target.read_text() == TEXT
    assert listing(tmp_path) == ["bdbackup.prom"]


def test_the_file_is_world_readable_whatever_the_umask(tmp_path, umask_077):
    target = tmp_path / "bdbackup.prom"

    write(target, TEXT)

    assert target.is_file()
    assert mode_of(target) == 0o644


def test_the_text_goes_through_a_synced_temporary_file_that_is_renamed(tmp_path, monkeypatch):
    target = tmp_path / "bdbackup.prom"
    events = []
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace
    opened = []

    def tracking_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        if ".tmp-" in os.fspath(path):
            opened.append((os.fspath(path), flags, mode, fd))
        return fd

    def tracking_fsync(fd):
        if any(fd == entry[3] for entry in opened):
            events.append(("fsync", os.fstat(fd).st_size))
        return real_fsync(fd)

    def tracking_replace(source, destination):
        mode = stat.S_IMODE(os.stat(source).st_mode)
        events.append(("replace", os.fspath(source), os.fspath(destination), mode))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "replace", tracking_replace)

    write(target, TEXT)

    assert len(opened) == 1
    [(temp, flags, mode, _)] = opened
    assert flags & os.O_EXCL and flags & os.O_CREAT and flags & os.O_WRONLY
    assert mode == 0o600
    # The temporary file is in the same directory, hidden, and its name does not end in .prom,
    # so the textfile collector never reads it.
    assert os.path.dirname(temp) == str(tmp_path)
    assert re.fullmatch(r"\.bdbackup\.prom\.tmp-[0-9a-f]{32}", os.path.basename(temp))
    assert not temp.endswith(".prom")
    # It was written in full and synced before the rename, and had its final mode by then.
    assert events == [
        ("fsync", len(TEXT.encode())),
        ("replace", temp, str(target), 0o644),
    ]
    assert listing(tmp_path) == ["bdbackup.prom"]


def test_a_short_write_is_continued(tmp_path, monkeypatch):
    target = tmp_path / "bdbackup.prom"
    real_open, real_write = os.open, os.write
    temps = set()

    def tracking_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if ".tmp-" in os.fspath(path):
            temps.add(fd)
        return fd

    def seven_bytes_at_a_time(fd, data):
        return real_write(fd, bytes(data)[:7] if fd in temps else data)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "write", seven_bytes_at_a_time)

    write(target, TEXT)

    assert target.is_file()
    assert target.read_text() == TEXT


def fail_for_the_temp_file(monkeypatch, name, error, *, after_bytes=None):
    """Make ``os.<name>`` raise ``error`` when it is given the temporary file's descriptor.

    With ``after_bytes``, ``os.write`` first writes that many bytes, then fails.
    """
    real_open, real_call = os.open, getattr(os, name)
    temps = set()
    state = {"written": 0}

    def tracking_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if ".tmp-" in os.fspath(path):
            temps.add(fd)
        return fd

    def failing(fd, *args, **kwargs):
        if fd not in temps:
            return real_call(fd, *args, **kwargs)
        if after_bytes is not None and state["written"] < after_bytes:
            count = real_call(fd, bytes(args[0])[: after_bytes - state["written"]])
            state["written"] += count
            return count
        raise error

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, name, failing)


FAILURES = [
    pytest.param("write", OSError(errno.ENOSPC, "No space left on device"), None, id="write"),
    pytest.param("write", OSError(errno.ENOSPC, "No space left on device"), 10, id="mid-write"),
    pytest.param("fsync", OSError(errno.EIO, "Input/output error"), None, id="fsync"),
    pytest.param("fchmod", OSError(errno.EPERM, "Operation not permitted"), None, id="fchmod"),
]


@pytest.mark.parametrize(("name", "error", "after_bytes"), FAILURES)
@pytest.mark.parametrize("existing", [True, False], ids=["old-file", "no-old-file"])
def test_a_failed_write_leaves_no_temporary_file_and_the_old_file_untouched(
    tmp_path, monkeypatch, name, error, after_bytes, existing
):
    target = tmp_path / "bdbackup.prom"
    if existing:
        target.write_text("old\n")
    fail_for_the_temp_file(monkeypatch, name, error, after_bytes=after_bytes)

    with pytest.raises(OSError) as raised:
        write(target, TEXT)

    assert raised.value.errno == error.errno
    assert listing(tmp_path) == (["bdbackup.prom"] if existing else [])
    if existing:
        assert target.read_text() == "old\n"


@pytest.mark.parametrize("existing", [True, False], ids=["old-file", "no-old-file"])
def test_a_failed_rename_leaves_no_temporary_file_and_the_old_file_untouched(
    tmp_path, monkeypatch, existing
):
    target = tmp_path / "bdbackup.prom"
    if existing:
        target.write_text("old\n")

    def refuse(source, destination):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", refuse)

    with pytest.raises(OSError, match="cross-device"):
        write(target, TEXT)

    assert listing(tmp_path) == (["bdbackup.prom"] if existing else [])
    if existing:
        assert target.read_text() == "old\n"


def test_a_missing_directory_is_an_oserror_and_creates_nothing(tmp_path):
    with pytest.raises(OSError):
        write(tmp_path / "absent" / "bdbackup.prom", TEXT)

    assert listing(tmp_path) == []
