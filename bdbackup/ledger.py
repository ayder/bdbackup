"""Ledger facts recorded for every backup: the unit GFS moves and its checksum."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from bdbackup.backends import BackupError


class LedgerError(BackupError):
    """A backup's unit or checksum cannot be determined."""


def checksum(path: Path) -> str:
    return ""


def unit_path(path: Path, restore_info: Mapping[str, str]) -> Path:
    return path
