"""Physical MySQL/MariaDB backup helpers using xtrabackup / mariabackup."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from bdbackup.utils import setup_logging, today_stamp


class XtraBackup:
    """Run full or incremental physical backups with xtrabackup / mariabackup."""

    def __init__(
        self,
        backup_root: str | Path,
        user: str = "xtrabackup",
        password: str | None = None,
        binary: str = "xtrabackup",
        compress: str = "zstd",
        compress_threads: int = 4,
        retention_days: int = 5,
    ):
        self.backup_root = Path(backup_root)
        self.user = user
        self.password = password
        self.binary = binary
        self.compress = compress
        self.compress_threads = compress_threads
        self.retention_days = retention_days
        self.logger = setup_logging("bdbackup.xtrabackup")

    @property
    def date_dir(self) -> Path:
        from datetime import datetime

        return self.backup_root / datetime.now().strftime("%Y-%m-%d")

    def _base_cmd(self, target_dir: Path, incremental_basedir: Path | None = None) -> list[str]:
        cmd = [
            self.binary,
            "--backup",
            f"--compress={self.compress}" if self.compress else "",
            f"--compress-threads={self.compress_threads}",
            f"--target-dir={target_dir}",
            f"--user={self.user}",
        ]
        if incremental_basedir:
            cmd.append(f"--incremental-basedir={incremental_basedir}")
        if self.password:
            cmd.append(f"--password={self.password}")
        return [arg for arg in cmd if arg]

    def _run(self, cmd: list[str]) -> None:
        self.logger.info("Running: %s", " ".join(cmd))
        proc = subprocess.run(
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

    def full_backup(self) -> Path:
        """Run a full physical backup."""
        self.date_dir.mkdir(parents=True, exist_ok=True)
        time = today_stamp().split("-", 2)[2].replace("-", "")
        target = self.date_dir / f"Full_{time}"
        target.mkdir(parents=True, exist_ok=True)

        self._run(self._base_cmd(target))

        latest_link = self.date_dir / "Full_Latest"
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(target, target_is_directory=True)
        (self.date_dir / ".full_success").touch()

        self._cleanup()
        return target

    def incremental_backup(self) -> Path:
        """Run an incremental physical backup based on the latest valid base."""
        flag = self.date_dir / ".full_success"
        if not flag.exists():
            raise RuntimeError("No successful full backup found for today; run full backup first")

        latest_link = self.date_dir / "Full_Latest"
        basedir: Path | None = None

        inc_root = self.date_dir / "Incremental"
        if inc_root.exists():
            for candidate in sorted(inc_root.iterdir(), reverse=True):
                if (candidate / "xtrabackup_checkpoints").exists():
                    basedir = candidate
                    break

        if not basedir:
            if (latest_link / "xtrabackup_checkpoints").exists():
                basedir = latest_link.resolve()
            else:
                raise RuntimeError("No valid base directory with xtrabackup_checkpoints found")

        time = today_stamp().split("-", 2)[2].replace("-", "")
        target = inc_root / time
        target.mkdir(parents=True, exist_ok=True)

        self._run(self._base_cmd(target, basedir))
        return target

    def _cleanup(self) -> None:
        """Remove daily backup directories older than retention_days."""
        if not self.backup_root.exists():
            return
        import time

        now = time.time()
        cutoff = now - (self.retention_days * 86400)
        for item in self.backup_root.iterdir():
            if item.is_dir() and item.stat().st_mtime < cutoff:
                self.logger.info("Removing old backup directory: %s", item)
                shutil.rmtree(item, ignore_errors=True)
