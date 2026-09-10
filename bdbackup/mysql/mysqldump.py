"""Logical MySQL backup engine using mysqldump."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo, register_engine
from bdbackup.mysql.helpers import mysql_cnf_file
from bdbackup.utils import redact_cmd, today_stamp


class MySQLBackup:
    """Create compressed logical MySQL backups with mysqldump."""

    DEFAULT_OPTIONS = (
        "--single-transaction",
        "--databases",
        "--routines",
        "--events",
        "--triggers",
    )

    def __init__(
        self,
        out_dir: str | Path = ".",
        user: str = "root",
        password: str | None = None,
        host: str = "localhost",
        port: int = 3306,
        options: Iterable[str] | None = None,
        jobs: int = 1,
        database: str | None = None,
    ):
        self.out_dir = Path(out_dir).expanduser().absolute()
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.database = database
        if jobs < 1:
            raise ValueError("jobs must be >= 1")
        self.jobs = jobs
        self.options = list(dict.fromkeys([*self.DEFAULT_OPTIONS, *(options or [])]))
        if "--all-databases" in self.options:
            self.options.remove("--databases")
        self.logger = logging.getLogger("bdbackup.mysql.mysqldump")

    def _build_cmd(self, database: str | None, defaults_file: Path) -> list[str]:
        cmd = [
            "mysqldump",
            f"--defaults-extra-file={defaults_file}",
            *self.options,
        ]
        if database:
            cmd.append(database)
        return cmd

    def backup(self, database: str | None = None) -> BackupResult:
        """Stream only SQL to a private gzip file, then verify and publish it."""
        database = database if database is not None else self.database
        if database is not None and (
            not database or database.startswith("-") or "\x00" in database
        ):
            raise BackupError("Invalid database name")
        if not database and "--all-databases" not in self.options:
            raise BackupError("Specify a database or include --all-databases")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        label = quote(database or "all-databases", safe="")
        out_filename = self.out_dir / f"{label}-{today_stamp()}-{uuid4().hex}.sql.gz"
        fd, raw = tempfile.mkstemp(prefix=".bdbackup-dump-", suffix=".tmp", dir=self.out_dir)
        os.close(fd)
        pending = Path(raw)
        try:
            with (
                mysql_cnf_file(
                    user=self.user,
                    password=self.password,
                    host=self.host,
                    port=self.port,
                ) as defaults_file,
                tempfile.TemporaryFile() as errors,
            ):
                cmd = self._build_cmd(database, defaults_file)
                self.logger.info("Running: %s", " ".join(redact_cmd(cmd, [self.password])))
                with subprocess.Popen(  # noqa: S603
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                ) as proc:
                    try:
                        if proc.stdout is None:
                            raise BackupError("mysqldump stdout was unexpectedly None")
                        with gzip.open(pending, "wb") as gz:
                            shutil.copyfileobj(proc.stdout, gz, 1024 * 1024)
                        proc.wait()
                    except BaseException:
                        proc.kill()
                        proc.wait()
                        raise
                    errors.seek(max(0, errors.tell() - 65536))
                    diagnostic = errors.read().decode("utf-8", errors="replace")
                    if proc.returncode:
                        raise BackupError(f"mysqldump failed ({proc.returncode}): {diagnostic}")
                    if diagnostic:
                        self.logger.warning("mysqldump: %s", diagnostic.rstrip())
            self.verify(BackupResult(pending))
            with pending.open("rb") as stream:
                os.fsync(stream.fileno())
            # A unique name and exclusive hard-link publication never replace an older dump.
            os.link(pending, out_filename)
        finally:
            pending.unlink(missing_ok=True)
        return BackupResult(out_filename, size_bytes=out_filename.stat().st_size, success=True)

    def verify(self, result: BackupResult | None = None) -> BackupResult:
        """Read the entire gzip stream, including its checksum, and reject empty SQL."""
        if result is None:
            raise BackupError("A dump result is required for verification")
        dump = result.path
        try:
            with gzip.open(dump, "rb") as gz:
                if not gz.read(1):
                    raise BackupError(f"Dump contains no SQL: {dump}")
                while gz.read(1024 * 1024):
                    pass
        except (OSError, EOFError) as exc:
            raise BackupError(f"Dump verification failed for {dump}: {exc}") from exc
        return BackupResult(dump, size_bytes=dump.stat().st_size, success=True)

    def backup_all(self, databases: Iterable[str]) -> list[BackupResult]:
        """Dump multiple databases, optionally in parallel."""
        dbs = list(databases)
        if self.jobs == 1:
            return [self.backup(db) for db in dbs]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            return list(pool.map(self.backup, dbs))

    def prune(self) -> list[Path]:
        """mysqldump has no built-in retention policy; return an empty list."""
        return []


ENGINE = EngineInfo(
    name="mysqldump",
    backend=MySQLBackup,
    description="Logical backups via mysqldump (gzip-compressed SQL).",
    family="mysql",
)
register_engine(ENGINE)
