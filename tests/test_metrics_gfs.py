"""The GFS families of 0.7.4 that ``metrics`` computes from the history database (spec 16).

Coverage, reach and size per source job, the last move per stage pair, the unmanaged records,
the stage's verify flag and the schema version (tier A). Dates are local: every test here runs
with ``TZ=UTC``, and the metrics run's "today" is injected as ``now``.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bdbackup.config import Config
from bdbackup.engines import _reset_for_tests
from bdbackup.gfs import store as gfs_store
from bdbackup.history import History
from bdbackup.metrics.collector import DESTINATION, Results
from bdbackup.metrics.run import collect_all
from tests.test_gfs_run import Env
from tests.test_metrics_collect import epoch, in_process_probe, sql, touch_database, values

TODAY = datetime(2026, 11, 18, 2, 30, tzinfo=UTC)
JOB = "gfs-main"
SOURCE = ("job_name", "stage", "source_job")


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture(autouse=True)
def utc():
    """Run in UTC, so a unit's local day and the injected "today" are the same everywhere."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def collect(env, now=TODAY):
    config = Config(env.config_path)
    return collect_all(
        config.jobs, History(config.history), now=now, timeout_seconds=5, probe=in_process_probe
    )


def series(collected, family):
    """``(stage, source job) -> value`` of one per-source family of the gfs job."""
    return {
        (stage, source): value
        for (job, stage, source), value in values(collected, family, *SOURCE).items()
        if job == JOB
    }


def coverage(collected, stage="1", source="files"):
    """``(expected, filled, missing, newest missing)`` of a stage and source; ``None`` if absent."""
    names = ("expected_slots", "filled_slots", "missing_slots", "newest_missing_timestamp_seconds")
    return tuple(
        series(collected, f"bdbackup_gfs_coverage_{name}").get((stage, source)) for name in names
    )


def days(first: date, last: date, *, skip=()) -> list[date]:
    return [first + timedelta(n) for n in range((last - first).days + 1)
            if first + timedelta(n) not in skip]


def units_on(env, days_, series_="files"):
    for day in days_:
        env.file_unit(f"{series_}-{day}.tar.gz", day=day, series=series_)


def midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


# ---- coverage against policy (spec 4.1) --------------------------------------------------------

D = date
SINGLE_DAILY = [(["BACKUP"], "daily", "14d")]
# The worked example of the spec: one daily stage of 14 days, today 2026-11-18. The units from
# 11-03 and 11-04 are older than the window; they only show that the install is not new.
EXAMPLE = [
    pytest.param(
        datetime(2026, 11, 18, 0, 30, tzinfo=UTC), days(D(2026, 11, 3), D(2026, 11, 17)),
        (13, 13, 0, None), id="00:30-newest-unit-is-11-17",
    ),
    pytest.param(
        datetime(2026, 11, 18, 2, 30, tzinfo=UTC), days(D(2026, 11, 3), D(2026, 11, 18)),
        (14, 14, 0, None), id="02:30-after-the-backup",
    ),
    pytest.param(
        datetime(2026, 11, 18, 2, 30, tzinfo=UTC),
        days(D(2026, 11, 3), D(2026, 11, 18), skip=[D(2026, 11, 10)]),
        (14, 13, 1, midnight(D(2026, 11, 10))), id="the-11-10-backup-failed",
    ),
    pytest.param(
        datetime(2026, 11, 18, 2, 30, tzinfo=UTC), days(D(2026, 11, 16), D(2026, 11, 18)),
        (3, 3, 0, None), id="the-first-backup-was-11-16",
    ),
]


@pytest.mark.parametrize(("now", "unit_days", "expected"), EXAMPLE)
def test_the_worked_example_of_the_spec(env, now, unit_days, expected):
    env.write(SINGLE_DAILY)
    units_on(env, unit_days)

    collected = collect(env, now)

    assert coverage(collected) == expected


def test_a_weekly_stage_counts_only_the_weeks_wholly_inside_its_window(env):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/weekly"], "weekly", "8w")])
    # Stage 2 holds the days after 09-23 (today - 8w) up to 11-13 (today - 5d). The ISO week
    # 09-21..09-27 starts before the window and 11-09..11-15 ends after it: neither counts.
    # Nothing falls in the week 10-12..10-18.
    units_on(env, days(D(2026, 9, 1), D(2026, 11, 18), skip=days(D(2026, 10, 12), D(2026, 10, 18))))

    collected = collect(env)

    assert coverage(collected, "2") == (6, 5, 1, midnight(D(2026, 10, 12)))
    assert coverage(collected, "1") == (5, 5, 0, None)  # 11-14 to 11-18, one day each


def test_a_unit_still_waiting_in_an_earlier_stage_fills_its_slot(env):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/weekly"], "weekly", "8w")])
    env.file_unit("a.tar.gz", day=D(2026, 10, 20))  # GFS has not run: it is in stage 1
    assert env.query("SELECT count(*) FROM gfs_units") == [(0,)]

    collected = collect(env)

    # The weeks before its own (10-19..10-25) end before the oldest unit; 10-26 and 11-02 are
    # empty. Stage 1 has no slot: every day of its window starts after the newest unit.
    assert coverage(collected, "2") == (3, 1, 2, midnight(D(2026, 11, 2)))
    assert coverage(collected, "1") == (0, 0, 0, None)


def test_deleted_units_still_set_where_a_new_install_starts(env):
    env.write(SINGLE_DAILY)
    units_on(env, days(D(2026, 11, 3), D(2026, 11, 18)))
    assert env.run_api(datetime(2026, 11, 18, 12))[0] == 0
    deleted = env.query("SELECT count(*) FROM gfs_units WHERE deleted_at IS NOT NULL")[0][0]
    assert deleted >= 1  # the units of 11-03 and 11-04 are expired and gone

    collected = collect(env)

    assert coverage(collected) == (14, 14, 0, None)  # not 12: the oldest unit ever was 11-03


def test_each_source_job_has_its_own_coverage(env):
    env.write(SINGLE_DAILY)
    units_on(env, days(D(2026, 11, 3), D(2026, 11, 18)))
    units_on(env, days(D(2026, 11, 3), D(2026, 11, 18), skip=[D(2026, 11, 12)]), "other")

    collected = collect(env)

    assert coverage(collected, source="files") == (14, 14, 0, None)
    assert coverage(collected, source="other") == (14, 13, 1, midnight(D(2026, 11, 12)))


# ---- reach (spec 4.2) and size (spec 4.3) ------------------------------------------------------


def unit_records(env, unit: Path):
    """The records of a unit, oldest first."""
    return [r for r in env.history.records()[::-1] if r.unit == str(unit.resolve())]


def two_sources(env):
    """Files and a chain of xtrabackup units, one of each in stage 1 and in stage 2.

    The chains' members complete an hour apart, so that a unit's newest and oldest times differ.
    Returns ``{(stage, source job): the records of its unit}``.
    """
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    for unit in (env.xtrabackup_unit(7), env.xtrabackup_unit(0)):
        day = date.fromisoformat(unit.name)
        for hour, record in zip((13, 14), unit_records(env, unit)[1:], strict=True):
            when = datetime(day.year, day.month, day.day, hour, tzinfo=UTC).isoformat()
            sql(env, "UPDATE backup_runs SET completed_at=? WHERE id=?", when, record.id)
    assert env.run_api(datetime.now())[0] == 0
    stage_names = {str(env.abs("BACKUP")): "1", str(env.abs("NFS/daily")): "2"}
    placed = {}
    for unit, source, stage_path in env.query(
        "SELECT u.unit, u.series, l.stage_path FROM gfs_units u"
        " JOIN gfs_locations l ON l.unit_id = u.id"
    ):
        placed[(stage_names[stage_path], source)] = [
            r for r in env.history.records() if r.unit == unit
        ]
    assert set(placed) == {("1", "files"), ("1", "xb"), ("2", "files"), ("2", "xb")}
    return placed


def test_reach_is_the_newest_and_oldest_time_per_stage_and_source(env):
    placed = two_sources(env)

    collected = collect(env)

    newest = series(collected, "bdbackup_gfs_source_newest_timestamp_seconds")
    oldest = series(collected, "bdbackup_gfs_source_oldest_timestamp_seconds")
    for key, records in placed.items():
        stamps = [epoch(r.completed_at) for r in records]
        assert newest[key] == max(stamps)
        assert oldest[key] == min(stamps)
    chain = placed[("1", "xb")]
    assert len(chain) == 3 and newest[("1", "xb")] > oldest[("1", "xb")]  # the newest incremental
    assert set(newest) == set(oldest) == set(placed)  # nothing in stage 3: no reach series


def test_source_units_and_bytes_sum_to_the_stage_figures(env):
    placed = two_sources(env)

    collected = collect(env)

    units = series(collected, "bdbackup_gfs_source_units")
    size = series(collected, "bdbackup_gfs_source_bytes")
    stage_units = values(collected, "bdbackup_gfs_stage_units", "job_name", "stage")
    stage_bytes = values(collected, "bdbackup_gfs_stage_bytes", "job_name", "stage")
    assert units == {
        (stage, source): int((stage, source) in placed)
        for stage in "123" for source in ("files", "xb")
    }
    assert size == {
        (stage, source): sum(r.size_bytes for r in placed.get((stage, source), ()))
        for stage in "123" for source in ("files", "xb")
    }
    for stage in "123":
        assert sum(v for (s, _), v in units.items() if s == stage) == stage_units[(JOB, stage)]
        assert sum(v for (s, _), v in size.items() if s == stage) == stage_bytes[(JOB, stage)]
    assert stage_units[(JOB, "1")] == 2 and stage_bytes[(JOB, "1")] > 0


# ---- last move per stage pair (spec 4.4) -------------------------------------------------------

TWO_PATHS = [(["BACKUP"], "daily", "5d"), (["NFS/daily", "NFS/daily2"], "daily", "20d"),
             (["NFS/weekly"], "weekly", "8w")]


def step(env, run_at, source, destination, *, action="move", outcome="ok", job=JOB):
    sql(env, "INSERT INTO gfs_steps (run_at, gfs_job, action, source, destination, outcome)"
             " VALUES (?, ?, ?, ?, ?, ?)", run_at, job, action, source, destination, outcome)


def last_moves(collected):
    found = values(collected, "bdbackup_gfs_last_move_timestamp_seconds",
                   "job_name", "from_stage", "to_stage")
    return {key[1:]: value for key, value in found.items() if key[0] == JOB}


def test_the_last_move_of_a_stage_pair_is_the_newest_ok_move(env):
    env.write(TWO_PATHS)
    touch_database(env)
    back, d1, d2, weekly = (str(env.abs(p)) for p in ("BACKUP", "NFS/daily", "NFS/daily2",
                                                       "NFS/weekly"))
    step(env, "2026-11-10T02:00:00+00:00", f"{back}/files/a", f"{d1}/files/a")
    step(env, "2026-11-12T02:00:00+00:00", f"{back}/files/b", f"{d1}/files/b")  # the newest 1 to 2
    step(env, "2026-11-12T02:00:01+00:00", f"{back}/files/b", f"{d2}/files/b")  # its second path
    step(env, "2026-11-11T02:00:00+00:00", f"{d1}/files/a", f"{weekly}/files/a")
    step(env, "2026-11-18T02:00:00+00:00", f"{d1}/files/c", f"{weekly}/files/c", outcome="failed")
    step(env, "2026-11-18T02:00:00+00:00", f"{d1}/files/c", f"{weekly}/files/c", action="hold")
    step(env, "2026-11-17T02:00:00+00:00", f"{d1}/files/a", None)  # completes an interrupted move
    step(env, "2026-11-17T02:00:00+00:00", f"{back}/files/z", f"{weekly}/files/z", job="other")
    step(env, "2026-11-17T02:00:00+00:00", "/elsewhere/a", f"{weekly}/files/z")  # no stage

    collected = collect(env)

    assert last_moves(collected) == {
        ("1", "2"): epoch("2026-11-12T02:00:01+00:00"),
        ("2", "3"): epoch("2026-11-11T02:00:00+00:00"),
    }


def test_no_move_means_no_last_move_series(env):
    touch_database(env)

    assert last_moves(collect(env)) == {}


# ---- unmanaged records (spec 4.5) --------------------------------------------------------------


def unmanaged(collected):
    units = values(collected, "bdbackup_gfs_unmanaged_units", "job_name", "stage", "reason")
    size = values(collected, "bdbackup_gfs_unmanaged_bytes", "job_name", "stage", "reason")
    return (
        {key[1:]: value for key, value in units.items() if key[0] == JOB},
        {key[1:]: value for key, value in size.items() if key[0] == JOB},
    )


def test_unmanaged_records_by_reason_equal_what_the_run_counts(env):
    env.file_unit("kept.tar.gz", days_ago=0)
    bare = env.file_unit("bare.tar.gz", days_ago=1)  # a record with no unit and no checksum
    sql(env, "UPDATE backup_runs SET unit=NULL, checksum=NULL WHERE path=?", str(bare.resolve()))
    gone = env.file_unit("gone.tar.gz", days_ago=2)
    gone_size = gone.stat().st_size
    gone.unlink()  # missing when GFS first judges it
    assert env.run_api(datetime.now())[0] == 0
    counted = gfs_store.load(
        env.history, env.abs("BACKUP"),
        {path for stage in Config(env.config_path).get("gfs-main").params["stage"]
         for path in stage.paths},
    )[1]

    collected = collect(env)

    units, size = unmanaged(collected)
    assert units == {("1", "no_checksum"): 1, ("1", "unavailable"): 1}
    assert size == {("1", "no_checksum"): bare.stat().st_size, ("1", "unavailable"): gone_size}
    assert sum(units.values()) == counted == env.query("SELECT unmanaged FROM gfs_runs")[0][0] == 2


def test_both_classes_are_zero_when_nothing_is_unmanaged(env):
    env.file_unit("kept.tar.gz", days_ago=0)
    assert env.run_api(datetime.now())[0] == 0

    units, size = unmanaged(collect(env))

    assert units == {("1", "no_checksum"): 0, ("1", "unavailable"): 0}
    assert size == {("1", "no_checksum"): 0, ("1", "unavailable"): 0}


# ---- the stage's verify flag and the schema version (spec 4.6) ---------------------------------


def test_stage_verify_is_1_or_0_for_each_stage(env):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/daily"], "daily", "20d", "verify = false"),
               (["NFS/weekly"], "weekly", "8w", "verify = true")])

    collected = collect(env)

    found = values(collected, "bdbackup_gfs_stage_verify", "job_name", "stage")
    assert found == {(JOB, "1"): 1, (JOB, "2"): 0, (JOB, "3"): 1}


def test_the_schema_version_is_the_databases_user_version(env):
    touch_database(env)

    collected = collect(env)

    version = sql(env, "PRAGMA user_version")[0][0]
    assert version >= 6
    assert values(collected, "bdbackup_history_schema_version") == {(): version}


def test_the_schema_version_does_not_wait_for_the_share(env):
    touch_database(env)

    collected = collect_all(
        Config(env.config_path).jobs, History(Config(env.config_path).history), now=TODAY,
        timeout_seconds=5, probe=lambda requests, timeout: Results({}, {DESTINATION: "timeout"}),
    )

    assert values(collected, "bdbackup_history_schema_version") == {
        (): sql(env, "PRAGMA user_version")[0][0]
    }


def test_there_is_no_schema_version_before_the_database_exists(env):
    assert not env.database.exists()

    assert values(collect(env), "bdbackup_history_schema_version") == {}
