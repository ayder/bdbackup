"""The pgbackrest engine's export: one tar hashed while written, and one read-back verify."""

from __future__ import annotations

import errno
import gc
import hashlib
import io
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bdbackup import ledger
from bdbackup.backends import BackupError, BackupResult
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History, HistorySettings
from bdbackup.postgres import pgbackrest
from bdbackup.postgres.pgbackrest import PgBackRest
from bdbackup.postgres.repository import FullBackup
from tests.pgbackrest_fake import make_repo

OLDER = "20261108-010000F"
NEWEST = "20261109-010000F"
HEX = "0123456789abcdef" * 2  # 32 hex digits, the shape of uuid4().hex


def full(label: str = NEWEST) -> FullBackup:
    return FullBackup(
        label=label,
        stop=datetime(2026, 11, 9, 1, 1, tzinfo=UTC),
        pg_version="16",
        page_errors=False,
        repo_bytes=0,
    )


def make_engine(tmp_path, repo, *, stanza="main", marker=True) -> PgBackRest:
    stage = tmp_path / "stage1"
    stage.mkdir(exist_ok=True)
    if marker:
        (stage / MARKER).write_text("")
    return PgBackRest(
        stanza=stanza,
        repo_path=repo,
        out_dir=stage / "pg",
        first_stage=stage,
        lock_file=tmp_path / "state" / "history.sqlite3.pgbackrest-pg.lock",
    )


def history(tmp_path) -> History:
    return History(
        HistorySettings(
            database=tmp_path / "state" / "history.sqlite3", restore_root=tmp_path / "restores"
        )
    )


def members(path: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(path) as tar:
        return tar.getmembers()


def expected_names(stanza: str = "main", label: str = NEWEST) -> list[str]:
    full_dir = f"backup/{stanza}/{label}"
    return [
        full_dir,
        f"{full_dir}/backup.manifest",
        f"{full_dir}/pg_data",
        f"{full_dir}/pg_data/PG_VERSION.gz",
        f"{full_dir}/pg_data/base",
        f"{full_dir}/pg_data/base/1",
        f"{full_dir}/pg_data/base/1/1259.gz",
        f"backup/{stanza}/backup.info",
        f"backup/{stanza}/backup.info.copy",
        f"archive/{stanza}/archive.info",
        f"archive/{stanza}/archive.info.copy",
    ]


def tree(root: Path) -> dict[str, tuple]:
    """Every entry under ``root`` with its mode, size, mtime, inode and file bytes."""
    found = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        data = path.read_bytes() if path.is_file() else None
        found[path.relative_to(root).as_posix()] = (
            st.st_mode,
            st.st_size,
            st.st_mtime_ns,
            st.st_ino,
            data,
        )
    return found


# ---- the unit -------------------------------------------------------------------------------


def test_export_layout(tmp_path):
    repo = make_repo(tmp_path, fulls=((OLDER, True, False), (NEWEST, True, False)))
    engine = make_engine(tmp_path, repo)

    result = engine.export(full())

    assert result.path == engine.unit_path(NEWEST)
    assert result.success
    assert result.size_bytes == result.path.stat().st_size
    assert result.checksum == hashlib.sha256(result.path.read_bytes()).hexdigest()
    # Only the published unit is left in out_dir: no temporary file.
    assert sorted(p.name for p in engine.out_dir.iterdir()) == [f"main-{NEWEST}.tar"]

    infos = members(result.path)
    assert [m.name for m in infos] == expected_names()
    with tarfile.open(result.path) as tar:
        for member in infos:
            source = repo / member.name
            assert member.isdir() == source.is_dir()
            if member.isfile():
                assert tar.extractfile(member).read() == source.read_bytes()
    # The info files carry the stat they were read with.
    for member in infos[-4:]:
        st = (repo / member.name).stat()
        assert member.mode == st.st_mode & 0o7777
        assert int(member.mtime) == int(st.st_mtime)
        assert member.size == st.st_size


def test_export_checksum_matches_ledger(tmp_path):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo)
    store = history(tmp_path)

    result = store.run("pg", "pgbackrest-full", lambda: engine.export(full()), {"label": NEWEST})

    [record] = store.records(job="pg")
    assert record.status == "success"
    assert record.checksum == result.checksum == ledger.checksum(result.path)


def test_symlinked_repo_path_members(tmp_path):
    real = make_repo(tmp_path / "disk")
    link = tmp_path / "var-lib-pgbackrest"
    link.symlink_to(real, target_is_directory=True)
    engine = make_engine(tmp_path, link)

    result = engine.export(full())

    names = [m.name for m in members(result.path)]
    assert names == expected_names()
    assert all(name.startswith(("backup/main/", "archive/main/")) for name in names)
    engine.verify(result)


def test_repo_untouched(tmp_path):
    repo = make_repo(tmp_path, fulls=((OLDER, True, False), (NEWEST, True, False)))
    engine = make_engine(tmp_path, repo)
    before = tree(repo)

    engine.verify(engine.export(full()))

    assert tree(repo) == before


# ---- verify ---------------------------------------------------------------------------------


def test_verify_reads_once_and_matches(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo)
    result = engine.export(full())
    size = result.path.stat().st_size

    counted = []
    original = pgbackrest._HashingReader.read

    def counting(self, *args):
        data = original(self, *args)
        counted.append(len(data))
        return data

    opened = []

    def tracking_open(file, *args, **kwargs):
        opened.append((Path(file), *args))
        return open(file, *args, **kwargs)

    monkeypatch.setattr(pgbackrest._HashingReader, "read", counting)
    # Shadows the builtin inside the engine module only: every file the engine opens.
    monkeypatch.setattr(pgbackrest, "open", tracking_open, raising=False)

    verified = engine.verify(result)

    assert sum(counted) == size
    assert opened == [(result.path, "rb")]
    assert verified.path == result.path
    assert verified.success
    assert verified.checksum == result.checksum
    assert verified.size_bytes == size


@pytest.mark.parametrize("where", ["data", "padding"])
def test_verify_detects_corruption(tmp_path, where):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo)
    result = engine.export(full())
    data = bytearray(result.path.read_bytes())
    # A byte inside a data file's content, or the very last byte of the tar's zero padding,
    # which only the drain after the walk reads.
    offset = data.index(f"{NEWEST} 1259".encode()) if where == "data" else len(data) - 1
    data[offset] ^= 0x01
    result.path.write_bytes(bytes(data))

    with pytest.raises(BackupError, match="checksum"):
        engine.verify(result)


def test_verify_detects_other_members(tmp_path):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo)
    result = engine.export(full())
    # Replace the unit with a valid tar of other members, and claim its own digest.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("backup/main/backup.info")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    result.path.write_bytes(buffer.getvalue())
    forged = BackupResult(
        result.path,
        size_bytes=len(buffer.getvalue()),
        success=True,
        checksum=hashlib.sha256(buffer.getvalue()).hexdigest(),
    )

    with pytest.raises(BackupError, match="members"):
        engine.verify(forged)


# ---- refusals before anything is written ----------------------------------------------------


def test_marker_missing_creates_nothing(tmp_path):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo, marker=False)

    with pytest.raises(BackupError) as caught:
        engine.export(full())

    assert str(caught.value) == (
        f"{engine.first_stage}: .bdbackup-destination missing; "
        "run --initialize-structure for its gfs job"
    )
    assert list(engine.first_stage.iterdir()) == []
    assert not engine.out_dir.exists()


def test_require_marker_touches_nothing_when_the_stage_is_absent(tmp_path):
    repo = make_repo(tmp_path)
    engine = PgBackRest(
        stanza="main",
        repo_path=repo,
        out_dir=tmp_path / "share" / "stage1" / "pg",
        first_stage=tmp_path / "share" / "stage1",
        lock_file=tmp_path / "pg.lock",
    )

    with pytest.raises(BackupError, match="missing"):
        engine.require_marker()

    assert not (tmp_path / "share").exists()


def test_refuse_root(monkeypatch, tmp_path):
    engine = make_engine(tmp_path, tmp_path / "repo")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(BackupError) as caught:
        engine.refuse_root()
    assert str(caught.value) == (
        "pgbackrest jobs do not run as root; run as the OS user that owns the repository"
    )

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    engine.refuse_root()


def test_existing_final_name_fails(tmp_path):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo)
    final = engine.unit_path(NEWEST)
    final.parent.mkdir(parents=True)
    final.write_bytes(b"an earlier export")

    with pytest.raises(BackupError) as caught:
        engine.export(full())

    assert str(caught.value) == f"unit already exists: {final}"
    assert final.read_bytes() == b"an earlier export"
    assert [p.name for p in engine.out_dir.iterdir()] == [final.name]


def test_backup_and_prune(tmp_path):
    engine = make_engine(tmp_path, tmp_path / "repo")

    with pytest.raises(BackupError) as caught:
        engine.backup()

    assert str(caught.value) == "pgbackrest jobs run only through bdbackup run"
    assert engine.prune() == []


# ---- failures midway publish nothing --------------------------------------------------------


def test_input_change_fails(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo)
    target = repo / "backup" / "main" / NEWEST / "pg_data" / "PG_VERSION.gz"
    original = tarfile.TarFile.addfile

    def addfile_then_change(self, tarinfo, fileobj=None, **kwargs):
        original(self, tarinfo, fileobj, **kwargs)
        if tarinfo.name.endswith("PG_VERSION.gz"):
            with target.open("ab") as stream:
                stream.write(b" grown by an expire")

    monkeypatch.setattr(tarfile.TarFile, "addfile", addfile_then_change)

    with pytest.raises(BackupError) as caught:
        engine.export(full())

    assert str(caught.value) == f"input changed during export: {target}"
    assert list(engine.out_dir.iterdir()) == []


# The failed tar stream flushes its buffer again when it is collected; that write must not
# reach the closed descriptor (another file may reuse its number), so it must not raise.
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_write_error_leaves_no_unit(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    # Enough data for several writes, so the failure comes with a partial temp file on disk.
    big = repo / "backup" / "main" / NEWEST / "pg_data" / "base" / "1" / "16384.gz"
    big.write_bytes(os.urandom(3 * 1024 * 1024))
    engine = make_engine(tmp_path, repo)
    store = history(tmp_path)
    original = pgbackrest._HashingWriter.write
    calls = []
    at_failure = []

    def fail_second_write(self, data):
        calls.append(len(data))
        if len(calls) == 2:
            at_failure.extend((p.name, p.stat().st_size) for p in engine.out_dir.iterdir())
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return original(self, data)

    monkeypatch.setattr(pgbackrest._HashingWriter, "write", fail_second_write)

    with pytest.raises(BackupError, match=os.strerror(errno.ENOSPC)):
        store.run("pg", "pgbackrest-full", lambda: engine.export(full()), {"label": NEWEST})
    gc.collect()

    # A partly written temporary file was on disk when the write failed, and is gone now.
    [(name, size)] = at_failure
    assert name.startswith(f".main-{NEWEST}.tar.tmp-")
    assert size > 0
    assert list(engine.out_dir.iterdir()) == []
    [record] = store.records(job="pg")
    assert record.status == "failed"
    assert record.error == "BackupError"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_unreadable_file_fails_cleanly(tmp_path):
    repo = make_repo(tmp_path)
    engine = make_engine(tmp_path, repo)
    target = repo / "backup" / "main" / NEWEST / "pg_data" / "base" / "1" / "1259.gz"
    target.chmod(0)
    try:
        with pytest.raises(BackupError) as caught:
            engine.export(full())
    finally:
        target.chmod(0o640)

    assert str(target) in str(caught.value)
    assert list(engine.out_dir.iterdir()) == []


# ---- temporary files ------------------------------------------------------------------------


def test_cleanup_ignores_other_stanza_temps(tmp_path):
    repo = make_repo(tmp_path, stanza="main")
    make_repo(tmp_path, stanza="main-db")
    main = make_engine(tmp_path, repo, stanza="main")
    main_db = make_engine(tmp_path, repo, stanza="main-db")
    out = main.out_dir
    out.mkdir(parents=True)
    main_stale = out / f".main-{OLDER}.tar.tmp-{HEX}"
    main_db_live = out / f".main-db-{OLDER}.tar.tmp-{HEX}"
    not_ours = [
        out / f".main-{OLDER}.tar.tmp-short",  # not a uuid4 hex
        out / f".main-{OLDER}.tar.tmp-{HEX}.keep",  # does not fully match
        out / f".main-not-a-label.tar.tmp-{HEX}",
        out / f"main-{OLDER}.tar",  # a published unit
    ]
    for path in [main_stale, main_db_live, *not_ours]:
        path.write_bytes(b"partial")

    main.export(full())

    assert not main_stale.exists()
    assert main_db_live.exists()
    assert all(path.exists() for path in not_ours)

    # And the other way round: the main-db job leaves the main job's temporary file alone.
    main_live = out / f".main-{OLDER}.tar.tmp-{HEX}"
    main_live.write_bytes(b"partial")

    main_db.export(full())

    assert not main_db_live.exists()
    assert main_live.exists()
    assert [m.name for m in members(main_db.unit_path(NEWEST))] == expected_names("main-db")
