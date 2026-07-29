"""Template-driven tar archive backup."""

from __future__ import annotations

import fnmatch
import logging
import os
import tarfile
from collections.abc import Iterable
from pathlib import Path

from bdbackup.backends import BackupError, BackupResult
from bdbackup.templates import build_matcher, resolve_patterns


class FileBackup:
    """Create a tar archive from a list of paths supplied by a template file."""

    FORMATS = {
        "tar": ("w", ".tar", "r"),
        "tar.gz": ("w:gz", ".tar.gz", "r:gz"),
        "tar.zst": ("w:zstd", ".tar.zst", "r:zstd"),
    }

    def __init__(
        self,
        backup_dst: str | Path,
        template_filename: str | Path | None = None,
        chdir: str | Path | None = None,
        compression: bool = False,
        format: str = "tar",
        exclude: Iterable[str] | None = None,
        exclude_pattern: Iterable[str] | None = None,
        exclude_templates: Iterable[str] | None = None,
        follow_symlinks: bool = False,
    ):
        self.chdir = Path(chdir) if chdir else None
        self.template_filename = Path(template_filename) if template_filename else None
        self.backup_paths: list[Path] = []
        self.backup_dst = Path(backup_dst)
        self.compression = compression
        self.format = "tar.gz" if compression else format
        self.follow_symlinks = follow_symlinks
        # Relative exclude entries resolve against chdir (the source path),
        # never against the process working directory.
        self.exclude = {
            (Path(e) if Path(e).is_absolute() else self.chdir / Path(e)).resolve()
            if self.chdir
            else Path(e).resolve()
            for e in (exclude or [])
        }
        self.exclude_patterns = list(exclude_pattern or [])
        self.exclude_templates = list(exclude_templates or [])
        self._template_matcher = (
            build_matcher(resolve_patterns(self.exclude_templates))
            if self.exclude_templates
            else None
        )
        self.tar: tarfile.TarFile | None = None
        self.logger = logging.getLogger("bdbackup.file")

        if self.format not in self.FORMATS:
            raise ValueError(
                f"Unsupported format {self.format!r}; choose from {set(self.FORMATS)}"
            )
        self.tar_opt, self.tar_ext, self.tar_read_opt = self.FORMATS[self.format]
        self.tar_filename = Path(str(self.backup_dst) + self.tar_ext)

        if self.backup_dst.is_dir():
            raise IsADirectoryError(
                f"backup_dst must be a file path, got directory: {self.backup_dst}"
            )

        self.backup_dst.parent.mkdir(parents=True, exist_ok=True)

        if self.template_filename:
            self.load_template(self.template_filename)

    def load_template(self, template: str | Path) -> list[Path]:
        """Load paths from a template file."""
        template = Path(template)
        if not template.exists():
            raise FileNotFoundError(f"Template file not found: {template}")
        lines = [
            line.strip()
            for line in template.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.backup_paths = [Path(line) for line in lines]
        self.logger.info("Loaded %d paths from %s", len(self.backup_paths), template)
        return self.backup_paths

    def open_archive(self) -> tarfile.TarFile:
        """Open the destination tar archive."""
        try:
            self.tar = tarfile.open(self.tar_filename, self.tar_opt)
        except Exception as exc:  # pragma: no cover
            self.logger.error("Unable to open %s: %s", self.tar_filename, exc)
            raise
        return self.tar

    def close_archive(self) -> None:
        """Close the archive if it is open."""
        if self.tar:
            try:
                self.tar.close()
            except Exception as exc:  # pragma: no cover
                self.logger.error("Unable to close %s: %s", self.tar_filename, exc)
                raise
            finally:
                self.tar = None

    def _match_roots(self) -> list[Path]:
        """Roots that relative template patterns are matched against."""
        # Path() strips trailing slashes, which resolve() may keep on macOS.
        if self.chdir:
            return [Path(self.chdir.resolve())]
        return [Path(p.resolve()) for p in self.backup_paths]

    def _is_excluded(self, path: Path, is_dir: bool | None = None) -> bool:
        """Check whether a path matches an explicit exclude or a glob pattern."""
        resolved = path.resolve()
        if resolved in self.exclude:
            return True
        if any(resolved.is_relative_to(excluded) for excluded in self.exclude):
            return True
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(str(resolved), pattern) or fnmatch.fnmatch(path.name, pattern):
                return True
        if self._template_matcher is not None:
            if is_dir is None:
                is_dir = path.is_dir()
            if self._template_matcher.matches(resolved, is_dir, self._match_roots()):
                return True
        return False

    def _resolve_path(self, raw_path: Path) -> Path:
        """Resolve a template path against the configured base directory."""
        if self.chdir:
            return self.chdir / raw_path
        return raw_path

    @staticmethod
    def _make_arcname(path: Path, base: Path | None) -> str:
        """Produce a non-colliding archive member name.

        If a base directory is supplied, the returned name is the path relative
        to that base. Otherwise the raw path is preserved as-is (with a leading
        slash stripped to avoid tar warnings about absolute paths).
        """
        if base is not None:
            try:
                return str(path.resolve().relative_to(base.resolve()))
            except ValueError:
                pass
        return str(path).lstrip("/")

    def _walk(self) -> Iterable[tuple[Path, str]]:
        """Yield (path, arcname) pairs for all items to be archived."""
        for raw_path in self.backup_paths:
            path = self._resolve_path(raw_path)
            if self._is_excluded(path):
                self.logger.debug("Skipping excluded path: %s", path)
                continue
            if not path.exists():
                self.logger.warning("Backup path not found: %s", path)
                continue
            if path.is_dir():
                for root, _dirs, files in os.walk(path, followlinks=self.follow_symlinks):
                    root_path = Path(root)
                    for filename in files:
                        child = root_path / filename
                        if self._is_excluded(child, is_dir=False):
                            self.logger.debug("Skipping excluded path: %s", child)
                            continue
                        yield child, self._make_arcname(child, self.chdir)
            else:
                yield path, self._make_arcname(path, self.chdir)

    def backup(self, *, debug: bool = False, dry_run: bool = False) -> BackupResult:
        """Add every template path to the archive and verify it opens."""
        if not dry_run and not self.tar:
            self.open_archive()

        if not self.backup_paths:
            self.logger.warning("No information to backup. Is the template loaded?")
            return BackupResult(path=self.tar_filename, size_bytes=0, success=True)

        total_size = 0
        backed_up = 0
        for path, arcname in self._walk():
            if debug or dry_run:
                self.logger.info("Would back up: %s -> %s", path, arcname)
            if not dry_run:
                try:
                    self.tar.add(path, arcname=arcname)
                    backed_up += 1
                except Exception as exc:  # pragma: no cover
                    self.logger.error("Error adding %s: %s", path, exc)
            if path.exists():
                try:
                    total_size += path.stat().st_size
                except OSError:
                    pass

        self.logger.info("Backed up %d items to %s", backed_up, self.tar_filename)

        if not dry_run:
            self.close_archive()
            size_bytes = self.tar_filename.stat().st_size
        else:
            size_bytes = total_size
        return BackupResult(path=self.tar_filename, size_bytes=size_bytes, success=True)

    def verify(self, result: BackupResult | None = None) -> BackupResult:
        """Verify the produced archive can be opened and contains members."""
        archive = result.path if result else self.tar_filename
        if not archive.exists():
            raise BackupError(f"Archive not found for verification: {archive}")
        try:
            with tarfile.open(archive, self.tar_read_opt) as tar:
                members = tar.getmembers()
                if not members:
                    raise BackupError(f"Archive {archive} contains no members")
                self.logger.info("Verified archive %s with %d members", archive, len(members))
                return BackupResult(
                    path=archive,
                    size_bytes=archive.stat().st_size,
                    success=True,
                )
        except tarfile.TarError as exc:
            raise BackupError(f"Archive verification failed for {archive}: {exc}") from exc

    def prune(self) -> list[Path]:
        """FileBackup has no built-in retention policy; return an empty list."""
        return []

    def restore(self, extract_dir: str | Path, archive: str | Path | None = None) -> Path:
        """Extract the archive to *extract_dir*."""
        archive_path = Path(archive) if archive else self.tar_filename
        dest = Path(extract_dir)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, self.tar_read_opt) as tar:
            for member in tar.getmembers():
                # Only extract regular files and directories; avoid unsafe links.
                if member.isdev() or member.issym() or member.islnk():
                    self.logger.warning("Skipping unsafe tar member: %s", member.name)
                    continue
                tar.extract(member, path=dest)  # noqa: S202
        self.logger.info("Restored %s to %s", archive_path, dest)
        return dest

    def __enter__(self) -> FileBackup:
        if not self.tar:
            self.open_archive()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_archive()
