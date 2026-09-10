"""Template-driven, atomically published tar archive backups."""

from __future__ import annotations

import fnmatch
import logging
import os
import stat
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from bdbackup.backends import BackupError, BackupResult
from bdbackup.templates import build_matcher, resolve_patterns
from bdbackup.utils import process_lock


class FileBackup:
    """Create a verified archive without replacing a good backup on failure."""

    FORMATS = {
        "tar": ("w", ".tar", "r"),
        "tar.gz": ("w:gz", ".tar.gz", "r:gz"),
        "tar.zst": ("w:zst", ".tar.zst", "r:zst"),
    }

    def __init__(
        self,
        backup_dst: str | Path,
        template_filename: str | Path | None = None,
        chdir: str | Path | None = None,
        format: str = "tar",
        exclude: Iterable[str] | None = None,
        exclude_pattern: Iterable[str] | None = None,
        exclude_templates: Iterable[str] | None = None,
        follow_symlinks: bool = False,
    ):
        self.chdir = Path(chdir).expanduser().absolute() if chdir else None
        self.template_filename = Path(template_filename).expanduser() if template_filename else None
        self.backup_paths: list[Path] = []
        self.backup_dst = Path(backup_dst).expanduser().absolute()
        self.format = format
        self.follow_symlinks = follow_symlinks
        base = self.chdir or Path.cwd()
        self.exclude = {(base / Path(e).expanduser()).resolve() for e in (exclude or [])}
        self.exclude_patterns = list(exclude_pattern or [])
        self.exclude_templates = list(exclude_templates or [])
        self._template_matcher = (
            build_matcher(resolve_patterns(self.exclude_templates))
            if self.exclude_templates
            else None
        )
        self.tar: tarfile.TarFile | None = None
        self._pending: Path | None = None
        self._lock = None
        self._expected: list[str] = []
        self.logger = logging.getLogger("bdbackup.file")
        if format not in self.FORMATS:
            raise ValueError(f"Unsupported format {format!r}; choose from {set(self.FORMATS)}")
        if format == "tar.zst" and sys.version_info < (3, 14):
            raise ValueError("tar.zst requires Python 3.14 or newer; use tar or tar.gz")
        self.tar_opt, self.tar_ext, self.tar_read_opt = self.FORMATS[format]
        self.tar_filename = Path(str(self.backup_dst) + self.tar_ext)
        self.lock_filename = Path(str(self.tar_filename) + ".lock")
        if self.backup_dst.is_dir():
            raise IsADirectoryError(f"backup_dst must be a file path: {self.backup_dst}")
        if self.template_filename:
            self.load_template(self.template_filename)

    def load_template(self, template: str | Path) -> list[Path]:
        template = Path(template).expanduser()
        self.backup_paths = [
            Path(line.strip()).expanduser()
            for line in template.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return self.backup_paths

    def open_archive(self) -> tarfile.TarFile:
        """Open a private staging archive under the destination's process lock."""
        if self.tar is not None:
            return self.tar
        lock = process_lock(self.lock_filename)
        lock.__enter__()
        self._lock = lock
        try:
            fd, name = tempfile.mkstemp(
                prefix=f".{self.tar_filename.name}.",
                suffix=".tmp",
                dir=self.tar_filename.parent,
            )
            os.close(fd)
            self._pending = Path(name)
            self._expected = []
            self.tar = tarfile.open(
                self._pending,
                self.tar_opt,
                dereference=self.follow_symlinks,
            )
            return self.tar
        except BaseException:
            self.close_archive(publish=False)
            raise

    def close_archive(self, *, publish: bool = True) -> None:
        """Close, verify and publish; on any error discard only staging output."""
        try:
            if self.tar is not None:
                self.tar.close()
                self.tar = None
            if self._pending is not None and publish:
                self.verify(BackupResult(self._pending))
                with tarfile.open(self._pending, "r:*") as archive:
                    if self._expected and archive.getnames() != self._expected:
                        raise BackupError("Archive members do not match the selected inputs")
                with self._pending.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(self._pending, self.tar_filename)
        finally:
            self.tar = None
            try:
                if self._pending is not None:
                    self._pending.unlink(missing_ok=True)
            finally:
                self._pending = None
                if self._lock is not None:
                    self._lock.__exit__(None, None, None)
                    self._lock = None

    def _match_roots(self) -> list[Path]:
        if self.chdir:
            return [self.chdir]
        return [self._resolve_path(p) for p in self.backup_paths]

    def _hard_excluded(self, path: Path) -> bool:
        if path in {self.tar_filename, self.lock_filename, self._pending}:
            return True
        resolved = path.resolve()
        internal = {self.tar_filename.resolve(), self.lock_filename.resolve()}
        if self._pending is not None:
            internal.add(self._pending.resolve())
        if resolved in internal:
            return True
        if any(resolved.is_relative_to(e) for e in self.exclude):
            return True
        roots = self._match_roots()
        candidates = [path]
        for parent in path.parents:
            if any(parent == root or parent.is_relative_to(root) for root in roots):
                candidates.append(parent)
        return any(
            fnmatch.fnmatch(str(candidate), pattern) or fnmatch.fnmatch(candidate.name, pattern)
            for candidate in candidates
            for pattern in self.exclude_patterns
        )

    def _is_excluded(self, path: Path, is_dir: bool | None = None) -> bool:
        if self._hard_excluded(path):
            return True
        return bool(
            self._template_matcher
            and self._template_matcher.matches(
                path,
                path.is_dir() if is_dir is None else is_dir,
                self._match_roots(),
            )
        )

    def _resolve_path(self, raw_path: Path) -> Path:
        return Path(os.path.abspath((self.chdir or Path.cwd()) / raw_path))

    @staticmethod
    def _make_arcname(path: Path, base: Path | None) -> str:
        # Keep the lexical name: resolving symlinks changes names and creates collisions.
        if base is not None:
            try:
                return path.relative_to(base).as_posix()
            except ValueError:
                pass
        return path.as_posix().lstrip("/")

    def _walk(self) -> Iterable[tuple[Path, str]]:
        emitted: set[str] = set()

        def visit(path: Path, ancestors: frozenset[tuple[int, int]], base: Path | None):
            if self._hard_excluded(path):
                return
            info = path.stat() if self.follow_symlinks else path.lstat()
            is_dir = stat.S_ISDIR(info.st_mode)
            if not (is_dir or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise BackupError(f"Unsupported special input file: {path}")
            excluded = self._is_excluded(path, is_dir)
            arcname = self._make_arcname(path, base)
            if not excluded and arcname not in emitted:
                emitted.add(arcname)
                yield path, arcname
            if not is_dir:
                return
            if excluded and not (self._template_matcher and self._template_matcher.has_negations):
                return
            inode = (info.st_dev, info.st_ino)
            if inode in ancestors:
                raise BackupError(f"Symlink directory cycle: {path}")
            with os.scandir(path) as entries:
                children = sorted(entry.name for entry in entries)
            for name in children:
                yield from visit(path / name, ancestors | {inode}, base)

        for raw_path in self.backup_paths:
            base = self.chdir or (None if raw_path.is_absolute() else Path.cwd())
            yield from visit(self._resolve_path(raw_path), frozenset(), base)

    def backup(self, *, debug: bool = False, dry_run: bool = False) -> BackupResult:
        if not self.backup_paths:
            self.close_archive(publish=False)
            raise BackupError("No backup paths selected; the template is empty")
        total_size = count = 0
        try:
            if not dry_run:
                self.open_archive()
            for path, arcname in self._walk():
                if debug or dry_run:
                    self.logger.info("Would back up: %s -> %s", path, arcname)
                before = path.stat() if self.follow_symlinks else path.lstat()
                if not dry_run:
                    self.tar.add(path, arcname=arcname, recursive=False)
                    after = path.stat() if self.follow_symlinks else path.lstat()
                    if stat.S_ISREG(before.st_mode) and (
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ino,
                    ) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise BackupError(f"Input changed during backup: {path}")
                    self._expected.append(arcname)
                count += 1
                total_size += before.st_size if stat.S_ISREG(before.st_mode) else 0
            if not count:
                raise BackupError("No archive members selected after exclusions")
            if not dry_run:
                self.close_archive()
        except BaseException:
            self.close_archive(publish=False)
            raise
        return BackupResult(
            self.tar_filename,
            size_bytes=total_size if dry_run else self.tar_filename.stat().st_size,
            success=True,
        )

    def verify(self, result: BackupResult | None = None) -> BackupResult:
        archive = result.path if result else self.tar_filename
        try:
            with tarfile.open(archive, "r:*") as tar:
                count = 0
                for member in tar:
                    count += 1
                    if member.isfile():
                        with tar.extractfile(member) as stream:
                            while stream.read(1024 * 1024):
                                pass
                if not count:
                    raise BackupError(f"Archive {archive} contains no members")
                # Consume the compressed trailer too, so gzip/zstd corruption is surfaced.
                while tar.fileobj.read(1024 * 1024):
                    pass
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise BackupError(f"Archive verification failed for {archive}: {exc}") from exc
        return BackupResult(archive, size_bytes=archive.stat().st_size, success=True)

    def prune(self) -> list[Path]:
        return []

    @staticmethod
    def _restore_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
        if PurePosixPath(member.name).is_absolute() or ".." in PurePosixPath(member.name).parts:
            raise BackupError(f"Unsafe archive member path: {member.name}")
        filtered = tarfile.data_filter(member, destination)
        # Preserve ordinary directory permissions; discard special permission bits.
        if filtered.isdir():
            filtered.mode = member.mode & 0o777
        return filtered

    @classmethod
    def restore_archive(cls, archive: str | Path, extract_dir: str | Path) -> Path:
        """Restore safe files, directories and contained links on every supported Python."""
        dest = Path(extract_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(Path(archive).expanduser(), "r:*") as tar:
                # extractall restores directory metadata after its children.
                tar.extractall(dest, filter=cls._restore_filter)  # noqa: S202
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise BackupError(f"Restore failed: {exc}") from exc
        return dest

    def restore(self, extract_dir: str | Path, archive: str | Path | None = None) -> Path:
        return self.restore_archive(archive or self.tar_filename, extract_dir)

    def __enter__(self) -> FileBackup:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_archive(publish=exc_type is None)
