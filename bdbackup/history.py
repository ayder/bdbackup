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
                if schema not in (0, 1) or (not create and schema != 1):
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
    ) -> BackupResult:
        with self._connect(create=True) as db:
            cursor = db.execute(
                """INSERT INTO backup_runs
                   (job_name, backup_type, started_at, status, restore_info)
                   VALUES (?, ?, ?, 'running', ?)""",
                (job_name, backup_type, datetime.now(UTC).isoformat(),
                 json.dumps(restore_info or {})),
            )
            run_id = cursor.lastrowid
        try:
            result = action()
            if not result.success:
                raise BackupError("Backend reported an unsuccessful backup")
            path = result.path.resolve(strict=True)
            identity = artifact_identity(path)
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
                   size_bytes=?, identity=?, restore_info=? WHERE id=?""",
                (datetime.now(UTC).isoformat(), str(path), result.size_bytes, identity,
                 json.dumps(restore_info or {}), run_id),
            )
        return result

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
