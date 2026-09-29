"""GFS stage configuration: keep ages, stages and the configuration rules (spec 2 §3.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bdbackup.config import Job
    from bdbackup.history import HistorySettings


@dataclass(frozen=True)
class Keep:
    count: int
    unit: str

    @classmethod
    def parse(cls, text: object) -> Keep:
        raise ValueError("not implemented")

    def floor(self, today: date) -> date:
        return today


@dataclass(frozen=True)
class Stage:
    paths: tuple[Path, ...]
    period: str
    keep: Keep


def parse_gfs_job(section: Mapping[str, object], base: Path) -> dict[str, object]:
    return {k: v for k, v in section.items() if k not in {"type", "schedule"}}


def check_gfs_config(jobs: Mapping[str, Job], history: HistorySettings | None) -> None:
    return None
