"""GFS filesystem actions: contents check, verified copy, rename and removal (spec 2 §4.3, §4.4).

Copies go through a ``Transport`` so that object storage can be added later (spec 2 §5).
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from bdbackup.gfs.store import ManagedUnit
from bdbackup.ledger import LedgerError, checksum

_CHUNK = 1024 * 1024


# The classes of a refusal (spec 16 section 6.1), fixed: metrics never parses the reason text.
REFUSAL_CLASSES = (
    "checksum_mismatch", "unexpected_entry", "missing_location", "stage_not_configured",
    "destination_not_ready", "final_name_exists", "other",
)


class Refusal(Exception):
    """GFS will not act on this unit now; the reason is reported and recorded.

    ``reason_class`` is one of ``REFUSAL_CLASSES``; a refusal that names none is ``other``.
    """

    def __init__(self, reason: str, reason_class: str = "other"):
        super().__init__(reason)
        self.reason = reason
        self.reason_class = reason_class


def final_name_exists(final: Path) -> Refusal:
    """The refusal when a unit's final name is taken; the docs say how to clear it (D7)."""
    return Refusal(
        f'final name exists: {final}; see docs/README_GFS.md, "Final name exists"',
        "final_name_exists",
    )


class Transport(Protocol):
    def copy(self, source: Path, temp: Path, on_file: Callable[[str, str], None]) -> None: ...
    def same_filesystem(self, a: Path, b: Path) -> bool: ...
    def relocate(self, source: Path, final: Path) -> None: ...
    def rename(self, temp: Path, final: Path) -> None: ...
    def replace(self, temp: Path, final: Path) -> None: ...
    def remove(self, path: Path) -> None: ...
    def exists(self, path: Path) -> bool: ...


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalTransport:
    """Local or mounted directories.

    A share that maps every user to one account refuses chmod with EPERM. The copy then goes on
    with whatever permission bits the share gave it, and ``modes_refused`` counts the refusals
    for the run's report. Any other error from those calls still fails the copy.
    """

    modes_refused: int = 0

    @contextmanager
    def _mode_may_be_refused(self) -> Iterator[None]:
        try:
            yield
        except PermissionError as exc:
            if exc.errno != errno.EPERM:
                raise
            self.modes_refused += 1

    def _copy_file(self, source: Path, target: Path) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as reader, target.open("xb") as writer:
            while chunk := reader.read(_CHUNK):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        with self._mode_may_be_refused():
            shutil.copymode(source, target)
        return digest.hexdigest()

    def copy(self, source: Path, temp: Path, on_file: Callable[[str, str], None]) -> None:
        """Copy a file or tree; report each regular file's relative path and SHA-256.

        Directories are created private and receive the source's permission bits once their
        contents are copied, bottom-up, so a read-only source directory never blocks the copy.
        """
        if stat.S_ISREG(source.lstat().st_mode):
            on_file(".", self._copy_file(source, temp))
            return
        temp.mkdir(mode=0o700)
        directories: list[tuple[Path, Path]] = []
        for directory, names, files in os.walk(source):
            here = Path(directory)
            target_dir = temp / here.relative_to(source)
            directories.append((here, target_dir))
            for name in sorted(names + files):
                entry, target = here / name, target_dir / name
                mode = entry.lstat().st_mode
                if stat.S_ISLNK(mode):
                    os.symlink(os.readlink(entry), target)
                elif stat.S_ISDIR(mode):
                    target.mkdir(mode=0o700)
                elif stat.S_ISREG(mode):
                    relative = entry.relative_to(source).as_posix()
                    on_file(relative, self._copy_file(entry, target))
                else:
                    raise Refusal(
                        f"unexpected entry {entry.relative_to(source).as_posix()}",
                        "unexpected_entry",
                    )
            _fsync_dir(target_dir)
        for here, target_dir in reversed(directories):
            with self._mode_may_be_refused():
                os.chmod(target_dir, stat.S_IMODE(here.lstat().st_mode))

    def same_filesystem(self, a: Path, b: Path) -> bool:
        """Whether two live paths are on one device; a path that cannot be examined is not."""
        try:
            return os.stat(a).st_dev == os.stat(b).st_dev
        except OSError:
            return False

    def relocate(self, source: Path, final: Path) -> None:
        """Rename a unit to its final name on one filesystem, reading none of its data.

        ``os.rename`` silently replaces a file or an empty directory, so a taken name is refused
        first. It fails with EXDEV when the two paths are on different mounts of one device;
        the caller then copies.
        """
        final.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(final):
            raise final_name_exists(final)
        os.rename(source, final)
        _fsync_dir(final.parent)
        if source.parent != final.parent:
            _fsync_dir(source.parent)

    def rename(self, temp: Path, final: Path) -> None:
        if os.path.lexists(final):
            raise final_name_exists(final)
        os.rename(temp, final)
        _fsync_dir(final.parent)

    def replace(self, temp: Path, final: Path) -> None:
        """Rename over an existing final name; only ledger snapshots are replaced."""
        os.replace(temp, final)
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
    """A unit holds its members and its engine's markers, nothing else (spec 2 D12).

    Inside a member only regular files and directories are allowed (spec 2 §2).
    """
    if not os.path.lexists(at):
        raise Refusal(f"missing location {at}", "missing_location")
    if unit.kind != "xtrabackup":
        if not stat.S_ISREG(at.lstat().st_mode):
            raise Refusal(f"unexpected entry {at.name}", "unexpected_entry")
        return
    members = set(_member_paths(unit))
    fulls = {m for m in members if "/" not in m}
    for directory, names, files in os.walk(at):
        here = Path(directory)
        for name in sorted(names + files):
            entry = here / name
            relative = entry.relative_to(at).as_posix()
            if any(relative == m or relative.startswith(m + "/") for m in members):
                mode = entry.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise Refusal(f"unexpected entry {relative}", "unexpected_entry")
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
            raise Refusal(f"unexpected entry {relative}", "unexpected_entry")


def verify(unit: ManagedUnit, at: Path, *, checksums: bool = True) -> None:
    """Check a location against the ledger: its contents, then every member's checksum.

    The contents check reads metadata only and always runs; ``checksums=False`` skips the
    read of the data (a stage with ``verify = false``).
    """
    check_contents(unit, at)
    if not checksums:
        return
    for member, relative in zip(unit.members, _member_paths(unit), strict=True):
        try:
            actual = checksum(at / relative if relative != "." else at)
        except LedgerError as exc:
            raise Refusal(f"unexpected entry {exc}", "unexpected_entry") from None
        except OSError:
            raise Refusal(f"missing location {at / relative}", "missing_location") from None
        if actual != member.checksum:
            raise Refusal("checksum mismatch", "checksum_mismatch")


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
                raise Refusal("checksum mismatch", "checksum_mismatch")
    except BaseException:
        if transport.exists(temp):
            transport.remove(temp)
        raise
    return temp
