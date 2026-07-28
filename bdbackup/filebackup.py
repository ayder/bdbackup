"""Template-driven tar archive backup."""

from __future__ import annotations

import os
import tarfile
from collections.abc import Iterable
from pathlib import Path

from bdbackup.utils import setup_logging


class FileBackup:
    """Create a tar archive from a list of paths supplied by a template file."""

    def __init__(
        self,
        backup_dst: str | Path,
        template_filename: str | Path | None = None,
        chdir: str | Path | None = None,
        compression: bool = False,
        exclude: Iterable[str] | None = None,
    ):
        self.chdir = Path(chdir) if chdir else None
        self.template_filename = Path(template_filename) if template_filename else None
        self.backup_paths: list[Path] = []
        self.backup_dst = Path(backup_dst)
        self.compression = compression
        self.exclude = {Path(e).resolve() for e in (exclude or [])}
        self.tar: tarfile.TarFile | None = None
        self.logger = setup_logging("bdbackup.file")

        self.tar_opt = "w:gz" if compression else "w"
        ext = ".tar.gz" if compression else ".tar"
        self.tar_filename = Path(str(self.backup_dst) + ext)

        if self.backup_dst.is_dir():
            raise IsADirectoryError(
                f"backup_dst must be a file path, got directory: {self.backup_dst}"
            )

        self.backup_dst.parent.mkdir(parents=True, exist_ok=True)

        if self.template_filename:
            self.load_template(self.template_filename)
            self.open_archive()

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

    def _is_excluded(self, path: Path) -> bool:
        """Check whether a path (or any parent) is in the exclude set."""
        resolved = path.resolve()
        if resolved in self.exclude:
            return True
        return any(resolved.is_relative_to(excluded) for excluded in self.exclude)

    def backup(self, debug: bool = False) -> list[Path]:
        """Add every template path to the archive."""
        if not self.tar:
            self.open_archive()

        if not self.backup_paths:
            self.logger.warning("No information to backup. Is the template loaded?")
            return []

        if self.chdir:
            if not self.chdir.is_dir():
                raise NotADirectoryError(f"Unable to chdir to {self.chdir}")
            os.chdir(self.chdir)
            self.logger.info("Changed working directory to %s", self.chdir)

        backed_up: list[Path] = []
        for raw_path in self.backup_paths:
            path = raw_path
            if self._is_excluded(path):
                if debug:
                    self.logger.info("Skipping excluded path: %s", path)
                continue
            if path.exists():
                if debug:
                    self.logger.info("Backing up: %s", path)
                try:
                    self.tar.add(path, arcname=path.name)
                    backed_up.append(path)
                except Exception as exc:  # pragma: no cover
                    self.logger.error("Error adding %s: %s", path, exc)
            else:
                self.logger.warning("Backup path not found: %s", path)

        self.logger.info("Backed up %d items to %s", len(backed_up), self.tar_filename)
        return backed_up

    def __enter__(self) -> FileBackup:
        if not self.tar:
            self.open_archive()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_archive()
