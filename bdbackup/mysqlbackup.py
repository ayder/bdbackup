"""Logical MySQL backup helpers using mysqldump."""

from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from bdbackup.backends import BackupError, BackupResult
from bdbackup.utils import mysql_cnf_file, redact_cmd, today_stamp


class MySQLBackup:
    """Create compressed logical MySQL backups with mysqldump."""

    def __init__(
        self,
        out_dir: str | Path = ".",
        user: str = "root",
        password: str | None = None,
        host: str = "localhost",
        port: int = 3306,
        options: Iterable[str] | None = None,
        jobs: int = 1,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.jobs = max(1, jobs)
        default_opts = [
            "--single-transaction",
            "--databases",
            "--routines",
            "--events",
            "--triggers",
        ]
        self.options = list(options) if options is not None else default_opts
        self.logger = logging.getLogger("bdbackup.mysql")

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
        """Dump a single database to a gzip-compressed SQL file."""
        out_filename = self.out_dir / f"{database or 'all-databases'}-{today_stamp()}.sql.gz"
        with mysql_cnf_file(
            user=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
        ) as defaults_file:
            cmd = self._build_cmd(database, defaults_file)
            self.logger.info(
                "Running: %s",
                " ".join(redact_cmd(cmd, [self.password])),
            )

            with (
                subprocess.Popen(  # noqa: S603
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                ) as proc,
                gzip.open(out_filename, "wb") as gz,
            ):
                if proc.stdout is None:
                    raise RuntimeError("mysqldump stdout was unexpectedly None")
                shutil.copyfileobj(proc.stdout, gz, 1024 * 1024)
                proc.wait()
                if proc.returncode != 0:
                    error = "mysqldump failed"
                    if proc.stdout:
                        error = proc.stdout.read().decode("utf-8", errors="replace")
                    self.logger.error("mysqldump failed for %s: %s", database, error)
                    out_filename.unlink(missing_ok=True)
                    raise subprocess.CalledProcessError(
                        proc.returncode, cmd, output=error
                    )

        self.logger.info("Wrote compressed dump to %s", out_filename)
        size_bytes = out_filename.stat().st_size if out_filename.exists() else 0
        return BackupResult(path=out_filename, size_bytes=size_bytes, success=True)

    def verify(self, result: BackupResult | None = None) -> BackupResult:
        """Verify the gzip dump is readable and non-empty."""
        dump = result.path if result else self.out_dir
        if not dump.exists():
            raise BackupError(f"Dump not found for verification: {dump}")
        if dump.stat().st_size == 0:
            raise BackupError(f"Dump is empty: {dump}")
        try:
            with gzip.open(dump, "rb") as gz:
                # Read a small trailer to ensure gzip structure is valid.
                gz.read(1024)
                gz.seek(-8, 2)
                gz.read()
        except (gzip.BadGzipFile, OSError) as exc:
            raise BackupError(f"Dump verification failed for {dump}: {exc}") from exc
        self.logger.info("Verified compressed dump %s", dump)
        return BackupResult(path=dump, size_bytes=dump.stat().st_size, success=True)

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
