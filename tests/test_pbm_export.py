"""PBM exports operate on synthetic storage entries without running PBM."""

import builtins
import errno
import hashlib
import io
import os
import signal
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from bdbackup import tar_export
from bdbackup.backends import BackupError
from bdbackup.mongodb.pbm import PBM
from tests.pbm_fake import NAME, make_storage

JOB = "mongo-main"
NODE = "secondary.example:27017"
FILENAME = f"{JOB}-{NAME.replace(':', '')}.tar"


@pytest.fixture
def engine(tmp_path):
    backend = PBM(tmp_path / "storage", tmp_path / "export")
    make_storage(backend.storage_path)
    return backend


def export(engine, *, verify=True):
    return engine.export(JOB, NAME, (NODE,), verify=verify)


def test_exports_only_the_named_backup_with_exact_bytes(engine):
    make_storage(engine.storage_path, "2026-11-24T12:03:14Z", data=b"older")
    pitr = engine.storage_path / "pbmPitr"
    pitr.mkdir()
    (pitr / "oplog.s2").write_bytes(b"not part of the unit")
    result = export(engine)
    assert result.success and result.path == engine.out_dir / FILENAME
    contents = result.path.read_bytes()
    assert result.checksum == hashlib.sha256(contents).hexdigest()
    assert result.size_bytes == len(contents)
    with tarfile.open(result.path, "r:") as archive:
        files = {
            member.name: archive.extractfile(member).read()
            for member in archive if member.isfile()
        }
        assert files == {
            f"{NAME}.pbm.json": b'{"status":"done"}',
            f"{NAME}/rs0/data/collection.s2": b"backup bytes\x00\x01\xff",
            f"{NAME}/rs0/oplog.s2": b"oplog bytes",
        }
        assert {member.name for member in archive if member.isdir()} == {
            NAME, f"{NAME}/rs0", f"{NAME}/rs0/data", f"{NAME}/rs0/empty",
        }
    assert list(engine.out_dir.iterdir()) == [result.path]


def snapshot(storage):
    return {
        path.relative_to(storage): (
            state.st_ino, state.st_mode, state.st_size, state.st_mtime_ns, state.st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in (storage, *storage.rglob("*"))
        if (state := path.lstat()) is not None
    }


def test_storage_is_unchanged_and_all_source_files_open_read_only(engine, monkeypatch):
    root = engine.storage_path.resolve()
    before = snapshot(root)
    observations = set()
    descriptors = {}
    real_descriptor_open = os.open

    def observe(filename, read_only, dir_fd=None):
        if isinstance(filename, int):
            return
        path = Path(filename)
        path = (descriptors.get(dir_fd, Path.cwd()) / path).resolve()
        if path.is_relative_to(root):
            assert read_only, f"PBM storage opened for writing: {path}"
            if path.is_file():
                observations.add(path.relative_to(root))

    def descriptor_open(filename, flags, *args, dir_fd=None, **kwargs):
        destructive = os.O_ACCMODE | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        observe(filename, not flags & destructive, dir_fd)
        fd = real_descriptor_open(filename, flags, *args, dir_fd=dir_fd, **kwargs)
        directory = descriptors.get(dir_fd, Path.cwd()) / Path(filename)
        if directory.is_dir():
            descriptors[fd] = directory
        else:
            descriptors.pop(fd, None)
        return fd

    def opening(original):
        def tracked(filename, mode="r", *args, **kwargs):
            observe(filename, mode in {"r", "rb"})
            return original(filename, mode, *args, **kwargs)
        return tracked

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", descriptor_open)
        patch.setattr(builtins, "open", opening(builtins.open))
        patch.setattr(io, "open", opening(io.open))
        export(engine)
    assert snapshot(root) == before, "an export must leave all PBM entries and metadata intact"
    expected = {path for path, entry in before.items() if entry[-1] is not None}
    assert observations == expected, "every PBM source file must be observed opened read-only"


@pytest.mark.parametrize("missing", ["metadata", "tree"])
def test_missing_entries_name_the_node_that_took_the_backup(engine, missing):
    if missing == "metadata":
        (engine.storage_path / f"{NAME}.pbm.json").unlink()
    else:
        (engine.storage_path / NAME).rename(engine.storage_path / "other-tree")
    with pytest.raises(BackupError, match=rf"PBM took backup {NAME} on .*secondary\.example"):
        export(engine)
    assert not (engine.out_dir / FILENAME).exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_rejects_non_file_members(engine, kind):
    bad = engine.storage_path / NAME / "rs0" / "bad"
    if kind == "symlink":
        bad.symlink_to(bad.parent / "oplog.s2")
    else:
        os.mkfifo(bad)
    with pytest.raises(BackupError, match="unsupported.*rs0/bad"):
        export(engine)
    assert not list(engine.out_dir.glob("*"))


@pytest.mark.parametrize("phase", ["write", "verify"])
def test_export_failure_cleans_up_temporary_file(engine, monkeypatch, phase):
    def fail(*args, **kwargs):
        if phase == "write":
            raise OSError(errno.EIO, "fixture write failed")
        raise BackupError("fixture verify failed")

    if phase == "write":
        monkeypatch.setattr(tar_export.HashingWriter, "write", fail)
    else:
        monkeypatch.setattr(tar_export, "read_back", fail)
    with pytest.raises(BackupError, match=f"fixture {phase} failed"):
        export(engine)
    assert not list(engine.out_dir.glob("*"))


def test_no_verify_skips_read_back_and_still_records_the_hash(engine, monkeypatch):
    monkeypatch.setattr(tar_export, "read_back", lambda *args: pytest.fail("unexpected read-back"))
    result = export(engine, verify=False)
    assert result.success, "a successful unverified tar still publishes"
    assert result.checksum == hashlib.sha256(result.path.read_bytes()).hexdigest()


@pytest.mark.parametrize("when", ["before", "during-verification", "dangling-link"])
def test_export_refuses_an_existing_final_name(engine, monkeypatch, when):
    engine.out_dir.mkdir()
    final = engine.out_dir / FILENAME
    if when == "before":
        final.write_bytes(b"other owner's unit")
    elif when == "dangling-link":
        final.symlink_to("missing")
    else:
        actual = tar_export.read_back

        def verify(*args):
            actual(*args)
            final.write_bytes(b"other owner's unit")

        monkeypatch.setattr(tar_export, "read_back", verify)
    with pytest.raises(BackupError, match="unit already exists"):
        export(engine)
    if when == "dangling-link":
        assert final.readlink() == Path("missing")
    else:
        assert final.read_bytes() == b"other owner's unit"
    assert list(engine.out_dir.iterdir()) == [final]


def test_sigkill_during_write_leaves_only_this_jobs_temporary_file(engine):
    child_code = """
import os
import signal
import sys
from bdbackup import tar_export
from bdbackup.mongodb.pbm import PBM

writer = tar_export.HashingWriter.write
def interrupted(self, data):
    writer(self, data)
    os.kill(os.getpid(), signal.SIGKILL)
tar_export.HashingWriter.write = interrupted
storage, output, job, name, node = sys.argv[1:]
PBM(storage, output).export(job, name, (node,))
"""
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_code, str(engine.storage_path), str(engine.out_dir),
         JOB, NAME, NODE],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert child.returncode == -signal.SIGKILL, "the child must be killed after writing tar bytes"
    [partial] = engine.out_dir.iterdir()
    assert partial.name.startswith(f".{FILENAME}.tmp-") and partial.stat().st_size > 0
    assert not (engine.out_dir / FILENAME).exists()
    unrelated = engine.out_dir / f".another-{FILENAME.removeprefix(JOB + '-')}.tmp-{'a' * 32}"
    unrelated.write_bytes(b"another job")
    result = export(engine)
    assert result.success and not partial.exists()
    assert unrelated.read_bytes() == b"another job"
