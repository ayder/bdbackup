"""Pure GFS target decisions (spec 2 r7 §2, §4.2): Unit values in, Decision values out."""

from datetime import date, timedelta

import pytest

from bdbackup.gfs.policy import Unit, decide
from bdbackup.gfs.stages import Keep, Stage


def stages(*specs):
    return [Stage((), period, Keep.parse(keep)) for keep, period in specs]


def days(first, last):
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    return [first + timedelta(n) for n in range((last - first).days + 1)]


def run(unit_days, stage_list, today, series=("s",), stage_of=None):
    units = [
        Unit((name, day), name, day, (stage_of or {}).get(day, 0))
        for name in series for day in unit_days
    ]
    return {(d.unit.series, d.unit.day): d for d in decide(units, stage_list, today)}


def check(by_day, day, action, target, reason, series="s"):
    key = (series, date.fromisoformat(day))
    assert key in by_day, f"no decision for {day}"
    decision = by_day[key]
    assert (decision.action, decision.target, decision.reason) == (action, target, reason)


SPEC_STAGES = stages(("5d", "daily"), ("20d", "daily"), ("8w", "weekly"), ("12m", "monthly"),
                     ("7y", "yearly"))


def test_worked_example():
    by_day = run(days("2019-01-01", "2026-09-29"), SPEC_STAGES, date(2026, 9, 29))
    check(by_day, "2026-09-24", "move", 1, "within 20d")
    check(by_day, "2026-09-13", "move", 1, "within 20d")
    check(by_day, "2026-09-09", "delete", None, "not newest of 2026-W37")
    check(by_day, "2026-09-06", "move", 2, "weekly 2026-W36")
    check(by_day, "2026-09-02", "delete", None, "not newest of 2026-W36")
    check(by_day, "2026-08-30", "move", 2, "weekly 2026-W35")
    check(by_day, "2026-08-02", "delete", None, "not newest of 2026-08")
    check(by_day, "2026-07-26", "move", 3, "monthly 2026-07")
    check(by_day, "2025-12-28", "move", 3, "monthly 2025-12")
    check(by_day, "2025-09-28", "delete", None, "not newest of 2025")
    check(by_day, "2024-12-29", "move", 4, "yearly 2024")
    check(by_day, "2026-09-29", "stay", 0, "newest of series")


def case_first_stage_only():
    by_day = run(days("2026-09-15", "2026-09-29"), stages(("5d", "daily")), date(2026, 9, 29))
    for day in days("2026-09-15", "2026-09-24"):
        check(by_day, day.isoformat(), "delete", None, "expired")
    for day in days("2026-09-25", "2026-09-28"):
        check(by_day, day.isoformat(), "stay", 0, "in place")
    check(by_day, "2026-09-29", "stay", 0, "newest of series")


def case_newest_stays_when_old():
    by_day = run([date(2026, 8, 1)], stages(("5d", "daily")), date(2026, 9, 29))
    check(by_day, "2026-08-01", "stay", 0, "newest of series")


def case_two_series():
    by_day = run(days("2026-09-07", "2026-09-20"), stages(("5d", "daily"), ("8w", "weekly")),
                 date(2026, 9, 29), series=("a", "b"))
    for name in ("a", "b"):
        check(by_day, "2026-09-13", "move", 1, "weekly 2026-W37", series=name)
        for day in days("2026-09-07", "2026-09-12"):
            check(by_day, day.isoformat(), "delete", None, "not newest of 2026-W37", series=name)


def case_weekly_then_yearly():
    by_day = run(days("2024-01-01", "2026-09-29"),
                 stages(("5d", "daily"), ("8w", "weekly"), ("7y", "yearly")), date(2026, 9, 29))
    check(by_day, "2024-12-29", "move", 2, "yearly 2024")
    check(by_day, "2025-12-28", "move", 2, "yearly 2025")
    check(by_day, "2025-06-29", "delete", None, "not newest of 2025")


def case_held_week():
    by_day = run(days("2026-09-26", "2026-09-30"), stages(("2d", "daily"), ("8w", "weekly")),
                 date(2026, 9, 30))
    check(by_day, "2026-09-28", "hold", 0, "held: 2026-W40 not complete")
    check(by_day, "2026-09-27", "move", 1, "weekly 2026-W39")


def case_month_waits_for_week():
    by_day = run(days("2026-09-20", "2026-10-02"), stages(("1d", "daily"), ("12m", "monthly")),
                 date(2026, 10, 2))
    check(by_day, "2026-09-27", "hold", 0, "held: 2026-09 not complete")


def case_no_backward():
    by_day = run([date(2026, 9, 10), date(2026, 9, 29)],
                 stages(("5d", "daily"), ("30d", "daily"), ("8w", "weekly")), date(2026, 9, 29),
                 stage_of={date(2026, 9, 10): 2})
    check(by_day, "2026-09-10", "stay", 2, "stays: config places it in an earlier stage")


def case_month_end_clamp():
    by_day = run([date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 31)],
                 stages(("1m", "daily"), ("12m", "monthly")), date(2026, 3, 31))
    check(by_day, "2026-03-01", "stay", 0, "in place")
    check(by_day, "2026-02-28", "move", 1, "monthly 2026-02")


def case_iso_week_53():
    by_day = run(days("2026-12-28", "2027-01-20"), stages(("5d", "daily"), ("8w", "weekly")),
                 date(2027, 1, 20))
    check(by_day, "2026-12-31", "delete", None, "not newest of 2026-W53")
    check(by_day, "2027-01-03", "move", 1, "weekly 2026-W53")


def case_skip_stages():
    by_day = run([date(2026, 8, 23), date(2026, 9, 29)],
                 stages(("5d", "daily"), ("20d", "daily"), ("8w", "weekly")), date(2026, 9, 29))
    check(by_day, "2026-08-23", "move", 2, "weekly 2026-W34")


CASES = {
    "first-stage-only": case_first_stage_only,
    "newest-stays-when-old": case_newest_stays_when_old,
    "two-series": case_two_series,
    "weekly-then-yearly": case_weekly_then_yearly,
    "held-week": case_held_week,
    "month-waits-for-week": case_month_waits_for_week,
    "no-backward": case_no_backward,
    "month-end-clamp": case_month_end_clamp,
    "iso-week-53": case_iso_week_53,
    "skip-stages": case_skip_stages,
}


@pytest.mark.parametrize("case", CASES)
def test_decision_rules(case):
    CASES[case]()
