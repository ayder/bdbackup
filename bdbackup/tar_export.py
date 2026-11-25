"""File and tar helpers for exporting a backup as one verified unit."""

import hashlib
import os
import re
import tarfile
from pathlib import Path

from bdbackup.backends import BackupError

CHUNK = 1024 * 1024  # Tar stream buffer for fewer, larger reads and writes over NFS.


def remove_stale_temps(directory: Path, pattern: re.Pattern[str]) -> None:
    """Remove only regular temporary files whose names match this export's pattern."""
    with os.scandir(directory) as scan:
        stale = [
            Path(entry.path)
            for entry in scan
            if pattern.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
        ]
    for path in stale:
        path.unlink(missing_ok=True)


def read_back(path: Path, unit: Path, checksum: str, written: list[str], *, opener=open) -> None:
    """Read the tar at ``path`` once: walk its members and re-hash every byte.

    ``unit`` is the name the tar is published under; a failure names it, not ``path``.
    """
    try:
        with opener(path, "rb") as stream:
            reader = HashingReader(stream)
            names = []
            with tarfile.open(fileobj=reader, mode="r|", bufsize=CHUNK) as tar:
                for member in tar:
                    names.append(member.name)
                    if member.isfile():
                        with tar.extractfile(member) as data:
                            while data.read(CHUNK):
                                pass
            # The zero blocks and padding after the last member are part of the digest.
            while reader.read(CHUNK):
                pass
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"verification of {unit} failed: {exc}") from exc
    digest = reader.sha256.hexdigest()
    if digest != checksum:
        raise BackupError(
            f"verification of {unit} failed: checksum {digest} read back, {checksum} written"
        )
    if names != written:
        raise BackupError(f"verification of {unit} failed: members differ from those written")


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class HashingWriter:
    """A write-only file object over ``fd`` that feeds every byte written to SHA-256."""

    def __init__(self, fd: int):
        self.fd = fd
        self.sha256 = hashlib.sha256()
        self.size = 0
        self.discarding = False

    def discard(self) -> None:
        """Drop every later write: the export failed and ``fd`` is about to be closed."""
        self.discarding = True

    def write(self, data) -> int:
        if self.discarding:
            return len(data)
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            self.sha256.update(view[:count])
            self.size += count
            view = view[count:]
        return len(data)


class HashingReader:
    """A read-only file object that feeds every byte read to SHA-256."""

    def __init__(self, stream):
        self.stream = stream
        self.sha256 = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.sha256.update(data)
        self.size += len(data)
        return data
