"""Logical MySQL backup helpers using mysqldump."""

from __future__ import annotations

import gzip
import subprocess
from collections.abc import Iterable
from pathlib import Path

from bdbackup.utils import setup_logging, today_stamp


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
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.options = list(options or ["--single-transaction", "--databases"])
        self.logger = setup_logging("bdbackup.mysql")

    def _build_cmd(self, database: str) -> list[str]:
        cmd = [
            "mysqldump",
            f"--host={self.host}",
            f"--port={self.port}",
            f"--user={self.user}",
        ]
        if self.password:
            cmd.append(f"--password={self.password}")
        cmd.extend(self.options)
        cmd.append(database)
        return cmd

    def backup(self, database: str) -> Path:
        """Dump a single database to a gzip-compressed SQL file."""
        out_filename = self.out_dir / f"{database}-{today_stamp()}.sql.gz"
        cmd = self._build_cmd(database)
        self.logger.info("Running: %s", " ".join(cmd))

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=False,
        )
        if proc.returncode != 0:
            error = proc.stdout.decode("utf-8", errors="replace")
            self.logger.error("mysqldump failed for %s: %s", database, error)
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=error)

        with gzip.open(out_filename, "wb") as f:
            f.write(proc.stdout)
        self.logger.info("Wrote compressed dump to %s", out_filename)
        return out_filename

    def backup_all(self, databases: Iterable[str]) -> list[Path]:
        """Dump multiple databases."""
        return [self.backup(db) for db in databases]
