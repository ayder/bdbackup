"""Verified physical backups with explicit incremental dependencies."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo, register_engine
from bdbackup.mysql.helpers import mysql_cnf_file
from bdbackup.utils import process_lock, redact_cmd


class XtraBackup:
    """Run XtraBackup/mariabackup and retain full dependency chains."""

    METADATA = "bdbackup.json"

    def __init__(
        self,
        backup_root: str | Path,
        user: str = "xtrabackup",
        password: str | None = None,
        binary: str = "xtrabackup",
        compress: str = "",
        compress_threads: int = 4,
        parallel: int = 1,
        throttle: int | None = None,
        retention_days: int = 5,
    ):
        self.backup_root = Path(backup_root).expanduser().resolve()
        self.user = user
        self.password = password
        self.binary = binary
        self.compress = compress
        self.is_mariadb = Path(binary).name in {"mariabackup", "mariadb-backup"}
        if self.is_mariadb and compress not in {"", "quicklz"}:
            raise ValueError("mariabackup supports only quicklz compression; omit for uncompressed")
        self.compress_threads = compress_threads
        self.parallel = parallel
        self.throttle = throttle
        self.retention_days = retention_days
        if retention_days < 1 or parallel < 1 or compress_threads < 1:
            raise ValueError("retention_days, parallel and compress_threads must be >= 1")
        if throttle is not None and throttle < 1:
            raise ValueError("throttle must be >= 1")
        self.logger = logging.getLogger("bdbackup.mysql.xtrabackup")

    @property
    def date_dir(self) -> Path:
        return self.backup_root / self._utc_now().strftime("%Y-%m-%d")

    @property
    def lock_path(self) -> Path:
        return self.backup_root / ".bdbackup.lock"

    def _utc_now(self) -> datetime:
        return datetime.now(UTC)

    def _identity(self) -> str:
        return f"{self._utc_now():%Y%m%dT%H%M%S%f}-{uuid4().hex}"

    def _latest_full_date_dir(self) -> Path | None:
        if not self.backup_root.exists():
            return None
        candidates = [
            p
            for p in self.backup_root.iterdir()
            if (
                not p.is_symlink()
                and p.is_dir()
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
                and (p / ".full_success").is_file()
            )
        ]
        return max(candidates, default=None)

    def _latest_full(self) -> Path:
        day = self._latest_full_date_dir()
        if day is None:
            raise BackupError("No successful full backup found; run full backup first")
        full = (day / "Full_Latest").resolve()
        if full.parent != day or not full.name.startswith("Full_") or not self._valid_base(full):
            raise BackupError("Latest full is missing or invalid; run a new full backup")
        return full

    def _base_cmd(
        self,
        target_dir: Path,
        defaults_file: Path,
        incremental_basedir: Path | None = None,
    ) -> list[str]:
        cmd = [
            self.binary,
            f"--defaults-extra-file={defaults_file}",
            "--backup",
            f"--target-dir={target_dir}",
        ]
        if self.compress:
            cmd.extend(
                [f"--compress={self.compress}", f"--compress-threads={self.compress_threads}"]
            )
        if self.parallel > 1:
            cmd.append(f"--parallel={self.parallel}")
        if self.throttle:
            cmd.append(f"--throttle={self.throttle}")
        if incremental_basedir:
            cmd.append(f"--incremental-basedir={incremental_basedir}")
        return cmd

    def _run(self, cmd: list[str]) -> None:
        self.logger.info("Running: %s", " ".join(redact_cmd(cmd, [self.password])))
        # Keep large backup logs out of memory.
        with tempfile.TemporaryFile(mode="w+") as output:
            proc = subprocess.run(  # noqa: S603
                cmd,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
                text=True,
            )
            if proc.returncode:
                output.seek(0, os.SEEK_END)
                output.seek(max(0, output.tell() - 65536))
                raise BackupError(f"{self.binary} failed ({proc.returncode}): {output.read()}")

    @staticmethod
    def _read_to_lsn(checkpoints: Path) -> int | None:
        try:
            values = dict(
                line.split("=", 1) for line in checkpoints.read_text().splitlines() if "=" in line
            )
            return int({key.strip(): value.strip() for key, value in values.items()}["to_lsn"])
        except (OSError, ValueError, KeyError):
            return None

    @staticmethod
    def _checkpoints(target: Path) -> dict:
        checkpoint = target / "mariadb_backup_checkpoints"
        if not checkpoint.exists():
            checkpoint = target / "xtrabackup_checkpoints"
        try:
            values = dict(
                line.split("=", 1) for line in checkpoint.read_text().splitlines() if "=" in line
            )
            values = {key.strip(): value.strip() for key, value in values.items()}
            start, end = int(values["from_lsn"]), int(values["to_lsn"])
            kind = values["backup_type"]
            if start < 0 or end <= 0 or end < start:
                raise ValueError("invalid LSN range")
            if kind not in {"full-backuped", "incremental", "log-applied", "full-prepared"}:
                raise ValueError("unknown backup type")
            return {"from_lsn": start, "to_lsn": end, "backup_type": kind}
        except (OSError, ValueError, KeyError) as exc:
            raise BackupError(f"Invalid checkpoints in {target}: {exc}") from exc

    def _valid_base(self, path: Path) -> bool:
        try:
            return self._checkpoints(path)["backup_type"] in {"full-backuped", "incremental"}
        except BackupError:
            return False

    def _metadata(self, path: Path) -> dict:
        try:
            data = json.loads((path / self.METADATA).read_text())
            checkpoints = self._checkpoints(path)
            if (
                data["schema"] != 1
                or data["from_lsn"] != checkpoints["from_lsn"]
                or data["to_lsn"] != checkpoints["to_lsn"]
            ):
                raise ValueError("metadata/checkpoints disagree")
            if data["kind"] not in {"full", "incremental"}:
                raise ValueError("invalid backup kind")
            return data
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BackupError(f"Invalid dependency metadata in {path}: {exc}") from exc

    def _chain(self, full: Path) -> list[Path]:
        """Resolve the single committed chain belonging to this full, never by wall clock."""
        root = full.parent / "Incremental" / full.name
        if not root.exists():
            return []
        expected_full = str(full.relative_to(self.backup_root))
        children: dict[str, Path] = {}
        count = 0
        for candidate in root.iterdir():
            if candidate.name.endswith(".tmp") or candidate.name.startswith("."):
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                raise BackupError(f"Unexpected incremental entry: {candidate}")
            meta = self._metadata(candidate)
            if meta.get("full") != expected_full or meta["kind"] != "incremental":
                raise BackupError(f"Incremental belongs to a different full: {candidate}")
            parent = meta.get("parent")
            if not isinstance(parent, str) or parent in children:
                raise BackupError(f"Ambiguous incremental parent: {candidate}")
            children[parent] = candidate
            count += 1
        chain = []
        parent = full
        while str(parent.relative_to(self.backup_root)) in children:
            child = children.pop(str(parent.relative_to(self.backup_root)))
            if self._checkpoints(child)["from_lsn"] != self._checkpoints(parent)["to_lsn"]:
                raise BackupError(f"Broken incremental LSN chain: {child}")
            if not self._valid_base(child):
                raise BackupError(f"Incremental has already been prepared: {child}")
            chain.append(child)
            parent = child
        if len(chain) != count:
            raise BackupError("Incremental chain has missing parents or cycles")
        return chain

    def _create(self, target: Path, full: Path | None = None, parent: Path | None = None):
        pending = Path(str(target) + ".tmp")
        pending.mkdir(parents=True, mode=0o700)
        try:
            with mysql_cnf_file(user=self.user, password=self.password) as defaults:
                self._run(self._base_cmd(pending, defaults, parent))
            self.verify(BackupResult(pending))
            checkpoints = self._checkpoints(pending)
            if parent is not None:
                if (
                    checkpoints["backup_type"] != "incremental"
                    or checkpoints["from_lsn"] != self._checkpoints(parent)["to_lsn"]
                ):
                    raise BackupError("New incremental does not continue its selected parent")
            elif checkpoints["backup_type"] != "full-backuped" or checkpoints["from_lsn"] != 0:
                raise BackupError("Full backup did not produce full-backuped checkpoints")
            metadata = {
                "schema": 1,
                "kind": "incremental" if parent else "full",
                "full": str((full or target).relative_to(self.backup_root)),
                "parent": str(parent.relative_to(self.backup_root)) if parent else None,
                "created": self._utc_now().isoformat(),
                **checkpoints,
            }
            (pending / self.METADATA).write_text(json.dumps(metadata, indent=2) + "\n")
            pending.rename(target)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
        return BackupResult(target, size_bytes=self._size(target), success=True)

    @staticmethod
    def _size(target: Path) -> int:
        return sum(p.stat().st_size for p in target.rglob("*") if p.is_file())

    def backup(self, name: str | None = None) -> BackupResult:
        return self.full_backup()

    def full_backup(self, *, acquire_lock: bool = True) -> BackupResult:
        with process_lock(self.lock_path) if acquire_lock else nullcontext():
            target = self.date_dir / f"Full_{self._identity()}"
            result = self._create(target)
            link = target.parent / "Full_Latest"
            temporary_link = target.parent / f".latest-{uuid4().hex}"
            try:
                temporary_link.symlink_to(target.name, target_is_directory=True)
                os.replace(temporary_link, link)
            finally:
                temporary_link.unlink(missing_ok=True)
            (target.parent / ".full_success").touch()
            self.prune(acquire_lock=False)
            return result

    def incremental_backup(self, *, acquire_lock: bool = True) -> BackupResult:
        with process_lock(self.lock_path) if acquire_lock else nullcontext():
            full = self._latest_full()
            # Legacy incrementals have no reliable parent identity; never guess their chain.
            if not (full / self.METADATA).exists():
                raise BackupError(
                    "Legacy backup has no dependency metadata; take a new full backup"
                )
            self._metadata(full)
            chain = self._chain(full)
            parent = chain[-1] if chain else full
            target = full.parent / "Incremental" / full.name / self._identity()
            return self._create(target, full, parent)

    def prepare(
        self,
        target: str | Path,
        destination: str | Path,
    ) -> Path:
        """Prepare a separate full recovery copy, following an incremental's parent chain."""
        source = Path(target).expanduser().resolve()
        dest = Path(destination).expanduser().absolute()
        if dest.exists() or dest.is_symlink():
            raise BackupError(f"Recovery destination already exists: {dest}")
        if dest.resolve().is_relative_to(self.backup_root):
            raise BackupError("Recovery destination must be outside the backup root")
        with process_lock(self.lock_path):
            cp = self._checkpoints(source)
            if cp["backup_type"] == "incremental":
                meta = self._metadata(source)
                full = (self.backup_root / meta["full"]).resolve()
                if not full.is_relative_to(self.backup_root):
                    raise BackupError("Full backup escapes the backup root")
                chain = self._chain(full)
                if source not in chain:
                    raise BackupError("Requested incremental is not in the committed chain")
                sources = [full, *chain[: chain.index(source) + 1]]
            elif cp["backup_type"] == "full-backuped":
                sources = [source]
            else:
                raise BackupError("Source has already been prepared; use an original backup")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with (
                process_lock(dest.with_name(dest.name + ".lock")),
                tempfile.TemporaryDirectory(
                    prefix=".bdbackup-prepare-",
                    dir=dest.parent,
                ) as workspace,
                mysql_cnf_file(user=self.user, password=self.password) as defaults,
            ):
                if dest.exists():
                    raise BackupError(f"Recovery destination already exists: {dest}")
                copies = []
                for index, original in enumerate(sources):
                    copy = Path(workspace) / str(index)
                    shutil.copytree(original, copy, symlinks=True)
                    # Database recovery tools may follow symlinks; reject them in the work copy.
                    if any(p.is_symlink() for p in copy.rglob("*")):
                        raise BackupError("Physical backup contains unsupported symlinks")
                    if any(p.suffix in {".zst", ".qp", ".lz4"} for p in copy.rglob("*")):
                        self._run(
                            [
                                self.binary,
                                f"--defaults-extra-file={defaults}",
                                "--decompress",
                                f"--target-dir={copy}",
                            ]
                        )
                    copies.append(copy)
                base = copies[0]
                common = [
                    self.binary,
                    f"--defaults-extra-file={defaults}",
                    "--prepare",
                    f"--target-dir={base}",
                ]
                self._run(
                    [*common, "--apply-log-only"]
                    if len(copies) > 1 and not self.is_mariadb
                    else common
                )
                for index, incremental in enumerate(copies[1:], 1):
                    cmd = [*common, f"--incremental-dir={incremental}"]
                    if index < len(copies) - 1 and not self.is_mariadb:
                        cmd.append("--apply-log-only")
                    self._run(cmd)
                self.verify(BackupResult(base))
                prepared = self._checkpoints(base)
                expected_lsn = self._checkpoints(sources[-1])["to_lsn"]
                if prepared["to_lsn"] != expected_lsn or prepared["backup_type"] not in {
                    "full-prepared",
                    "log-applied",
                }:
                    raise BackupError("Prepared copy does not match the requested recovery point")
                (base / self.METADATA).unlink(missing_ok=True)
                base.rename(dest)
        return dest

    def verify(self, result: BackupResult | None = None) -> BackupResult:
        target = result.path if result else self._latest_full()
        self._checkpoints(target)
        return BackupResult(target, size_bytes=self._size(target), success=True)

    def prune(self, *, acquire_lock: bool = True) -> list[Path]:
        """Expire whole daily chains under the same lock as writers, keeping the latest full."""
        if not self.backup_root.exists():
            return []
        with process_lock(self.lock_path) if acquire_lock else nullcontext():
            newest = self._latest_full_date_dir()
            if newest is None:
                raise BackupError("No successful full backup; refusing to prune")
            self._latest_full()
            cutoff = self._utc_now().date() - timedelta(days=self.retention_days - 1)
            removed = []
            for item in self.backup_root.iterdir():
                if item.is_symlink() or not item.is_dir() or item == newest:
                    continue
                if not (item / ".full_success").is_file():
                    continue
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.name):
                    continue
                try:
                    day = datetime.strptime(item.name, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    shutil.rmtree(item)
                    removed.append(item)
            return removed


ENGINE = EngineInfo(
    name="xtrabackup",
    backend=XtraBackup,
    description="Physical backups via XtraBackup / mariabackup with explicit dependency chains.",
    family="mysql",
)
register_engine(ENGINE)
