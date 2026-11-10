"""Read a pgBackRest repository: its info output, its info files and a full backup directory.

Pure reading. Nothing here writes to the repository or runs a subprocess.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bdbackup.backends import BackupError


class RepositoryError(BackupError):
    """Raised when a pgBackRest repository cannot be read safely."""


LABEL = re.compile(r"\d{8}-\d{6}F")


@dataclass(frozen=True)
class FullBackup:
    label: str
    stop: datetime
    pg_version: str
    page_errors: bool
    repo_bytes: int


@dataclass(frozen=True)
class InfoPair:
    relative: str
    main: bytes
    copy: bytes
    main_stat: os.stat_result
    copy_stat: os.stat_result


def newest_full(info: list[dict], stanza: str) -> FullBackup | None:
    raise NotImplementedError


def parse_info_file(text: str) -> dict[str, dict[str, object]]:
    raise NotImplementedError


def archive_copy(backup_info: dict, label: str) -> bool:
    raise NotImplementedError


def read_info_pair(
    repo: Path,
    relative: str,
    *,
    label: str | None = None,
    attempts: int = 5,
    wait: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> InfoPair:
    raise NotImplementedError


def full_entries(repo: Path, stanza: str, label: str) -> list[Path]:
    raise NotImplementedError
