"""Restore recorded backups into a new recovery directory."""

from __future__ import annotations

import gzip
import json
import re
import shutil
from pathlib import Path

from bdbackup.backends import BackupError
from bdbackup.filebackup import FileBackup
from bdbackup.history import BackupRecord
from bdbackup.mysql import MySQLBackup, XtraBackup


def backup_restore_info(backend) -> dict[str, str]:
    """Save only recovery routing, never connection credentials or arbitrary options."""
    if isinstance(backend, FileBackup):
        return {"kind": "file"}
    if isinstance(backend, MySQLBackup):
        return {"kind": "mysqldump"}
    if isinstance(backend, XtraBackup):
        return {"kind": "xtrabackup", "backup_root": str(backend.backup_root),
                "binary": backend.binary}
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


def restore_record(record: BackupRecord, destination: Path) -> Path:
    if not record.available:
        raise BackupError("Backup is unsuccessful, missing, or has been replaced/modified")
    source = Path(record.path)
    # Do not resolve the final component: a dangling destination symlink must be rejected.
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError(f"Recovery destination must be new: {destination}")
    info = json.loads(record.restore_info)
    kind = info.get("kind")
    if kind == "xtrabackup":
        backend = XtraBackup(backup_root=info["backup_root"], binary=info["binary"])
        return backend.prepare(source, destination)
    if kind not in {"file", "mysqldump"}:
        raise BackupError(f"History restore is not supported for engine {record.backup_type!r}")
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
        if not record.available:
            raise BackupError("Backup changed during restoration")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination
