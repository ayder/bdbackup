"""Persistent backup run history. Connections are short-lived and process-safe."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING

from bdbackup.backends import BackupError, BackupResult
from bdbackup.ledger import LedgerError, checksum, unit_path

if TYPE_CHECKING:
    from bdbackup.ssh_copy import CopySettings


class HistoryError(BackupError):
    """History could not be read or written reliably."""


@dataclass(frozen=True)
class HistorySettings:
    database: Path
    restore_root: Path


def stat_identity(stat) -> str:
    """The identity of an artifact from its ``os.stat`` facts.

    ``stat`` is anything with ``st_mode``, ``st_dev``, ``st_ino``, ``st_size`` and
    ``st_mtime_ns``: an ``os.stat_result``, or what a metrics probe child read. A regular file's
    size and modification time are part of it, a directory's are not.
    """
    values = [stat.st_dev, stat.st_ino]
    if S_ISREG(stat.st_mode):
        values.extend([stat.st_size, stat.st_mtime_ns])
    return json.dumps(values)


def artifact_identity(path: Path) -> str:
    """Detect removed/replaced artifacts, including archives with reused names."""
    return stat_identity(path.stat())


@dataclass(frozen=True)
class BackupRecord:
    id: int
    job_name: str
    backup_type: str
    started_at: str
    completed_at: str | None
    status: str
    path: str | None
    size_bytes: int | None
    identity: str | None
    restore_info: str
    error: str | None
    encrypted: bool = False
    unit: str | None = None
    checksum: str | None = None

    @property
    def available(self) -> bool:
        if self.status != "success" or not self.path:
            return False
        try:
            return artifact_identity(Path(self.path)) == self.identity
        except OSError:
            return False


@dataclass(frozen=True)
class LastRun:
    """The outcome of a job's newest run that was not a dry run (schema 6, ``last_runs``).

    ``facts`` is the JSON object the job type stores: ``{"phases": {...}, <job-type keys>}``;
    it is kept from the last run that wrote some, and is ``None`` when none ever did.
    """

    job_name: str
    job_type: str
    finished_at: str
    exit_code: int
    result: str
    facts: dict | None = None


@dataclass(frozen=True)
class Delivery:
    """The delivery queued for a successful backup and its latest attempt."""

    record_id: int
    job_name: str
    destination: str
    method: str
    status: str
    failure_class: str | None
    attempt_count: int
    bytes_sent: int | None
    started_at: str | None
    ended_at: str | None


class History:
    """Record attempts before work starts; mark success only after verification."""

    def __init__(self, settings: HistorySettings):
        self.settings = settings

    @contextmanager
    def _connect(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        path = self.settings.database
        connection = None
        try:
            if create:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            elif not path.exists():
                raise HistoryError(f"History database does not exist: {path}")
            mode = "rw" if create else "ro"
            connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=30)
            connection.row_factory = sqlite3.Row
            with connection:
                if create:
                    connection.execute("BEGIN IMMEDIATE")
                schema = connection.execute("PRAGMA user_version").fetchone()[0]
                if schema not in (0, 1, 2, 3, 4, 5, 6, 7) or (not create and schema == 0):
                    raise HistoryError(f"Unsupported history schema version: {schema}")
                if create and schema == 0:
                    connection.execute(
                        """CREATE TABLE backup_runs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            job_name TEXT NOT NULL,
                            backup_type TEXT NOT NULL,
                            started_at TEXT NOT NULL,
                            completed_at TEXT,
                            status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed')),
                            path TEXT,
                            size_bytes INTEGER,
                            identity TEXT,
                            restore_info TEXT NOT NULL,
                            error TEXT
                        )"""
                    )
                    connection.execute(
                        "CREATE INDEX backup_runs_job ON backup_runs(job_name, id DESC)"
                    )
                    connection.execute("PRAGMA user_version = 1")
                if create and schema in (0, 1):
                    connection.execute(
                        "ALTER TABLE backup_runs ADD COLUMN encrypted INTEGER NOT NULL "
                        "DEFAULT 0 CHECK(encrypted IN (0, 1))"
                    )
                    connection.execute("PRAGMA user_version = 2")
                if create and schema in (0, 1, 2):
                    # Ledger facts (spec 2): the unit GFS moves and its plain-hex checksum.
                    connection.execute("ALTER TABLE backup_runs ADD COLUMN unit TEXT")
                    connection.execute("ALTER TABLE backup_runs ADD COLUMN checksum TEXT")
                    connection.execute("PRAGMA user_version = 3")
                if create and schema in (0, 1, 2, 3):
                    # GFS (spec 2): managed units, where their copies are, and every step.
                    connection.execute(
                        """CREATE TABLE gfs_units (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            unit TEXT NOT NULL UNIQUE,
                            series TEXT NOT NULL,
                            relative TEXT NOT NULL,
                            deleted_at TEXT
                        )"""
                    )
                    connection.execute(
                        """CREATE TABLE gfs_locations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            unit_id INTEGER NOT NULL REFERENCES gfs_units(id),
                            stage_path TEXT NOT NULL,
                            path TEXT NOT NULL UNIQUE
                        )"""
                    )
                    connection.execute(
                        """CREATE TABLE gfs_steps (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            run_at TEXT NOT NULL,
                            gfs_job TEXT NOT NULL,
                            unit_id INTEGER REFERENCES gfs_units(id),
                            action TEXT NOT NULL,
                            source TEXT,
                            destination TEXT,
                            outcome TEXT NOT NULL,
                            reason TEXT
                        )"""
                    )
                    connection.execute("PRAGMA user_version = 4")
                if create and schema in (0, 1, 2, 3, 4):
                    # Restore and snapshots (spec 2 PR 3): the identity of each member at a
                    # copied location, and each record's S2 judgment when GFS first saw it.
                    connection.execute("ALTER TABLE gfs_locations ADD COLUMN identities TEXT")
                    connection.execute(
                        """CREATE TABLE gfs_members (
                            record_id INTEGER PRIMARY KEY REFERENCES backup_runs(id),
                            managed INTEGER NOT NULL CHECK(managed IN (0, 1))
                        )"""
                    )
                    # One row per applying GFS run, and the run each step belongs to (r8 R1).
                    connection.execute(
                        """CREATE TABLE gfs_runs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            gfs_job TEXT NOT NULL,
                            started_at TEXT NOT NULL,
                            finished_at TEXT,
                            status TEXT NOT NULL
                                CHECK(status IN ('running', 'completed', 'failed')),
                            exit_code INTEGER,
                            moved INTEGER,
                            deleted INTEGER,
                            held INTEGER,
                            deferred INTEGER,
                            refused INTEGER,
                            failed INTEGER,
                            unmanaged INTEGER
                        )"""
                    )
                    connection.execute(
                        "ALTER TABLE gfs_steps ADD COLUMN run_id INTEGER REFERENCES gfs_runs(id)"
                    )
                    # Units recorded before this table managed every record of the unit.
                    connection.execute(
                        """INSERT INTO gfs_members (record_id, managed)
                           SELECT backup_runs.id, 1 FROM backup_runs
                           JOIN gfs_units ON gfs_units.unit = backup_runs.unit
                           WHERE backup_runs.status = 'success'"""
                    )
                    connection.execute("PRAGMA user_version = 5")
                if create and schema in (0, 1, 2, 3, 4, 5):
                    # Metrics (0.7.3): the outcome of each job's newest run that was not a dry
                    # run, in one row per job; ``facts`` is the JSON its job type keeps.
                    connection.execute(
                        """CREATE TABLE last_runs (
                            job_name TEXT PRIMARY KEY,
                            job_type TEXT NOT NULL,
                            finished_at TEXT NOT NULL,
                            exit_code INTEGER NOT NULL,
                            result TEXT NOT NULL,
                            facts TEXT
                        )"""
                    )
                    connection.execute("PRAGMA user_version = 6")
                if create and schema in (0, 1, 2, 3, 4, 5, 6):
                    connection.execute(
                        """CREATE TABLE deliveries (
                            record_id INTEGER PRIMARY KEY REFERENCES backup_runs(id),
                            job_name TEXT NOT NULL,
                            destination TEXT NOT NULL,
                            method TEXT NOT NULL CHECK(method IN ('rsync', 'scp')),
                            status TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'copied', 'failed', 'abandoned')),
                            failure_class TEXT CHECK(failure_class IN (
                                'connect', 'destination_not_ready', 'tool_missing', 'name_taken',
                                'transfer', 'checksum_mismatch', 'source_unavailable', 'other'
                            )),
                            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                            bytes_sent INTEGER CHECK(bytes_sent >= 0),
                            started_at TEXT,
                            ended_at TEXT,
                            CHECK(
                                (status IN ('failed', 'abandoned')) = (failure_class IS NOT NULL)
                            ),
                            CHECK((status = 'copied') = (bytes_sent IS NOT NULL))
                        )"""
                    )
                    connection.execute("PRAGMA user_version = 7")
                yield connection
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"Cannot access history database {path}: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def run(
        self,
        job_name: str,
        backup_type: str,
        action: Callable[[], BackupResult],
        restore_info: dict[str, str] | None = None,
        *,
        encrypted: bool = False,
        copy: CopySettings | None = None,
    ) -> BackupResult:
        with self._connect(create=True) as db:
            cursor = db.execute(
                """INSERT INTO backup_runs
                   (job_name, backup_type, started_at, status, restore_info, encrypted)
                   VALUES (?, ?, ?, 'running', ?, ?)""",
                (job_name, backup_type, datetime.now(UTC).isoformat(),
                 json.dumps(restore_info or {}), int(encrypted)),
            )
            run_id = cursor.lastrowid
        try:
            result = action()
            if not result.success:
                raise BackupError("Backend reported an unsuccessful backup")
            path = result.path.resolve(strict=True)
            identity = artifact_identity(path)
            unit = unit_path(path, restore_info or {})
            digest = result.checksum or checksum(path)
        except BaseException as exc:
            # Store the exception class only: external diagnostics may contain credentials.
            with self._connect(create=True) as db:
                db.execute(
                    "UPDATE backup_runs SET status='failed', completed_at=?, error=? WHERE id=?",
                    (datetime.now(UTC).isoformat(), type(exc).__name__, run_id),
                )
            raise
        with self._connect(create=True) as db:
            db.execute(
                """UPDATE backup_runs SET status='success', completed_at=?, path=?,
                   size_bytes=?, identity=?, restore_info=?, unit=?, checksum=? WHERE id=?""",
                (datetime.now(UTC).isoformat(), str(path), result.size_bytes, identity,
                 json.dumps(restore_info or {}), str(unit), digest, run_id),
            )
            if copy is not None:
                db.execute(
                    """INSERT INTO deliveries (record_id, job_name, destination, method)
                       VALUES (?, ?, ?, ?)""",
                    (run_id, job_name, copy.destination, copy.method),
                )
        return result

    def deliveries(
        self, *, job: str | None = None, destination: str | None = None, uncopied: bool = False,
    ) -> list[Delivery]:
        """Read deliveries in backup record order, optionally restricted to the retry queue."""
        if not self.settings.database.exists():
            return []
        with self._connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] < 7:
                return []
            rows = db.execute(
                """SELECT * FROM deliveries
                   WHERE (? IS NULL OR job_name = ?)
                     AND (? IS NULL OR destination = ?)
                     AND (? = 0 OR status IN ('pending', 'failed'))
                   ORDER BY record_id DESC""",
                (job, job, destination, destination, uncopied),
            ).fetchall()
        return [Delivery(**dict(row)) for row in rows]

    def next_uncopied(
        self, job: str, destination: str, attempted: set[int], *,
        db: sqlite3.Connection | None = None,
    ) -> int | None:
        return None

    def start_delivery_attempt(self, record_id: int, method: str, started_at: datetime) -> int:
        """Record the method and start time while keeping the delivery eligible for retry."""
        with self._connect(create=True) as db:
            result = db.execute(
                """UPDATE deliveries SET method = ?, attempt_count = attempt_count + 1,
                   started_at = ?, ended_at = NULL
                   WHERE record_id = ? AND status IN ('pending', 'failed')""",
                (method, started_at.isoformat(), record_id),
            )
            if not result.rowcount:
                raise HistoryError(f"Backup ID {record_id} has no uncopied delivery")
            return db.execute(
                "SELECT attempt_count FROM deliveries WHERE record_id = ?", (record_id,),
            ).fetchone()[0]

    def finish_delivery_attempt(
        self, record_id: int, attempt: int, status: str, ended_at: datetime, *,
        failure_class: str | None = None, bytes_sent: int | None = None,
    ) -> None:
        """Record the outcome only if this is still the current unfinished attempt."""
        if status not in {"copied", "failed", "abandoned"}:
            raise HistoryError("Invalid delivery attempt outcome")
        with self._connect(create=True) as db:
            result = db.execute(
                """UPDATE deliveries SET status = ?, failure_class = ?, bytes_sent = ?, ended_at = ?
                   WHERE record_id = ? AND attempt_count = ? AND started_at IS NOT NULL
                     AND ended_at IS NULL AND status IN ('pending', 'failed')""",
                (status, failure_class, bytes_sent, ended_at.isoformat(), record_id, attempt),
            )
            if not result.rowcount:
                raise HistoryError(
                    f"Delivery attempt {attempt} for backup {record_id} is stale or missing"
                )

    def record_checksums(self, job: str | None = None) -> list[tuple[int, str]]:
        """Record unit and checksum for successful records that have none, oldest first.

        A checksum already recorded is never changed, even if one is written meanwhile.
        """
        with self._connect(create=True) as db:
            rows = db.execute(
                """SELECT * FROM backup_runs
                   WHERE status='success' AND checksum IS NULL AND (? IS NULL OR job_name=?)
                   ORDER BY id""",
                (job, job),
            ).fetchall()
        outcomes = []
        for record in (BackupRecord(**dict(row)) for row in rows):
            if not record.available:
                outcomes.append((record.id, "skipped: unavailable"))
                continue
            path = Path(record.path)
            try:
                unit = unit_path(path, json.loads(record.restore_info))
                digest = checksum(path)
            except (LedgerError, OSError):
                outcomes.append((record.id, "skipped: unexpected unit"))
                continue
            with self._connect(create=True) as db:
                db.execute(
                    "UPDATE backup_runs SET unit=?, checksum=? WHERE id=? AND checksum IS NULL",
                    (str(unit), digest, record.id),
                )
            outcomes.append((record.id, "recorded"))
        return outcomes

    def record_last_run(
        self, job_name: str, job_type: str, exit_code: int, result: str,
        facts: dict | None = None,
    ) -> None:
        """Set the outcome of ``job_name``'s run that ends now; one row per job.

        ``facts`` replaces the stored facts; ``None`` keeps them, so a night with nothing to
        export leaves what the last export recorded.
        """
        with self._connect(create=True) as db:
            db.execute(
                """INSERT INTO last_runs (job_name, job_type, finished_at, exit_code, result, facts)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_name) DO UPDATE SET
                       job_type=excluded.job_type, finished_at=excluded.finished_at,
                       exit_code=excluded.exit_code, result=excluded.result,
                       facts=COALESCE(excluded.facts, last_runs.facts)""",
                (job_name, job_type, datetime.now(UTC).isoformat(), exit_code, result,
                 None if facts is None else json.dumps(facts)),
            )

    def last_runs(self) -> dict[str, LastRun]:
        """Every job's last run; read-only.

        Empty when the database does not exist or is older than schema 6, which reading never
        migrates.
        """
        if not self.settings.database.exists():
            return {}
        with self._connect() as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='last_runs'"
            ).fetchone() is None:
                return {}
            rows = db.execute("SELECT * FROM last_runs ORDER BY job_name").fetchall()
        runs = {}
        for row in rows:
            try:
                facts = json.loads(row["facts"]) if row["facts"] is not None else None
            except ValueError:
                facts = None
            runs[row["job_name"]] = LastRun(
                row["job_name"], row["job_type"], row["finished_at"], row["exit_code"],
                row["result"], facts if isinstance(facts, dict) else None,
            )
        return runs

    def records(self, *, job: str | None = None, successful: bool = False) -> list[BackupRecord]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM backup_runs
                   WHERE (? IS NULL OR job_name=?) AND (?=0 OR status='success')
                   ORDER BY id DESC""",
                (job, job, int(successful)),
            ).fetchall()
        return [BackupRecord(**dict(row)) for row in rows]

    def get(self, run_id: int) -> BackupRecord:
        with self._connect() as db:
            row = db.execute("SELECT * FROM backup_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HistoryError(f"Backup ID {run_id} was not found")
        return BackupRecord(**dict(row))
