"""CLI and runner witnesses for backup records, retries, and queue serialization."""

import gzip
import logging
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from threading import Event

import pytest
from click.testing import CliRunner

from bdbackup import copy_runner
from bdbackup.backends import BackupResult
from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.engines import _reset_for_tests
from bdbackup.filebackup import FileBackup
from bdbackup.gfs import store
from bdbackup.history import History, artifact_identity
from bdbackup.mysql import MySQLBackup
from bdbackup.utils import process_lock
from tests.ssh_copy_fake import Remote

DEST = "ssh://vault/remote/archive"
JOB = "daily"
FILE = (
    '[daily]\ntype="file"\nbackup_dst="source-stage/daily"\n'
    'template_filename="paths"\nchdir="input"\ntimestamp=true\n'
)
MYSQL = '[daily]\ntype="mysqldump"\ndatabase="daily"\nout_dir="source-stage"\n'
COPY = f'\n[daily.copy]\ndestination="{DEST}"\nmethod="rsync"\n'
CONNECT_DIAGNOSTIC = "Permission denied (publickey), synthetic private detail"


@dataclass
class Site:
    path: Path

    def configure(self, job=FILE, copy=COPY):
        self.path.write_text('[history]\ndatabase="state/history.db"\n' + job + copy)

    @property
    def config(self):
        return Config(self.path)

    @property
    def history(self):
        return History(self.config.history)

    @property
    def copy_lock(self):
        return Path(f"{self.history.settings.database}.copy-{JOB}.lock")

    def run(self, *args):
        return CliRunner().invoke(main, ["run", "-c", str(self.path), "-j", JOB, *args])

    def drain(self, output):
        return copy_runner.run_job(self.history, JOB, self.config.get(JOB).copy, output.append)

    def seed(self, filename):
        path = self.path.parent / "source-stage" / filename
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(filename.encode())
        self.history.run(
            JOB, "file", lambda: BackupResult(path, path.stat().st_size, True),
            copy=self.config.get(JOB).copy,
        )
        return path.resolve()


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    stamps = count(1)
    monkeypatch.setattr(
        FileBackup, "_stamp", staticmethod(lambda: f"2026-11-25-{next(stamps):06d}"),
    )
    (tmp_path / "input").mkdir()
    (tmp_path / "input/data").write_text("data")
    (tmp_path / "paths").write_text("data\n")
    site = Site(tmp_path / "config.toml")
    site.configure()
    yield site
    _reset_for_tests()


@pytest.fixture
def remote(monkeypatch):
    peer = Remote()
    monkeypatch.setattr(subprocess, "run", peer)
    return peer


def fake_mysql_backup(self):
    self.out_dir.mkdir(exist_ok=True)
    path = self.out_dir / "daily.sql.gz"
    with gzip.open(path, "wb") as archive:
        archive.write(b"SELECT 1;")
    return BackupResult(path, path.stat().st_size, True)


@pytest.mark.parametrize("job", [FILE, MYSQL], ids=["file", "mysqldump"])
@pytest.mark.parametrize("method", ["rsync", "scp"])
def test_cli_copies_a_published_job_and_records_the_result(site, remote, monkeypatch, job, method):
    site.configure(job, COPY.replace("rsync", method))
    monkeypatch.setattr(MySQLBackup, "backup", fake_mysql_backup)
    observed = []

    def before(argv):
        backup, = site.history.records()
        observed.append((backup.status, site.history.deliveries()[0].status))

    remote.before = before
    result = site.run()

    assert result.exit_code == 0, result.output
    backup, = site.history.records()
    name = Path(backup.path).name
    assert re.search(
        rf"^Copy daily: {re.escape(name)} -> {re.escape(DEST)} "
        rf"\({method}, {backup.size_bytes} bytes, \d+ s\)$", result.stdout, re.M,
    ), "a published backup must report its copy"
    assert result.stdout.splitlines()[0] == f"Job daily: {backup.path}"
    delivery, = site.history.deliveries()
    assert (delivery.status, delivery.attempt_count, delivery.bytes_sent) == (
        "copied", 1, backup.size_bytes,
    )
    assert datetime.fromisoformat(delivery.ended_at) >= datetime.fromisoformat(delivery.started_at)
    assert observed and set(observed) == {("success", "pending")}
    assert remote.digest(f"/remote/archive/{name}") == backup.checksum
    assert site.history.last_runs() == {}


def test_no_verify_keeps_the_local_effect_and_still_checks_the_remote_digest(
    site, remote, monkeypatch,
):
    verify = FileBackup.verify
    calls = []

    def track(self, result):
        calls.append(result.path)
        return verify(self, result)

    monkeypatch.setattr(FileBackup, "verify", track)
    remote.bad_checksum = True
    result = site.run("--no-verify")

    assert result.exit_code == 1, "remote SHA verification still applies under --no-verify"
    assert len(calls) == 1 and calls[0].name.endswith(".tmp")
    assert site.history.records()[0].status == "success"
    assert site.history.deliveries()[0].failure_class == "checksum_mismatch"
    assert remote.files == {}


def test_copy_failure_logs_last_diagnostic_and_leaves_successful_local_backup(
    site, remote, caplog,
):
    remote.before = lambda argv: subprocess.CompletedProcess(
        argv, 255, "", f"First line\n{CONNECT_DIAGNOSTIC}\n",
    )
    with caplog.at_level(logging.ERROR, logger="bdbackup"):
        result = site.run()

    assert result.exit_code == 1, "a failed copy must fail the run after recording success"
    backup, = site.history.records()
    name = Path(backup.path).name
    assert f"Copy daily: {name} failed: connect" in result.stdout
    assert backup.status == "success" and Path(backup.path).is_file()
    delivery, = site.history.deliveries()
    assert (delivery.status, delivery.failure_class, delivery.attempt_count) == (
        "failed", "connect", 1,
    )
    assert CONNECT_DIAGNOSTIC.encode() not in site.history.settings.database.read_bytes()
    assert any(
        record.levelno == logging.ERROR and CONNECT_DIAGNOSTIC in record.message
        for record in caplog.records
    )
    assert "First line" not in caplog.text
    assert len(remote.calls) == 1


def test_failed_backup_starts_no_copy_and_leaves_an_existing_delivery_pending(site, remote):
    site.seed("older.tar")
    (site.path.parent / "paths").write_text("")

    result = site.run()

    assert result.exit_code == 1
    assert remote.calls == []
    assert [record.status for record in site.history.records()] == ["failed", "success"]
    delivery, = site.history.deliveries()
    assert (delivery.status, delivery.attempt_count) == ("pending", 0)


def test_without_copy_a_backup_starts_no_ssh_and_leaves_an_existing_delivery_pending(site, remote):
    site.seed("older.tar")
    site.configure(copy="")

    result = site.run()

    assert result.exit_code == 0, result.output
    assert remote.calls == [] and "Copy " not in result.stdout
    delivery, = site.history.deliveries()
    assert (delivery.status, delivery.attempt_count) == ("pending", 0)


@pytest.mark.parametrize("job,method", [(FILE, "rsync"), (MYSQL, "scp")], ids=["file", "mysqldump"])
def test_dry_run_reports_copy_without_starting_ssh_or_creating_history(site, remote, job, method):
    site.configure(job, COPY.replace("rsync", method))

    result = site.run("--dry-run")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == f"Dry run; configured copy: {DEST} ({method})"
    assert not remote.calls
    assert not site.history.settings.database.parent.exists()
    assert not (site.path.parent / "source-stage").exists()


def test_dry_run_leaves_a_pending_copy_untouched(site, remote):
    site.seed("older.tar")
    database = site.history.settings.database
    before = database.read_bytes()

    result = site.run("--dry-run")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == f"Dry run; configured copy: {DEST} (rsync)"
    assert not remote.calls and not site.copy_lock.exists()
    assert database.read_bytes() == before


def test_newest_first_retries_once_with_current_method_and_leaves_other_destination(site, remote):
    site.configure(copy=COPY.replace("vault", "other"))
    other = site.seed("other.tar")
    site.configure()
    older = site.seed("older.tar")
    remote.before = lambda argv: subprocess.CompletedProcess(argv, 255, "", "interrupted")

    first = site.run()
    assert first.exit_code == 1, "failed attempts must be reported by the CLI"
    old_records = site.history.records()
    assert len(remote.calls) == 2, "every eligible backup is attempted once despite a failure"
    assert [row.attempt_count for row in site.history.deliveries()] == [1, 1, 0]
    assert older.name in first.stdout and other.name not in first.stdout

    site.configure(copy=COPY.replace("rsync", "scp"))
    remote.before = None
    second = site.run()

    assert second.exit_code == 0, second.output
    newest = Path(site.history.records()[0].path)
    assert [(method, path) for method, path, _ in remote.transfers] == [
        ("scp", newest), ("scp", Path(old_records[0].path)), ("scp", older),
    ]
    assert [(row.method, row.status, row.attempt_count) for row in site.history.deliveries()] == [
        ("scp", "copied", 1), ("scp", "copied", 2), ("scp", "copied", 2),
        ("rsync", "pending", 0),
    ]


def test_missing_failed_source_is_abandoned_once_and_matching_copy_sends_no_bytes(site, remote):
    older = site.seed("gone.tar")
    record = site.history.records()[0]
    attempt = site.history.start_delivery_attempt(record.id, "rsync", datetime.now(UTC))
    site.history.finish_delivery_attempt(
        record.id, attempt, "failed", datetime.now(UTC), failure_class="connect",
    )
    older.unlink()
    matching = site.seed("matching.tar")
    remote.files[f"/remote/archive/{matching.name}"] = matching.read_bytes()

    first = site.run()

    assert first.exit_code == 1, "abandonment must fail the run"
    assert "Copy daily: gone.tar failed: source_unavailable" in first.stdout
    assert f"Copy daily: matching.tar -> {DEST} (already there)" in first.stdout
    assert [path.name for _, path, _ in remote.transfers] == ["daily-2026-11-25-000001.tar"]
    abandoned = next(row for row in site.history.deliveries() if row.record_id == record.id)
    assert abandoned.status == "abandoned" and abandoned.attempt_count == 2
    matched = site.history.deliveries()[1]
    assert matched.bytes_sent == 0 and matched.status == "copied"

    assert site.run().exit_code == 0
    assert next(row for row in site.history.deliveries() if row.record_id == record.id) == abandoned


def record_locations(site, record, *paths, missing_identity=frozenset()):
    history = site.history
    original = Path(record.path)
    units, _, judgments = store.load(history, original.parent)
    unit = next(unit for unit in units if unit.unit == original)
    unit_id = store.record_unit(history, unit)
    store.record_members(history, judgments)
    locations = [store.Location(path.parent, path) for path in paths]
    identities = {
        path: {record.id: artifact_identity(path)}
        for path in paths if path.exists() and path not in missing_identity
    }
    store.record_move(history, unit_id, locations, unit.locations, identities)


def test_move_during_transfer_retries_from_its_new_gfs_location(site, remote, monkeypatch):
    older = site.seed("moved.tar")
    moved = older.parent.parent / "later-stage" / older.name
    moved.parent.mkdir()
    from bdbackup import history as history_module

    checksum = history_module.checksum
    local_reads = []

    def observe(path):
        local_reads.append(path)
        return checksum(path)

    monkeypatch.setattr(history_module, "checksum", observe)

    def move(path):
        if path == older:
            record = site.history.records()[1]
            units, _, judgments = store.load(site.history, older.parent)
            unit = next(unit for unit in units if unit.unit == older)
            unit_id = store.record_unit(site.history, unit)
            store.record_members(site.history, judgments)
            older.rename(moved)
            store.record_move(
                site.history, unit_id, [store.Location(moved.parent, moved)], unit.locations,
                {moved: {record.id: artifact_identity(moved)}},
            )

    remote.after_transfer = move
    first = site.run()

    assert first.exit_code == 1, "a move during transfer must leave the delivery retryable"
    assert f"Copy daily: {older.name} failed: transfer" in first.stdout
    assert f"/remote/archive/{older.name}" not in remote.files

    second = site.run()

    assert second.exit_code == 0, second.output
    assert [path for _, path, _ in remote.transfers if path.name == older.name] == [older, moved]
    assert site.history.deliveries()[-1].status == "copied"
    assert len(local_reads) == 2 and all(path.name.startswith("daily-") for path in local_reads)


def test_first_identity_valid_gfs_location_is_used_once_without_transfer_fallback(site, remote):
    older = site.seed("locations.tar")
    record, = site.history.records()
    stale, unknown, first, second = [
        older.parent.parent / stage / older.name
        for stage in ("stale", "unknown", "first", "second")
    ]
    for path in (stale, unknown, first, second):
        path.parent.mkdir()
        path.write_bytes(older.read_bytes())
    record_locations(site, record, stale, unknown, first, second, missing_identity={unknown})
    stale.write_bytes(b"changed")
    seen = []

    def fail_first(argv):
        if argv[0] == "rsync" and Path(argv[-2]).name == older.name:
            seen.append(Path(argv[-2]))
            return subprocess.CompletedProcess(argv, 1, "", "failed during transfer")

    remote.before = fail_first
    result = site.run()

    assert result.exit_code == 1, "the first identity-valid location's failure must be recorded"
    assert seen == [first]
    assert site.history.deliveries()[-1].failure_class == "transfer"


@pytest.mark.parametrize("legacy", [False, True], ids=["missing-history", "schema-six"])
def test_copy_phase_with_no_delivery_schema_is_a_read_only_noop(site, remote, legacy):
    database = site.history.settings.database
    if legacy:
        site.seed("before-upgrade.tar")
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("DROP TABLE deliveries")
            db.execute("PRAGMA user_version = 6")
        before = database.read_bytes()
    lines = []

    assert site.drain(lines) == 0, "an unavailable delivery schema is an empty copy queue"
    assert site.history.next_uncopied(JOB, DEST, set()) is None
    assert not lines and not remote.calls
    assert not site.copy_lock.exists()
    if legacy:
        assert database.read_bytes() == before
    else:
        assert not database.parent.exists()


def test_interruption_before_result_leaves_pending_and_the_next_run_retries(site, remote):
    class Interrupted(BaseException):
        pass

    def interrupt(path):
        raise Interrupted

    remote.after_transfer = interrupt
    remote.cleanup_blocked = True
    with pytest.raises(Interrupted):
        site.run()
    backup, = site.history.records()
    delivery, = site.history.deliveries()
    assert backup.status == "success"
    assert delivery.status == "pending" and delivery.attempt_count == 1
    leftover, = remote.files
    assert "/.bdbackup-tmp-" in leftover

    remote.after_transfer = None
    remote.cleanup_blocked = False
    result = site.run()

    assert result.exit_code == 0, result.output
    assert leftover in remote.files
    assert [row.status for row in site.history.deliveries()] == ["copied", "copied"]


def test_second_backup_finishes_and_defers_while_first_is_in_transfer(site, remote):
    first = site.seed("first.tar")
    entered, release = Event(), Event()
    lines = []

    def pause(path):
        if path == first:
            entered.set()
            assert release.wait(5), "second backup did not finish during transfer"

    remote.after_transfer = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(site.drain, lines)
        try:
            assert entered.wait(2), "first run must reach its transfer"
            deferred = site.run()
            assert deferred.exit_code == 3, deferred.output
            assert "Copy daily: deferred: another run of daily is copying" in deferred.stdout
            assert [row.status for row in site.history.records()] == ["success", "success"]
            assert [row.status for row in site.history.deliveries()] == ["pending", "pending"]
        finally:
            release.set()
        assert future.result(timeout=5) == 0
    assert [row.status for row in site.history.deliveries()] == ["copied", "copied"]
    assert [path for _, path, _ in remote.transfers][0] == first
    assert len(lines) == 2


def test_final_rescan_sees_a_row_committed_after_the_optimistic_empty_read(
    site, remote, monkeypatch,
):
    site.seed("first.tar")
    empty, release = Event(), Event()
    original = History.next_uncopied

    def at_empty(self, job, destination, attempted, *, db=None):
        found = original(self, job, destination, attempted, db=db)
        if db is None and found is None and attempted and not empty.is_set():
            empty.set()
            assert release.wait(5), "new backup did not reach its copy phase"
        return found

    monkeypatch.setattr(History, "next_uncopied", at_empty)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(site.drain, [])
        try:
            assert empty.wait(2), "lock holder must reach an empty optimistic scan"
            deferred = site.run()
            assert deferred.exit_code == 3, deferred.output
            assert site.history.deliveries()[0].status == "pending"
        finally:
            release.set()
        assert future.result(timeout=5) == 0
    assert [row.status for row in site.history.deliveries()] == ["copied", "copied"]


def test_final_empty_read_and_flock_release_hold_the_same_sqlite_writer(
    site, remote, monkeypatch,
):
    site.seed("first.tar")
    empty, release = Event(), Event()
    original = History.next_uncopied
    observations = []

    def assert_writer_held():
        with closing(sqlite3.connect(site.history.settings.database, timeout=0)) as db:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")

    def at_empty(self, job, destination, attempted, *, db=None):
        found = original(self, job, destination, attempted, db=db)
        if db is not None and found is None:
            assert db.in_transaction
            empty.set()
            assert release.wait(5), "writer probe did not finish"
        return found

    @contextmanager
    def observe_release(path):
        with process_lock(path):
            yield
        # The flock has been released, and a new enqueue must still be blocked.
        assert_writer_held()
        observations.append("unlocked while writer held")

    monkeypatch.setattr(History, "next_uncopied", at_empty)
    monkeypatch.setattr(copy_runner, "process_lock", observe_release, raising=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(site.drain, [])
        try:
            assert empty.wait(2), "lock holder must confirm empty inside the write transaction"
            assert_writer_held()
        finally:
            release.set()
        assert future.result(timeout=5) == 0
    assert observations == ["unlocked while writer held"]

    monkeypatch.setattr(copy_runner, "process_lock", process_lock)
    assert site.run().exit_code == 0
    assert [row.status for row in site.history.deliveries()] == ["copied", "copied"]
