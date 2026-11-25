"""History schema 6: ``last_runs``, one row per job with the outcome of its newest real run."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from bdbackup.backends import BackupResult
from bdbackup.history import History, HistoryError, HistorySettings, LastRun

COLUMNS = ["job_name", "job_type", "finished_at", "exit_code", "result", "facts"]
FACTS = {
    "phases": {"write": 2.5, "verify": 3.0},
    "label": "20261109-010000F",
    "bytes": 1234,
    "full_age_seconds": 9000,
}


def make_history(tmp_path) -> History:
    return History(HistorySettings(tmp_path / "state" / "history.sqlite3", tmp_path / "restore"))


def add_record(history: History, tmp_path, job="job") -> None:
    artifact = tmp_path / f"{job}.tar"
    artifact.write_bytes(b"unit")
    history.run(job, "file", lambda: BackupResult(artifact, 4, True), {"kind": "file"})


def make_schema_5(history: History, tmp_path) -> None:
    """A database as 0.7.2 leaves it: one record, schema 5 and no ``last_runs`` table."""
    add_record(history, tmp_path)
    with closing(sqlite3.connect(history.settings.database)) as db, db:
        db.execute("DROP TABLE IF EXISTS deliveries")
        db.execute("DROP TABLE IF EXISTS last_runs")
        db.execute("PRAGMA user_version = 5")


def last_run(history: History, job: str) -> LastRun:
    runs = history.last_runs()
    assert job in runs, f"no last_runs row for {job!r}: {sorted(runs)}"
    return runs[job]


def user_version(history: History) -> int:
    with closing(sqlite3.connect(history.settings.database)) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def tables(history: History) -> set[str]:
    with closing(sqlite3.connect(history.settings.database)) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_a_new_database_is_schema_7_with_the_last_runs_table(tmp_path):
    history = make_history(tmp_path)

    add_record(history, tmp_path)

    assert user_version(history) == 7
    with closing(sqlite3.connect(history.settings.database)) as db:
        assert [r[1] for r in db.execute("PRAGMA table_info(last_runs)")] == COLUMNS
        assert db.execute("SELECT count(*) FROM last_runs").fetchone()[0] == 0


def test_a_schema_5_database_migrates_and_gets_last_runs_on_the_first_write(tmp_path):
    history = make_history(tmp_path)
    make_schema_5(history, tmp_path)
    assert user_version(history) == 5
    assert "last_runs" not in tables(history)

    history.record_last_run("pg", "pgbackrest", 0, "exported")

    assert user_version(history) == 7
    assert "last_runs" in tables(history)
    assert [r.job_name for r in history.records()] == ["job"]  # what was there is kept
    assert set(history.last_runs()) == {"pg"}


def test_last_runs_of_a_schema_5_database_is_empty_and_never_migrates_it(tmp_path):
    history = make_history(tmp_path)
    make_schema_5(history, tmp_path)
    before = history.settings.database.read_bytes()

    assert history.last_runs() == {}

    assert user_version(history) == 5
    assert history.settings.database.read_bytes() == before
    # Only a write migrates it, and then the row is there to be read.
    history.record_last_run("pg", "pgbackrest", 1, "failed")
    assert last_run(history, "pg").result == "failed"


def test_last_runs_without_a_database_is_empty_and_creates_nothing(tmp_path):
    history = make_history(tmp_path)

    assert history.last_runs() == {}

    assert not history.settings.database.parent.exists()
    history.record_last_run("pg", "pgbackrest", 0, "exported")
    assert last_run(history, "pg").exit_code == 0


def test_record_last_run_upserts_one_row_per_job(tmp_path):
    history = make_history(tmp_path)
    before = datetime.now(UTC)

    history.record_last_run("pg", "pgbackrest", 0, "exported")
    history.record_last_run("gfs-main", "gfs", 3, "deferred")
    history.record_last_run("pg", "pgbackrest", 1, "failed")

    runs = history.last_runs()
    assert sorted(runs) == ["gfs-main", "pg"]
    pg = runs["pg"]
    assert isinstance(pg, LastRun)
    assert (pg.job_name, pg.job_type, pg.exit_code, pg.result) == ("pg", "pgbackrest", 1, "failed")
    assert (runs["gfs-main"].job_type, runs["gfs-main"].exit_code) == ("gfs", 3)
    finished = datetime.fromisoformat(pg.finished_at)
    assert finished.utcoffset() == timedelta(0)
    assert before <= finished <= datetime.now(UTC)
    with closing(sqlite3.connect(history.settings.database)) as db:
        assert db.execute("SELECT count(*) FROM last_runs").fetchone()[0] == 2


def test_record_last_run_keeps_the_stored_facts_when_it_is_given_none(tmp_path):
    history = make_history(tmp_path)

    history.record_last_run("pg", "pgbackrest", 0, "exported", FACTS)
    assert last_run(history, "pg").facts == FACTS
    history.record_last_run("pg", "pgbackrest", 0, "no_new_full")

    kept = last_run(history, "pg")
    assert kept.result == "no_new_full"
    assert kept.facts == FACTS

    newer = {"phases": {"write": 1.0}, "label": "20261110-010000F"}
    history.record_last_run("pg", "pgbackrest", 0, "exported", newer)
    assert last_run(history, "pg").facts == newer


def test_a_job_that_never_recorded_facts_has_none(tmp_path):
    history = make_history(tmp_path)

    history.record_last_run("gfs-main", "gfs", 0, "completed")

    assert last_run(history, "gfs-main").facts is None


def test_last_runs_reads_without_changing_the_database(tmp_path):
    history = make_history(tmp_path)
    history.record_last_run("pg", "pgbackrest", 0, "exported", FACTS)
    assert set(history.last_runs()) == {"pg"}
    before = history.settings.database.read_bytes()

    runs = history.last_runs()

    assert runs["pg"].facts == FACTS
    assert history.settings.database.read_bytes() == before


def test_a_future_schema_is_still_refused_without_modification(tmp_path):
    history = make_history(tmp_path)
    history.settings.database.parent.mkdir()
    with closing(sqlite3.connect(history.settings.database)) as db:
        db.execute("PRAGMA user_version=99")

    with pytest.raises(HistoryError, match="schema version"):
        history.record_last_run("pg", "pgbackrest", 0, "exported")
    with pytest.raises(HistoryError, match="schema version"):
        history.last_runs()

    assert user_version(history) == 99
    assert "last_runs" not in tables(history)
