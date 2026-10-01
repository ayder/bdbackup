"""GFS stage configuration: keep ages, stages and the configuration rules (spec 2 §3.1)."""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bdbackup.config import Job
    from bdbackup.history import HistorySettings

PERIODS = ("daily", "weekly", "monthly", "yearly")
MARKER = ".bdbackup-destination"
_KEEP = re.compile(r"([1-9][0-9]*)([dwmy])")
_JOB_KEYS = {"type", "stage"}
_STAGE_KEYS = {"paths", "period", "keep"}
# (shortest, longest) days of one unit, so "longer on every calendar" is decidable.
_UNIT_DAYS = {"d": (1, 1), "w": (7, 7), "m": (28, 31), "y": (365, 366)}


@dataclass(frozen=True)
class Keep:
    """An age: a unit is within it while its backup day is after ``floor(today)``."""

    count: int
    unit: str

    @classmethod
    def parse(cls, text: object) -> Keep:
        match = _KEEP.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            raise ValueError("keep must be a positive whole number followed by d, w, m or y")
        return cls(int(match.group(1)), match.group(2))

    def floor(self, today: date) -> date:
        if self.unit == "d":
            return today - timedelta(days=self.count)
        if self.unit == "w":
            return today - timedelta(weeks=self.count)
        months = self.count * (12 if self.unit == "y" else 1)
        year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
        month += 1
        return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))

    @property
    def min_days(self) -> int:
        return self.count * _UNIT_DAYS[self.unit][0]

    @property
    def max_days(self) -> int:
        return self.count * _UNIT_DAYS[self.unit][1]

    def __str__(self) -> str:
        return f"{self.count}{self.unit}"


@dataclass(frozen=True)
class Stage:
    paths: tuple[Path, ...]
    period: str
    keep: Keep


def _stage(index: int, table: object, base: Path, previous: Stage | None) -> Stage:
    if not isinstance(table, dict):
        raise ValueError("stage must be a non-empty array of tables")
    for key in table:
        if key not in _STAGE_KEYS:
            raise ValueError(f"stage {index}: unknown key {key!r}")
    paths = table.get("paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(p, str) or not p.strip() for p in paths)
    ):
        raise ValueError(f"stage {index}: paths must be a non-empty array of path strings")
    if previous is None and len(paths) != 1:
        raise ValueError(f"stage {index}: the first stage has exactly one path")
    period = table.get("period")
    if period not in PERIODS:
        raise ValueError(f"stage {index}: period must be one of {', '.join(PERIODS)}")
    if previous is None and period != "daily":
        raise ValueError(f'stage {index}: the first stage\'s period must be "daily"')
    if previous is not None and PERIODS.index(period) < PERIODS.index(previous.period):
        raise ValueError(
            f'stage {index}: period "{period}" goes backwards after "{previous.period}"'
        )
    try:
        keep = Keep.parse(table.get("keep"))
    except ValueError as exc:
        raise ValueError(f"stage {index}: {exc}") from None
    if previous is not None and keep.min_days <= previous.keep.max_days:
        raise ValueError(
            f'stage {index}: keep "{keep}" must be longer than "{previous.keep}" '
            "on every calendar"
        )
    resolved = tuple((base / Path(p).expanduser()).resolve() for p in paths)
    return Stage(resolved, period, keep)


def parse_gfs_job(section: Mapping[str, object], base: Path) -> dict[str, object]:
    """Validate one gfs job table; errors omit the ``Job '<name>': `` prefix."""
    if set(section) - _JOB_KEYS:
        raise ValueError("a gfs job accepts only type, active and stage")
    tables = section.get("stage")
    if not isinstance(tables, list) or not tables:
        raise ValueError("stage must be a non-empty array of tables")
    stages: list[Stage] = []
    for index, table in enumerate(tables, 1):
        stages.append(_stage(index, table, base, stages[-1] if stages else None))
    return {"stage": tuple(stages)}


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _engine_output(job: Job) -> Path | None:
    key = {"xtrabackup": "backup_root", "mysqldump": "out_dir", "file": "backup_dst"}.get(job.type)
    value = job.params.get(key) if key else None
    return Path(value).expanduser().resolve() if value is not None else None


def check_gfs_config(jobs: Mapping[str, Job], history: HistorySettings | None) -> None:
    """Rules across jobs: stage paths, the history database and engine outputs."""
    gfs = [job for job in jobs.values() if job.type == "gfs"]
    all_paths = []
    for job in gfs:
        if history is None:
            raise ValueError(f"Job {job.name!r}: gfs jobs require a [history] section")
        for stage in job.params["stage"]:
            for path in stage.paths:
                if history.database.is_relative_to(path):
                    raise ValueError(
                        f"Job {job.name!r}: [history] database must not be inside stage path "
                        f"{path}"
                    )
                for other in all_paths:
                    if _overlap(path, other):
                        raise ValueError(
                            f"Job {job.name!r}: stage paths overlap: {other} and {path}"
                        )
                all_paths.append(path)
    for job in gfs:
        first = job.params["stage"][0].paths[0]
        later = [p for stage in job.params["stage"][1:] for p in stage.paths]
        for other in jobs.values():
            output = _engine_output(other)
            if output is None:
                continue
            for path in later:
                if output.is_relative_to(path):
                    raise ValueError(
                        f"Job {other.name!r} writes into stage path {path}; "
                        "engines write only into the first stage"
                    )
            if not output.is_relative_to(first):
                continue
            if other.type == "xtrabackup" and other.params.get("retention_days") != 0:
                raise ValueError(
                    f"Job {other.name!r} writes into first stage {first}: set retention_days = 0"
                )
            if other.type == "file" and other.params.get("timestamp") is not True:
                raise ValueError(
                    f"Job {other.name!r} writes into first stage {first}: set timestamp = true"
                )
