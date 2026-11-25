"""Export one completed PBM backup from filesystem storage."""

from __future__ import annotations

import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from bdbackup import tar_export
from bdbackup.backends import BackupError, BackupResult

_STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z"
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def export_unit(
    storage: Path, out_dir: Path, job: str, name: str, nodes: tuple[str, ...], verify: bool,
) -> BackupResult:
    try:
        storage, out_dir = storage.resolve(), out_dir.resolve()
        if out_dir.is_relative_to(storage) or storage.is_relative_to(out_dir):
            raise BackupError("out_dir and storage_path must not overlap")

        metadata = f"{name}.pbm.json"
        try:
            root = os.open(storage, _READ | os.O_DIRECTORY)
        except (FileNotFoundError, NotADirectoryError):
            raise _missing(storage, name, nodes) from None
        try:
            try:
                meta_stat = os.stat(metadata, dir_fd=root, follow_symlinks=False)
                tree_stat = os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                raise _missing(storage, name, nodes) from None
            if not (stat.S_ISREG(meta_stat.st_mode) and stat.S_ISDIR(tree_stat.st_mode)):
                raise _missing(storage, name, nodes)

            out_dir.mkdir(parents=True, exist_ok=True)
            pattern = re.compile(re.escape(f".{job}-") + _STAMP + r"\.tar\.tmp-[0-9a-f]{32}")
            tar_export.remove_stale_temps(out_dir, pattern)
            stamp = name.replace(":", "")
            final = out_dir / f"{job}-{stamp}.tar"
            if os.path.lexists(final):
                raise BackupError(f"unit already exists: {final}")
            return _write_unit(root, ((metadata, meta_stat), (name, tree_stat)), final, verify)
        finally:
            os.close(root)
    except OSError as exc:
        raise BackupError(f"cannot export {name} to {out_dir}: {exc}") from exc


def _missing(storage: Path, name: str, nodes: tuple[str, ...]) -> BackupError:
    return BackupError(
        f"PBM took backup {name} on {', '.join(nodes)}, but its files are not in "
        f'{storage} on this host; see docs/README_PBM.md, "Where the storage is"'
    )


def _write_unit(
    storage: int, entries: tuple[tuple[str, os.stat_result], ...], final: Path, verify: bool,
) -> BackupResult:
    temp = final.with_name(f".{final.name}.tmp-{uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    writer = tar_export.HashingWriter(fd)
    linked = False
    try:
        names: list[str] = []
        tar = tarfile.open(fileobj=writer, mode="w|", bufsize=tar_export.CHUNK)
        for name, state in entries:
            _add_entry(tar, storage, PurePosixPath(name), state, names)
        tar.close()
        os.fsync(fd)
        closing, fd = fd, None
        os.close(closing)
        digest = writer.sha256.hexdigest()
        if verify:
            tar_export.read_back(temp, final, digest, names)
        # Like mysqldump, publish without a check-then-rename race at the final name.
        try:
            os.link(temp, final)
        except FileExistsError:
            raise BackupError(f"unit already exists: {final}") from None
        linked = True
        temp.unlink()
        tar_export.fsync_dir(final.parent)
    except BaseException:
        writer.discard()
        if fd is not None:
            os.close(fd)
        if linked:
            final.unlink(missing_ok=True)
        temp.unlink(missing_ok=True)
        raise
    return BackupResult(final, size_bytes=writer.size, success=True, checksum=digest)


def _signature(state: os.stat_result) -> tuple:
    return (
        state.st_dev, state.st_ino, state.st_mode, state.st_size, state.st_mtime_ns,
        state.st_ctime_ns,
    )


def _add_entry(
    tar: tarfile.TarFile, parent: int, relative: PurePosixPath, before: os.stat_result,
    names: list[str],
) -> None:
    directory = stat.S_ISDIR(before.st_mode)
    if not directory and not stat.S_ISREG(before.st_mode):
        raise BackupError(f"unsupported entry in PBM storage: {relative}")
    # Anchor every open to its already-open parent and refuse symlinks before reading bytes.
    fd = os.open(relative.name, _READ | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
    try:
        if _signature(os.fstat(fd)) != _signature(before):
            raise BackupError(f"PBM input changed during export: {relative}")
        info = tarfile.TarInfo(relative.as_posix())
        info.mode = stat.S_IMODE(before.st_mode)
        info.mtime = before.st_mtime
        info.uid, info.gid = before.st_uid, before.st_gid
        names.append(info.name)
        if directory:
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
            with os.scandir(fd) as scan:
                children = sorted(entry.name for entry in scan)
            for entry in children:
                state = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                _add_entry(tar, fd, relative / entry, state, names)
        else:
            info.size = before.st_size
            with os.fdopen(fd, "rb", closefd=False) as source:
                tar.addfile(info, source)
        after = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        if _signature(after) != _signature(before):
            raise BackupError(f"PBM input changed during export: {relative}")
    finally:
        os.close(fd)
