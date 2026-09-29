"""Ledger facts recorded for every backup: the unit GFS moves and its checksum.

The checksum of a file is the SHA-256 of its content. The checksum of a directory is the
SHA-256 of its manifest: one line per regular file, ``<sha256 hex>  <posix relative path>``,
sorted by the UTF-8 bytes of the path. Anything else inside a directory (a symlink, fifo,
socket or device) cannot be verified after a copy, so it is an error.
"""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from bdbackup.backends import BackupError

_CHUNK = 1024 * 1024
_DATE_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")


class LedgerError(BackupError):
    """A backup's unit or checksum cannot be determined."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def checksum(path: Path) -> str:
    """Return the SHA-256 of a file, or of a directory's manifest."""
    mode = path.lstat().st_mode
    if stat.S_ISREG(mode):
        return _file_sha256(path)
    if not stat.S_ISDIR(mode):
        raise LedgerError(f"Not a regular file or directory: {path}")
    entries = []
    for entry in path.rglob("*"):
        entry_mode = entry.lstat().st_mode
        if stat.S_ISDIR(entry_mode):
            continue
        if not stat.S_ISREG(entry_mode):
            raise LedgerError(f"Unsupported entry in backup: {entry}")
        relative = entry.relative_to(path).as_posix()
        entries.append((relative.encode(), f"{_file_sha256(entry)}  {relative}\n"))
    entries.sort(key=lambda item: item[0])
    return hashlib.sha256("".join(line for _, line in entries).encode()).hexdigest()


def unit_path(path: Path, restore_info: Mapping[str, str]) -> Path:
    """Return the path GFS moves for this backup.

    An xtrabackup full or incremental belongs to its full's date directory, which holds the
    whole chain. Every other backup is its own unit.
    """
    if restore_info.get("kind") != "xtrabackup":
        return path
    root = Path(restore_info["backup_root"])
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise LedgerError(f"Physical backup {path} is outside its root {root}") from None
    full = len(parts) == 2 and parts[1].startswith("Full_")
    incremental = (
        len(parts) == 4 and parts[1] == "Incremental" and parts[2].startswith("Full_")
    )
    if not (parts and _DATE_DIR.fullmatch(parts[0]) and (full or incremental)):
        raise LedgerError(f"Unexpected physical backup layout: {path}")
    return root / parts[0]
