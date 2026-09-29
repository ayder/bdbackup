"""Restore recorded backups into a new recovery directory."""

from __future__ import annotations

import gzip
import json
import re
import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path

from bdbackup.backends import BackupError
from bdbackup.filebackup import FileBackup
from bdbackup.gfs import store
from bdbackup.history import BackupRecord, History
from bdbackup.mysql import MySQLBackup, XtraBackup


def backup_restore_info(backend) -> dict[str, str]:
    """Save only recovery routing, never connection credentials or arbitrary options."""
    if isinstance(backend, FileBackup):
        return {"kind": "file"}
    if isinstance(backend, MySQLBackup):
        return {"kind": "mysqldump"}
    if isinstance(backend, XtraBackup):
        info = {"kind": "xtrabackup", "backup_root": str(backend.backup_root),
                "binary": backend.binary}
        if backend.encrypt:
            info.update(encryption="AES256", encrypt_key_file=str(backend.encrypt_key_file))
        return info
    return {}


def suggested_destination(record: BackupRecord, root: Path) -> Path:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", record.job_name).strip("-")[:80] or "backup"
    base = root / f"{name}-{record.id}"
    target = base
    number = 1
    while target.exists() or target.is_symlink():
        target = base.with_name(f"{base.name}-{number}")
        number += 1
    return target


def restore_record(
    record: BackupRecord, destination: Path, *, encrypt_key_file: Path | None = None,
    history: History | None = None, on_source: Callable[[Path], None] | None = None,
) -> Path:
    """Restore a record; a unit GFS manages is restored from its current location.

    ``on_source`` receives the path restored from, once, before anything is written.
    """
    known = store.places(history) if history is not None else {}
    if record.id not in known:
        if not record.available:
            raise BackupError("Backup is unsuccessful, missing, or has been replaced/modified")
        return _restore(record, Path(record.path), None, destination, encrypt_key_file,
                        on_source, lambda: record.available)
    if not known[record.id]:
        raise BackupError("Backup is unsuccessful, missing, or has been replaced/modified")
    reasons = []
    for place in known[record.id]:
        if not store.place_available(place):
            reasons.append(f"{place.member}: missing or replaced")
            continue
        return _restore(record, place.member, place.unit.parent, destination,
                        encrypt_key_file, on_source, partial(store.place_available, place))
    raise BackupError("No location of this backup verifies: " + "; ".join(reasons))


def _restore(
    record: BackupRecord, source: Path, backup_root: Path | None, destination: Path,
    encrypt_key_file: Path | None, on_source: Callable[[Path], None] | None,
    still_available: Callable[[], bool],
) -> Path:
    # Do not resolve the final component: a dangling destination symlink must be rejected.
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError(f"Recovery destination must be new: {destination}")
    info = json.loads(record.restore_info)
    kind = info.get("kind")
    if kind not in {"xtrabackup", "file", "mysqldump"}:
        raise BackupError(f"History restore is not supported for engine {record.backup_type!r}")
    if on_source is not None:
        on_source(source)
    if kind == "xtrabackup":
        backend = XtraBackup(
            # A moved date directory keeps its siblings' layout, so its parent is the root.
            backup_root=backup_root or info["backup_root"], binary=info["binary"],
            encrypt=bool(record.encrypted),
            encrypt_key_file=encrypt_key_file or info.get("encrypt_key_file"),
        )
        return backend.prepare(source, destination)
    destination.mkdir(parents=True, mode=0o700)
    try:
        if kind == "file":
            FileBackup.restore_archive(source, destination)
        else:
            sql = destination / "backup.sql"
            with gzip.open(source, "rb") as archive, sql.open("xb") as output:
                sql.chmod(0o600)
                shutil.copyfileobj(archive, output, 1024 * 1024)
            if not sql.stat().st_size:
                raise BackupError("Dump contains no SQL")
        if not still_available():
            raise BackupError("Backup changed during restoration")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination
