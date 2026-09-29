"""GFS state in the history database: managed units, their locations and every step."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bdbackup.history import BackupRecord, History


@dataclass(frozen=True)
class Location:
    stage_path: Path
    path: Path


@dataclass(frozen=True)
class ManagedUnit:
    id: int | None
    unit: Path
    series: str
    relative: Path
    kind: str
    time: datetime
    members: tuple[BackupRecord, ...]
    locations: tuple[Location, ...]
    size: int


def load(history: History, first_stage: Path) -> tuple[list[ManagedUnit], int]:
    return [], 0


def record_unit(history: History, unit: ManagedUnit) -> int:
    return 0


def record_move(history: History, unit_id: int, new: Sequence[Location],
                removed: Sequence[Location]) -> None:
    return None


def record_delete(history: History, unit_id: int) -> None:
    return None


def record_step(history: History, gfs_job: str, unit_id: int | None, action: str,
                source: str | None, destination: str | None, outcome: str,
                reason: str | None) -> None:
    return None
