"""Schema 7 and SSH delivery recording at the history transaction boundary."""

import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupError, BackupResult
from bdbackup.cli import main
from bdbackup.gfs import store
from bdbackup.history import Delivery, History, HistoryError, HistorySettings
from bdbackup.ssh_copy import CopySettings

DEST = "ssh://backup@vault/ssh-copy"
OTHER_DEST = "ssh://backup@other/ssh-copy"
NOW = datetime(2026, 11, 25, 11, tzinfo=UTC)
SETTINGS = CopySettings(DEST, "rsync", "vault", "/ssh-copy", user="backup")
OTHER = CopySettings(OTHER_DEST, "scp", "other", "/ssh-copy", user="backup")
TABLES = (
    "backup_runs", "gfs_units", "gfs_locations", "gfs_members", "gfs_runs", "gfs_steps",
    "last_runs",
)


@pytest.fixture
def history(tmp_path):
    return History(HistorySettings(tmp_path / "state" / "history.db", tmp_path / "restore"))


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / "archive.tar.gz"
    path.write_bytes(b"archive")
    return path


def add(history, artifact, *, job="file-job", copy=SETTINGS):
    history.run(job, "file", lambda: BackupResult(artifact, 7, True), copy=copy)
    return history.records(job=job)[0].id


def version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def schema_six(history, artifact):
    record = add(history, artifact, copy=None)
    history.record_last_run("export-job", "pgbackrest", 0, "exported", {"label": "latest"})
    with closing(sqlite3.connect(history.settings.database)) as db, db:
        db.execute("DROP TABLE IF EXISTS deliveries")
        db.execute("PRAGMA user_version = 6")
        db.execute(
            "INSERT INTO gfs_units (id, unit, series, relative)"
            " VALUES (10, ?, 'file-job', 'a.tar')",
            (str(artifact),),
        )
        db.execute(
            "INSERT INTO gfs_locations (unit_id, stage_path, path, identities)"
            " VALUES (10, '/gfs/first', '/gfs/first/a.tar', '{}')"
        )
        db.execute("INSERT INTO gfs_members (record_id, managed) VALUES (?, 1)", (record,))
        db.execute(
            "INSERT INTO gfs_runs (id, gfs_job, started_at, status)"
            " VALUES (12, 'rotate', '2026-11-25', 'completed')"
        )
        db.execute(
            "INSERT INTO gfs_steps (run_at, gfs_job, unit_id, action, outcome, run_id)"
            " VALUES ('2026-11-25', 'rotate', 10, 'move', 'ok', 12)"
        )


def snapshot(db):
    return {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in TABLES}  # noqa: S608


def test_schema_six_reads_in_place_and_first_write_keeps_all_old_rows(history, artifact):
    schema_six(history, artifact)
    with closing(sqlite3.connect(history.settings.database)) as db:
        assert version(db) == 6
        before = snapshot(db)
    before_bytes = history.settings.database.read_bytes()

    assert history.deliveries() == []
    assert history.settings.database.read_bytes() == before_bytes

    with history._connect(create=True):
        pass

    with closing(sqlite3.connect(history.settings.database)) as db:
        assert version(db) == 7, "first write must upgrade a schema 6 database to schema 7"
        assert snapshot(db) == before
    assert history.deliveries() == []


def test_fresh_database_has_schema_seven_even_without_copy(history, artifact):
    add(history, artifact, copy=None)
    with closing(sqlite3.connect(history.settings.database)) as db:
        assert version(db) == 7, "new databases must begin at schema 7"
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (0,)


def test_pending_and_success_become_visible_together(history, artifact):
    def action():
        assert history.records()[0].status == "running"
        assert history.deliveries() == []
        return BackupResult(artifact, 7, True)

    history.run("file-job", "file", action, copy=SETTINGS)

    backup, = history.records()
    assert backup.status == "success" and backup.checksum
    assert history.deliveries() == [
        Delivery(backup.id, "file-job", DEST, "rsync", "pending", None, 0, None, None, None),
    ], "the backup's success must leave a pending delivery"


def test_failed_backup_and_old_backup_have_no_delivery(history, artifact):
    old = add(history, artifact, copy=None)

    def fail():
        raise BackupError("backup failed")

    with pytest.raises(BackupError):
        history.run("file-job", "file", fail, copy=SETTINGS)
    new = add(history, artifact)

    assert history.get(old).status == "success"
    assert {record.status for record in history.records()} == {"success", "failed"}
    assert [delivery.record_id for delivery in history.deliveries()] == [new]


def test_delivery_insert_failure_rolls_back_the_backup_success_update(history, artifact):
    with history._connect(create=True) as db:
        assert version(db) == 7, "the delivery trigger requires a schema 7 database"
        db.execute(
            """CREATE TRIGGER refuse_delivery BEFORE INSERT ON deliveries
               BEGIN SELECT RAISE(ABORT, 'delivery insert refused'); END"""
        )

    with pytest.raises(HistoryError, match="delivery insert refused"):
        add(history, artifact)

    backup, = history.records()
    assert backup.status == "running"
    assert backup.checksum is None
    assert history.deliveries() == []


def test_attempt_count_and_method_follow_the_method_actually_used(history, artifact):
    record = add(history, artifact)
    first = history.start_delivery_attempt(record, "rsync", NOW)
    assert first == 1, "starting an attempt must increment its durable count"
    history.finish_delivery_attempt(record, first, "failed", NOW, failure_class="connect")

    started = NOW + timedelta(hours=1)
    retry = history.start_delivery_attempt(record, "scp", started)
    row, = history.deliveries()
    assert retry == 2
    assert (row.method, row.status, row.attempt_count) == ("scp", "failed", 2)
    assert (row.started_at, row.ended_at) == (started.isoformat(), None)

    ended = started + timedelta(seconds=2)
    history.finish_delivery_attempt(record, retry, "copied", ended, bytes_sent=0)
    assert history.deliveries() == [
        Delivery(
            record, "file-job", DEST, "scp", "copied", None, 2, 0,
            started.isoformat(), ended.isoformat(),
        ),
    ]


def test_interrupted_attempt_keeps_pending_and_the_attempt_method(history, artifact):
    record = add(history, artifact)
    history.start_delivery_attempt(record, "scp", NOW)

    assert history.deliveries() == [
        Delivery(record, "file-job", DEST, "scp", "pending", None, 1, None, NOW.isoformat(), None),
    ]


def test_uncopied_lookup_filters_destination_and_job_newest_first(history, artifact):
    old = add(history, artifact)
    add(history, artifact, job="other-job")
    different_destination = add(history, artifact, copy=OTHER)
    already_copied = add(history, artifact)
    abandoned = add(history, artifact)
    newest = add(history, artifact)

    for record, status, error, size in [
        (newest, "failed", "transfer", None),
        (abandoned, "abandoned", "source_unavailable", None),
        (already_copied, "copied", None, 7),
    ]:
        attempt = history.start_delivery_attempt(record, "rsync", NOW)
        history.finish_delivery_attempt(
            record, attempt, status, NOW, failure_class=error, bytes_sent=size,
        )

    assert [
        row.record_id for row in history.deliveries(job="file-job", destination=DEST, uncopied=True)
    ] == [newest, old]
    assert [
        row.record_id for row in history.deliveries(destination=OTHER_DEST)
    ] == [different_destination]


def test_history_rejects_a_diagnostic_instead_of_a_failure_class(history, artifact):
    record = add(history, artifact)
    attempt = history.start_delivery_attempt(record, "rsync", NOW)
    diagnostic = "credential in a synthetic SSH diagnostic"
    with pytest.raises(HistoryError, match="CHECK constraint failed"):
        history.finish_delivery_attempt(record, attempt, "failed", NOW, failure_class=diagnostic)
    assert diagnostic.encode() not in history.settings.database.read_bytes()
    history.finish_delivery_attempt(record, attempt, "failed", NOW, failure_class="connect")
    row, = history.deliveries()
    assert row.failure_class == "connect"


def test_old_attempt_cannot_overwrite_the_newer_attempt(history, artifact):
    record = add(history, artifact)
    first = history.start_delivery_attempt(record, "rsync", NOW)
    retry = history.start_delivery_attempt(record, "scp", NOW)

    with pytest.raises(HistoryError, match="stale or missing"):
        history.finish_delivery_attempt(record, first, "failed", NOW, failure_class="transfer")

    history.finish_delivery_attempt(record, retry, "copied", NOW, bytes_sent=7)
    with pytest.raises(HistoryError, match="uncopied"):
        history.start_delivery_attempt(record, "rsync", NOW)
    row, = history.deliveries()
    assert (row.status, row.method, row.attempt_count) == ("copied", "scp", 2)


def test_no_database_read_creates_nothing(history):
    assert history.deliveries() == []
    assert not history.settings.database.parent.exists()


@pytest.fixture
def list_history(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[history]\ndatabase="state/history.db"\n')

    def unexpected(*args, **kwargs):
        pytest.fail("history started an external process")

    monkeypatch.setattr(subprocess, "run", unexpected)

    def invoke(*args):
        result = CliRunner().invoke(main, ["history", "-c", str(config), *args])
        assert result.exit_code == 0, result.output
        return {int(line.split(":", 1)[0]): line for line in result.stdout.splitlines()}

    return invoke


def display_delivery(history, artifact, status, *, job="file-job"):
    record = add(history, artifact, job=job)
    if status == "pending":
        return record
    attempt = history.start_delivery_attempt(record, "scp", NOW)
    failure_class = {"abandoned": "source_unavailable", "failed": "connect"}.get(status)
    history.finish_delivery_attempt(
        record, attempt, status, NOW, failure_class=failure_class,
        bytes_sent=7 if status == "copied" else None,
    )
    return record


def test_history_shows_exact_copy_suffixes_on_mixed_records(history, artifact, list_history):
    plain = add(history, artifact, copy=None)
    before = list_history()[plain]
    suffixes = {
        display_delivery(history, artifact, "pending"): " | copy pending",
        display_delivery(history, artifact, "copied"): " | copy copied",
        display_delivery(history, artifact, "failed"): " | copy failed (connect)",
        display_delivery(history, artifact, "abandoned"): " | copy abandoned (source_unavailable)",
    }
    snapshot = history.settings.database.read_bytes()

    lines = list_history()

    assert lines[plain] == before
    assert set(lines) == {plain, *suffixes}
    for record, suffix in suffixes.items():
        assert lines[record].endswith(suffix), f"wrong copy suffix for {lines[record]}"
        assert lines[record].count(" | copy ") == 1
        assert len(lines[record].split(" | ")) == len(before.split(" | ")) + 1
        assert DEST not in lines[record]
    assert history.settings.database.read_bytes() == snapshot


def test_history_filters_still_treat_a_failed_copy_as_a_successful_backup(
    history, artifact, list_history,
):
    copied = display_delivery(history, artifact, "failed")
    other = display_delivery(history, artifact, "pending", job="other-job")

    def fail():
        raise BackupError("synthetic backup failure")

    with pytest.raises(BackupError):
        history.run("file-job", "file", fail, copy=SETTINGS)
    failed = history.records(job="file-job")[0].id

    assert set(list_history("--job", "file-job")) == {copied, failed}
    assert set(list_history("--successful")) == {copied, other}
    selected = list_history("--job", "file-job", "--successful")
    assert list(selected) == [copied]
    assert " | success | " in selected[copied]
    assert selected[copied].endswith(" | copy failed (connect)")
    assert " | copy " not in list_history()[failed]


def test_history_copy_suffix_follows_the_existing_gfs_suffix(history, artifact, list_history):
    record_id = display_delivery(history, artifact, "pending")
    source = Path(history.get(record_id).path)
    units, _, seen = store.load(history, source.parent)
    unit, = units
    store.record_unit(history, unit)
    store.record_members(history, seen)

    line = list_history()[record_id]

    assert line.endswith(
        f" | stage {source.parent} | locations {source} | copy pending"
    )


def test_history_reads_schema_six_without_migrating_or_adding_a_copy_suffix(
    history, artifact, list_history,
):
    schema_six(history, artifact)
    before = history.settings.database.read_bytes()

    line, = list_history().values()

    assert " | copy " not in line
    assert " | stage /gfs/first | locations /gfs/first/a.tar" in line
    assert history.settings.database.read_bytes() == before
