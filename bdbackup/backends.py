"""Backend abstractions for bdbackup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class BackupResult:
    """Outcome of a backup run."""

    path: Path
    size_bytes: int = 0
    duration_seconds: float = 0.0
    success: bool = False


class BackupError(Exception):
    """Raised when a backup or verification fails."""


@runtime_checkable
class BackupBackend(Protocol):
    """Contract for a backup implementation."""

    def backup(self, name: str | None = None) -> BackupResult:
        """Run the backup and return a result object."""
        ...

    def verify(self, result: BackupResult | None = None) -> BackupResult:
        """Verify a previously produced backup."""
        ...

    def prune(self) -> list[Path]:
        """Remove old backups according to the backend's retention policy."""
        ...


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root bdbackup logger exactly once.

    Module-level loggers are left without handlers; they propagate to this root.
    """
    root = logging.getLogger("bdbackup")
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.NOTSET)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
