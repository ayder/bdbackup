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

from bdbackup.backends import BackupError, BackupResult
from bdbackup.ledger import LedgerError, checksum, unit_path


class HistoryError(BackupError):
    """History could not be read or written reliably."""


@dataclass(frozen=True)
class HistorySettings:
    database: Path
    restore_root: Path


def artifact_identity(path: Path) -> str:
    """Detect removed/replaced artifacts, including archives with reused names."""
    stat = path.stat()
    values = [stat.st_dev, stat.st_ino]
    if path.is_file():
        values.extend([stat.st_size, stat.st_mtime_ns])
    return json.dumps(values)


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
                if schema not in (0, 1, 2, 3, 4, 5) or (not create and schema == 0):
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
                    # Units recorded before this table managed every record of the unit.
                    connection.execute(
                        """INSERT INTO gfs_members (record_id, managed)
                           SELECT backup_runs.id, 1 FROM backup_runs
                           JOIN gfs_units ON gfs_units.unit = backup_runs.unit
                           WHERE backup_runs.status = 'success'"""
                    )
                    connection.execute("PRAGMA user_version = 5")
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
            digest = checksum(path)
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
        return result

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
