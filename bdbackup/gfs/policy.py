"""Pure GFS target decisions (spec 2 r7 §2 and §4.2). No filesystem, no database.

Each unit's target is the first stage whose ``keep`` it is still within. A period stage only
accepts a unit eligible for its period: the newest unit of its ISO week; of those, the newest
of its month; of those, the newest of its year, per series. A bucket is decided only once it
is complete; a month (year) also waits for the ISO week holding its last day (r7, S1).
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

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
    action: str  # stay, move, delete, hold
    target: int | None
    reason: str


def _week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _bucket_label(day: date, period: str) -> str:
    if period == "weekly":
        return _week_label(day)
    if period == "monthly":
        return f"{day.year}-{day.month:02d}"
    return str(day.year)


def _eligibility(
    units: Sequence[Unit], today: date,
) -> tuple[dict[str, set[object]], Callable[[date, str], str | None]]:
    def week_done(day: date) -> bool:
        return day + timedelta(days=6 - day.weekday()) < today

    def month_done(year: int, month: int) -> bool:
        return week_done(date(year, month, calendar.monthrange(year, month)[1]))

    def year_done(year: int) -> bool:
        return week_done(date(year, 12, 31))

    def newest(group: list[Unit]) -> Unit:
        return max(group, key=lambda u: (u.day, str(u.id)))

    weeks: dict[tuple[int, int], list[Unit]] = defaultdict(list)
    for unit in units:
        weeks[unit.day.isocalendar()[:2]].append(unit)
    weekly = [newest(g) for g in weeks.values() if week_done(newest(g).day)]
    months: dict[tuple[int, int], list[Unit]] = defaultdict(list)
    for unit in weekly:
        months[(unit.day.year, unit.day.month)].append(unit)
    monthly = [newest(g) for (y, m), g in months.items() if month_done(y, m)]
    years: dict[int, list[Unit]] = defaultdict(list)
    for unit in monthly:
        years[unit.day.year].append(unit)
    yearly = [newest(g) for y, g in years.items() if year_done(y)]

    def incomplete(day: date, period: str) -> str | None:
        if not week_done(day):
            return _week_label(day)
        if period in ("monthly", "yearly") and not month_done(day.year, day.month):
            return f"{day.year}-{day.month:02d}"
        if period == "yearly" and not year_done(day.year):
            return str(day.year)
        return None

    sets = {
        "weekly": {u.id for u in weekly},
        "monthly": {u.id for u in monthly},
        "yearly": {u.id for u in yearly},
    }
    return sets, incomplete


def decide(units: Sequence[Unit], stages: Sequence[Stage], today: date) -> list[Decision]:
    """Return one decision per unit."""
    by_series: dict[str, list[Unit]] = defaultdict(list)
    for unit in units:
        by_series[unit.series].append(unit)
    floors = [stage.keep.floor(today) for stage in stages]
    decisions = []
    for members in by_series.values():
        eligible, incomplete = _eligibility(members, today)
        newest = max(members, key=lambda u: (u.day, str(u.id)))
        for unit in members:
            if unit is newest:
                decisions.append(Decision(unit, "stay", unit.stage, "newest of series"))
                continue
            target = next((i for i, floor in enumerate(floors) if unit.day > floor), None)
            if target is None:
                decisions.append(Decision(unit, "delete", None, "expired"))
                continue
            period = stages[target].period
            if period != "daily":
                pending = incomplete(unit.day, period)
                if pending:
                    decisions.append(
                        Decision(unit, "hold", unit.stage, f"held: {pending} not complete")
                    )
                    continue
                if unit.id not in eligible[period]:
                    label = _bucket_label(unit.day, period)
                    decisions.append(Decision(unit, "delete", None, f"not newest of {label}"))
                    continue
            if target < unit.stage:
                decisions.append(Decision(
                    unit, "stay", unit.stage, "stays: config places it in an earlier stage"
                ))
            elif target == unit.stage:
                decisions.append(Decision(unit, "stay", target, "in place"))
            else:
                reason = (f"within {stages[target].keep}" if period == "daily"
                          else f"{period} {_bucket_label(unit.day, period)}")
                decisions.append(Decision(unit, "move", target, reason))
    return decisions
