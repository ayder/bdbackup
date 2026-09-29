"""GFS filesystem actions: contents check, verified copy and removal (spec 2 §4.3, §4.4).

Copies go through a ``Transport`` so that object storage can be added later (spec 2 §5).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from bdbackup.gfs.store import ManagedUnit
from bdbackup.ledger import LedgerError, checksum

_CHUNK = 1024 * 1024


class Refusal(Exception):
    """GFS will not act on this unit now; the reason is reported and recorded."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Transport(Protocol):
    def copy(self, source: Path, temp: Path, on_file: Callable[[str, str], None]) -> None: ...
    def rename(self, temp: Path, final: Path) -> None: ...
    def remove(self, path: Path) -> None: ...
    def exists(self, path: Path) -> bool: ...


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalTransport:
    """Local or mounted directories."""

    def _copy_file(self, source: Path, target: Path) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as reader, target.open("xb") as writer:
            while chunk := reader.read(_CHUNK):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copymode(source, target)
        return digest.hexdigest()

    def copy(self, source: Path, temp: Path, on_file: Callable[[str, str], None]) -> None:
        """Copy a file or tree; report each regular file's relative path and SHA-256."""
        if stat.S_ISREG(source.lstat().st_mode):
            on_file(".", self._copy_file(source, temp))
            return
        temp.mkdir()
        for directory, names, files in os.walk(source):
            here = Path(directory)
            target_dir = temp / here.relative_to(source)
            for name in sorted(names + files):
                entry, target = here / name, target_dir / name
                mode = entry.lstat().st_mode
                if stat.S_ISLNK(mode):
                    os.symlink(os.readlink(entry), target)
                elif stat.S_ISDIR(mode):
                    target.mkdir()
                elif stat.S_ISREG(mode):
                    relative = entry.relative_to(source).as_posix()
                    on_file(relative, self._copy_file(entry, target))
                else:
                    raise Refusal(f"unexpected entry {entry.relative_to(source).as_posix()}")
            _fsync_dir(target_dir)

    def rename(self, temp: Path, final: Path) -> None:
        if os.path.lexists(final):
            raise Refusal(f"final name exists: {final}")
        os.rename(temp, final)
        _fsync_dir(final.parent)

    def remove(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif os.path.lexists(path):
            path.unlink()
        if path.parent.exists():
            _fsync_dir(path.parent)

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)


def _member_paths(unit: ManagedUnit) -> list[str]:
    return [Path(m.path).relative_to(unit.unit).as_posix() for m in unit.members]


def check_contents(unit: ManagedUnit, at: Path) -> None:
    """A unit holds its members and its engine's markers, nothing else (spec 2 D12)."""
    if not os.path.lexists(at):
        raise Refusal(f"missing location {at}")
    if unit.kind != "xtrabackup":
        if not stat.S_ISREG(at.lstat().st_mode):
            raise Refusal(f"unexpected entry {at.name}")
        return
    members = set(_member_paths(unit))
    fulls = {m for m in members if "/" not in m}
    for directory, names, files in os.walk(at):
        here = Path(directory)
        for name in sorted(names + files):
            entry = here / name
            relative = entry.relative_to(at).as_posix()
            if any(relative == m or relative.startswith(m + "/") for m in members):
                continue
            parts = relative.split("/")
            if relative == ".full_success" and entry.is_file() and not entry.is_symlink():
                continue
            if relative == "Full_Latest" and entry.is_symlink() and os.readlink(entry) in fulls:
                continue
            if (
                entry.is_dir() and not entry.is_symlink() and parts[0] == "Incremental"
                and (len(parts) == 1 or (len(parts) == 2 and parts[1].startswith("Full_")))
            ):
                continue
            raise Refusal(f"unexpected entry {relative}")
        names[:] = [n for n in names
                    if (here / n).relative_to(at).as_posix() not in members]


def verify(unit: ManagedUnit, at: Path) -> None:
    """Recompute every member's checksum at a location and compare with the ledger."""
    check_contents(unit, at)
    for member, relative in zip(unit.members, _member_paths(unit), strict=True):
        try:
            actual = checksum(at / relative if relative != "." else at)
        except LedgerError as exc:
            raise Refusal(f"unexpected entry {exc}") from None
        except OSError:
            raise Refusal(f"missing location {at / relative}") from None
        if actual != member.checksum:
            raise Refusal("checksum mismatch")


def _manifest(digests: dict[str, str], member: str) -> str:
    if member == ".":
        return digests["."]
    prefix = member + "/"
    lines = sorted(
        ((name[len(prefix):].encode(), f"{digest}  {name[len(prefix):]}\n")
         for name, digest in digests.items() if name.startswith(prefix)),
        key=lambda item: item[0],
    )
    return hashlib.sha256("".join(line for _, line in lines).encode()).hexdigest()


def copy_verified(unit: ManagedUnit, source: Path, final: Path, transport: Transport) -> Path:
    """Copy to a temporary name next to ``final``; the source is hashed while it is read."""
    check_contents(unit, source)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.parent / f".gfs-tmp-{uuid4().hex}-{final.name}"
    digests: dict[str, str] = {}
    try:
        transport.copy(source, temp, lambda name, digest: digests.__setitem__(name, digest))
        for member, relative in zip(unit.members, _member_paths(unit), strict=True):
            if _manifest(digests, relative) != member.checksum:
                raise Refusal("checksum mismatch")
    except BaseException:
        if transport.exists(temp):
            transport.remove(temp)
        raise
    return temp
