"""Pure GFS target decisions (spec 2 r7 §2 and §4.2). No filesystem, no database."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from bdbackup.gfs.stages import Stage


@dataclass(frozen=True)
class Unit:
    id: object
    series: str
    day: date
    stage: int


@dataclass(frozen=True)
class Decision:
    unit: Unit
    action: str
    target: int | None
    reason: str


def decide(units: Sequence[Unit], stages: Sequence[Stage], today: date) -> list[Decision]:
    return []
