"""Physical MySQL/MariaDB backup engine using xtrabackup / mariabackup.

This engine targets the legacy Percona XtraBackup / MariaDB mariabackup
interface. Versioned variants (e.g. xtrabackup_80.py / xtrabackup_84.py /
xtrabackup_97.py) can be dropped in as new modules next to this file without
editing any existing code — they self-register under their own names.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo, register_engine
from bdbackup.mysql.helpers import mysql_cnf_file
from bdbackup.utils import process_lock, redact_cmd


class XtraBackup:
    """Run full or incremental physical backups with xtrabackup / mariabackup (legacy)."""

    def __init__(
        self,
        backup_root: str | Path,
        user: str = "xtrabackup",
        password: str | None = None,
        binary: str = "xtrabackup",
        compress: str = "zstd",
        compress_threads: int = 4,
        parallel: int = 1,
        throttle: int | None = None,
        retention_days: int = 5,
    ):
        self.backup_root = Path(backup_root)
        self.user = user
        self.password = password
        self.binary = binary
        self.compress = compress
        self.compress_threads = compress_threads
        self.parallel = max(1, parallel)
        self.throttle = throttle
        self.retention_days = retention_days
        self.logger = logging.getLogger("bdbackup.mysql.xtrabackup")

    @property
    def date_dir(self) -> Path:
        return self.backup_root / datetime.now(UTC).strftime("%Y-%m-%d")

    def _utc_now(self) -> datetime:
        return datetime.now(UTC)

    def _latest_full_date_dir(self) -> Path | None:
        """Return the most recent daily directory containing a successful full backup."""
        if not self.backup_root.exists():
            return None
        candidates: list[Path] = []
        for item in self.backup_root.iterdir():
            if item.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.name):
                if (item / ".full_success").exists():
                    candidates.append(item)
        if not candidates:
            return None
        return sorted(candidates)[-1]

    def _base_cmd(
        self, target_dir: Path, defaults_file: Path, incremental_basedir: Path | None = None
    ) -> list[str]:
        cmd = [
            self.binary,
            "--backup",
            f"--compress={self.compress}" if self.compress else "",
            f"--compress-threads={self.compress_threads}",
            f"--target-dir={target_dir}",
            f"--defaults-extra-file={defaults_file}",
        ]
        if self.parallel > 1:
            cmd.append(f"--parallel={self.parallel}")
        if self.throttle:
            cmd.append(f"--throttle={self.throttle}")
        if incremental_basedir:
            cmd.append(f"--incremental-basedir={incremental_basedir}")
        return [arg for arg in cmd if arg]

    def _run(self, cmd: list[str]) -> None:
        self.logger.info(
            "Running: %s",
            " ".join(redact_cmd(cmd, [self.password])),
        )
        proc = subprocess.run(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
        )
        if proc.returncode != 0:
            self.logger.error("%s failed: %s", self.binary, proc.stdout)
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout)
        self.logger.info("%s completed successfully", self.binary)

    @staticmethod
    def _read_to_lsn(checkpoints: Path) -> int | None:
        """Parse the to_lsn value from an xtrabackup_checkpoints file."""
        if not checkpoints.exists():
            return None
        try:
            for line in checkpoints.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("to_lsn"):
                    value = line.split("=", 1)[1].strip()
                    return int(value)
        except (OSError, ValueError, IndexError):
            return None
        return None

    def _valid_base(self, path: Path) -> bool:
        """Return True if *path* contains a non-empty xtrabackup_checkpoints."""
        checkpoints = path / "xtrabackup_checkpoints"
        return self._read_to_lsn(checkpoints) is not None

    def backup(self, name: str | None = None) -> BackupResult:
        """Run a full physical backup by default.

        *name* is ignored; it exists only to satisfy the BackupBackend protocol.
        """
        return self.full_backup()

    def full_backup(self, *, acquire_lock: bool = True) -> BackupResult:
        """Run a full physical backup."""
        lock_path = self.backup_root / ".bdbackup.lock"
        lock_ctx = process_lock(lock_path) if acquire_lock else nullcontext()
        with lock_ctx:
            self.date_dir.mkdir(parents=True, exist_ok=True)
            time = self._utc_now().strftime("%H%M%S")
            target = self.date_dir / f"Full_{time}"
            tmp_target = Path(str(target) + ".tmp")
            tmp_target.mkdir(parents=True, exist_ok=True)

            with mysql_cnf_file(user=self.user, password=self.password) as defaults_file:
                try:
                    self._run(self._base_cmd(tmp_target, defaults_file))
                    tmp_target.rename(target)
                except subprocess.CalledProcessError:
                    shutil.rmtree(tmp_target, ignore_errors=True)
                    raise

            latest_link = self.date_dir / "Full_Latest"
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()
            latest_link.symlink_to(target, target_is_directory=True)
            (self.date_dir / ".full_success").touch()

            self.prune()

        size_bytes = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        return BackupResult(path=target, size_bytes=size_bytes, success=True)

    def incremental_backup(self, *, acquire_lock: bool = True) -> BackupResult:
        """Run an incremental physical backup based on the latest valid base."""
        lock_path = self.backup_root / ".bdbackup.lock"
        lock_ctx = process_lock(lock_path) if acquire_lock else nullcontext()
        with lock_ctx:
            full_date_dir = self._latest_full_date_dir()
            if full_date_dir is None:
                raise RuntimeError("No successful full backup found; run full backup first")

            latest_link = full_date_dir / "Full_Latest"
            basedir: Path | None = None

            inc_root = full_date_dir / "Incremental"
            if inc_root.exists():
                for candidate in sorted(inc_root.iterdir(), reverse=True):
                    if self._valid_base(candidate):
                        basedir = candidate
                        break

            if not basedir:
                resolved_full = latest_link.resolve()
                if self._valid_base(resolved_full):
                    basedir = resolved_full
                else:
                    raise RuntimeError("No valid base directory with xtrabackup_checkpoints found")

            time = self._utc_now().strftime("%H%M%S")
            target = inc_root / time
            tmp_target = Path(str(target) + ".tmp")
            tmp_target.mkdir(parents=True, exist_ok=True)

            with mysql_cnf_file(user=self.user, password=self.password) as defaults_file:
                try:
                    self._run(self._base_cmd(tmp_target, defaults_file, basedir))
                    tmp_target.rename(target)
                except subprocess.CalledProcessError:
                    shutil.rmtree(tmp_target, ignore_errors=True)
                    raise

            size_bytes = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            return BackupResult(path=target, size_bytes=size_bytes, success=True)

    def prepare(self, target: str | Path) -> Path:
        """Run --prepare on a backup directory to make it restorable."""
        target_path = Path(target)
        if not target_path.exists():
            raise BackupError(f"Backup directory not found: {target_path}")
        with mysql_cnf_file(user=self.user, password=self.password) as defaults_file:
            cmd = [
                self.binary,
                "--prepare",
                f"--target-dir={target_path}",
                f"--defaults-extra-file={defaults_file}",
            ]
            self._run(cmd)
        self.logger.info("Prepared backup at %s", target_path)
        return target_path

    def verify(self, result: BackupResult | None = None) -> BackupResult:
        """Verify a backup directory contains xtrabackup_checkpoints and a non-zero to_lsn."""
        target = result.path if result else self.date_dir / "Full_Latest"
        if target.is_symlink():
            target = target.resolve()
        if not target.exists():
            raise BackupError(f"Backup directory not found for verification: {target}")
        checkpoints = target / "xtrabackup_checkpoints"
        to_lsn = self._read_to_lsn(checkpoints)
        if to_lsn is None or to_lsn <= 0:
            raise BackupError(f"Invalid xtrabackup_checkpoints in {target}: to_lsn={to_lsn}")
        size_bytes = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        self.logger.info("Verified backup %s with to_lsn=%s", target, to_lsn)
        return BackupResult(path=target, size_bytes=size_bytes, success=True)

    def prune(self) -> list[Path]:
        """Remove daily backup directories older than retention_days.

        Uses the date encoded in the directory name rather than mtime.
        """
        removed: list[Path] = []
        if not self.backup_root.exists():
            return removed

        cutoff = self._utc_now() - timedelta(days=self.retention_days)
        for item in self.backup_root.iterdir():
            if not item.is_dir():
                continue
            match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", item.name)
            if not match:
                continue
            item_date = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=UTC
            )
            if item_date < cutoff:
                self.logger.info("Removing old backup directory: %s", item)
                shutil.rmtree(item)
                removed.append(item)
        return removed


ENGINE = EngineInfo(
    name="xtrabackup",
    backend=XtraBackup,
    description=(
        "Physical backups via legacy Percona XtraBackup / MariaDB mariabackup. "
        "Versioned variants (xtrabackup80/84/97) self-register as separate engines."
    ),
    family="mysql",
)
register_engine(ENGINE)
