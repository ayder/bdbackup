"""The pgBackRest engine: pick the newest full backup of a stanza to export as one tar.

This module holds the engine's keys, the pgBackRest commands it runs and the choice of which
full to export. Reading the repository is :mod:`bdbackup.postgres.repository`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History
from bdbackup.postgres.repository import (
    LABEL,
    FullBackup,
    archive_copy,
    full_entries,
    newest_full,
    parse_info_file,
    read_info_pair,
)


@dataclass(frozen=True)
class Choice:
    """The full to export and whether this job has not exported it yet."""

    full: FullBackup
    new: bool


def _whole_number(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


class PgBackRest:
    """Export the newest pgBackRest full of one stanza from a local POSIX repository."""

    def __init__(
        self,
        stanza: str,
        repo_path: str | Path,
        out_dir: str | Path,
        first_stage: str | Path,
        lock_file: str | Path,
        repo: int = 1,
        binary: str = "pgbackrest",
        config_file: str | Path | None = None,
        max_full_age_hours: int | None = None,
    ):
        if not isinstance(stanza, str) or not stanza.strip():
            raise ValueError("stanza must be a nonempty string")
        if not _whole_number(repo, 1):
            raise ValueError("repo must be a whole number of 1 or more")
        if max_full_age_hours is not None and not _whole_number(max_full_age_hours, 1):
            raise ValueError("max_full_age_hours must be a positive whole number")
        self.stanza = stanza
        self.repo = repo
        self.repo_path = Path(repo_path).expanduser().absolute()
        self.out_dir = Path(out_dir).expanduser().absolute()
        self.first_stage = Path(first_stage)
        self.lock_file = Path(lock_file)
        self.binary = binary
        self.config_file = Path(config_file) if config_file is not None else None
        self.max_full_age_hours = max_full_age_hours
        self._written: dict[Path, list[str]] = {}  # member names of each unit exported

    def command(self, *args: str) -> list[str]:
        """A pgBackRest command line that names the repository and stanza this job reads."""
        return [
            self.binary,
            *([f"--config={self.config_file}"] if self.config_file is not None else []),
            f"--repo={self.repo}",
            f"--repo{self.repo}-path={self.repo_path}",
            f"--stanza={self.stanza}",
            *args,
        ]

    def _run(self, cmd: list[str], name: str) -> str:
        """Run ``cmd`` and return its stdout; a failure carries pgBackRest's last stderr line."""
        try:
            completed = subprocess.run(  # noqa: S603
                cmd, check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as exc:
            lines = (exc.stderr or "").strip().splitlines()
            detail = lines[-1].strip() if lines else f"exit status {exc.returncode}"
            raise BackupError(f"pgbackrest {name} failed: {detail}") from exc
        return completed.stdout

    def version(self) -> str:
        """The pgBackRest version, for example ``2.58.0`` from ``pgBackRest 2.58.0``."""
        text = self._run([self.binary, "version"], "version").strip()
        match = re.match(r"pgBackRest (\S+)", text)
        if match is None:
            raise BackupError(f"unexpected pgbackrest version output: {text!r}")
        return match.group(1)

    def info(self) -> list[dict]:
        """The ``info --output=json`` document: one entry per stanza of the repository."""
        text = self._run(self.command("info", "--output=json"), "info")
        try:
            document = json.loads(text)
        except ValueError:
            document = None
        if not isinstance(document, list) or not all(isinstance(s, dict) for s in document):
            raise BackupError("pgbackrest info did not print a JSON list of stanzas")
        return document

    def choose(self, history: History, job_name: str) -> Choice:
        """The newest full, and whether ``job_name`` has not exported it yet.

        Refuses, as a BackupError, an encrypted repository, a stanza with no full and a full
        taken without archive-copy, in that order; a record never overrides a refusal.
        """
        full = newest_full(self.info(), self.stanza)
        if full is None:
            raise BackupError(f"stanza {self.stanza} has no full backup")
        pair = read_info_pair(
            self.repo_path, f"backup/{self.stanza}/backup.info", label=full.label
        )
        if not archive_copy(parse_info_file(pair.main.decode("utf-8")), full.label):
            raise BackupError(
                f"full {full.label} was taken without archive-copy; "
                "take fulls with --archive-copy or archive-copy=y"
            )
        return Choice(full, full.label not in self._exported_labels(history, job_name))

    @staticmethod
    def _exported_labels(history: History, job_name: str) -> set[str]:
        """Labels in the restore info of this job's successful records."""
        # A fresh install has no database until its first record is written.
        if not history.settings.database.exists():
            return set()
        labels = set()
        for record in history.records(job=job_name, successful=True):
            try:
                info = json.loads(record.restore_info)
            except ValueError:
                continue
            if isinstance(info, dict) and isinstance(info.get("label"), str):
                labels.add(info["label"])
        return labels

    def restore_info(self, full: FullBackup, version: str) -> dict:
        """What a restore needs to know about the export; never a passphrase or an option."""
        return {
            "kind": "pgbackrest",
            "stanza": self.stanza,
            "label": full.label,
            "pgbackrest_version": version,
            "pg_version": full.pg_version,
            "page_errors": full.page_errors,
        }

    def unit_path(self, label: str) -> Path:
        """Where the export of ``label`` is published."""
        return self.out_dir / f"{self.stanza}-{label}.tar"

    def refuse_root(self) -> None:
        """Refuse to run as root: the export runs as the OS user that owns the repository."""
        if os.geteuid() == 0:
            raise BackupError(
                "pgbackrest jobs do not run as root; "
                "run as the OS user that owns the repository"
            )

    def require_marker(self) -> None:
        """Require the first stage's marker, so an unmounted share is never written to."""
        if not (self.first_stage / MARKER).is_file():
            raise BackupError(
                f"{self.first_stage}: {MARKER} missing; "
                "run --initialize-structure for its gfs job"
            )

    def export(self, full: FullBackup) -> BackupResult:
        """Write ``full`` as one tar in ``out_dir``, hashing its bytes as they are written.

        Nothing is published unless the whole tar was written and synced; on any failure the
        temporary file is removed. Every file under ``repo_path`` is opened read-only.
        """
        self.require_marker()
        try:
            return self._export(full)
        except OSError as exc:
            # Not left as an OSError: BlockingIOError would read as a busy lock (exit 3).
            raise BackupError(
                f"cannot export {full.label} to {self.out_dir}: {exc.strerror or exc}"
            ) from exc

    def _export(self, full: FullBackup) -> BackupResult:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._remove_stale_temps()
        final = self.unit_path(full.label)
        if final.exists():
            raise BackupError(f"unit already exists: {final}")
        temp = self.out_dir / f".{self.stanza}-{full.label}.tar.tmp-{uuid.uuid4().hex}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        writer = _HashingWriter(fd)
        try:
            names = self._write_tar(writer, full.label)
            os.fsync(fd)
            # Cleared before the close: a failed close has still released the descriptor.
            closing, fd = fd, None
            os.close(closing)
            os.rename(temp, final)
        except BaseException:
            # A failed tar stream flushes its buffer again when it is collected; that write
            # must not reach the closed descriptor, which another file may have reused.
            writer.discard()
            if fd is not None:
                os.close(fd)
            temp.unlink(missing_ok=True)
            raise
        _fsync_dir(self.out_dir)
        self._written[final] = names
        return BackupResult(
            final, size_bytes=writer.size, success=True, checksum=writer.sha256.hexdigest()
        )

    def _remove_stale_temps(self) -> None:
        """Remove temporary files an earlier run of this stanza left; nobody else's."""
        pattern = re.compile(
            re.escape(f".{self.stanza}-") + LABEL.pattern + r"\.tar\.tmp-[0-9a-f]{32}"
        )
        with os.scandir(self.out_dir) as scan:
            stale = [
                Path(entry.path)
                for entry in scan
                if pattern.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
            ]
        for path in stale:
            path.unlink(missing_ok=True)

    def _write_tar(self, writer: _HashingWriter, label: str) -> list[str]:
        """Write the full's directory, then its info file pairs; return the member names."""
        root = self.repo_path.resolve()
        names = []
        # Not a ``with`` block: closing after a failed write would write to the file again.
        tar = tarfile.open(fileobj=writer, mode="w|", bufsize=_CHUNK)
        for path in full_entries(root, self.stanza, label):
            arcname = path.relative_to(root).as_posix()
            _add_entry(tar, path, arcname)
            names.append(arcname)
        pairs = [
            read_info_pair(root, f"backup/{self.stanza}/backup.info", label=label),
            read_info_pair(root, f"archive/{self.stanza}/archive.info"),
        ]
        for pair in pairs:
            for name, data, st in (
                (pair.relative, pair.main, pair.main_stat),
                (f"{pair.relative}.copy", pair.copy, pair.copy_stat),
            ):
                info = tarfile.TarInfo(name)
                info.mode = stat.S_IMODE(st.st_mode)
                info.mtime = st.st_mtime
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                names.append(name)
        tar.close()
        return names

    def verify(self, result: BackupResult) -> BackupResult:
        """Read the published tar back once: walk its members and re-hash every byte."""
        path = result.path
        written = self._written.get(path)
        if written is None or result.checksum is None:
            raise BackupError(f"{path} was not exported by this run; nothing to verify it against")
        try:
            with open(path, "rb") as stream:
                reader = _HashingReader(stream)
                names = []
                with tarfile.open(fileobj=reader, mode="r|", bufsize=_CHUNK) as tar:
                    for member in tar:
                        names.append(member.name)
                        if member.isfile():
                            with tar.extractfile(member) as data:
                                while data.read(_CHUNK):
                                    pass
                # The zero blocks and padding after the last member are part of the digest.
                while reader.read(_CHUNK):
                    pass
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise BackupError(f"verification of {path} failed: {exc}") from exc
        digest = reader.sha256.hexdigest()
        if digest != result.checksum:
            raise BackupError(
                f"verification of {path} failed: checksum {digest} read back, "
                f"{result.checksum} written"
            )
        if names != written:
            raise BackupError(f"verification of {path} failed: members differ from those written")
        return BackupResult(path, size_bytes=reader.size, success=True, checksum=digest)

    def backup(self, name: str | None = None) -> BackupResult:
        """Not used: a pgbackrest job exports through ``bdbackup run`` only."""
        raise BackupError("pgbackrest jobs run only through bdbackup run")

    def prune(self) -> list[Path]:
        """Nothing to prune: the GFS job that owns ``out_dir`` rotates the units."""
        return []


_CHUNK = 1024 * 1024  # tar stream buffer: fewer, larger reads and writes over NFS


def _changed(path: Path, before: os.stat_result) -> bool:
    try:
        after = path.lstat()
    except FileNotFoundError:
        return True
    return (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    )


def _add_entry(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    """Add one directory or regular file, failing if a file changes while it is read."""
    try:
        before = path.lstat()
        info = tar.gettarinfo(path, arcname)
        source = open(path, "rb") if info.isfile() else None
    except FileNotFoundError as exc:
        raise BackupError(f"input changed during export: {path}") from exc
    except OSError as exc:
        raise BackupError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        tar.addfile(info, source)
    except OSError as exc:
        # tarfile raises OSError when a file is shorter than its stat said.
        if source is not None and _changed(path, before):
            raise BackupError(f"input changed during export: {path}") from exc
        raise
    finally:
        if source is not None:
            source.close()
    if source is not None and _changed(path, before):
        raise BackupError(f"input changed during export: {path}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _HashingWriter:
    """A write-only file object over ``fd`` that feeds every byte written to SHA-256."""

    def __init__(self, fd: int):
        self.fd = fd
        self.sha256 = hashlib.sha256()
        self.size = 0
        self.discarding = False

    def discard(self) -> None:
        """Drop every later write: the export failed and ``fd`` is about to be closed."""
        self.discarding = True

    def write(self, data) -> int:
        if self.discarding:
            return len(data)
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            self.sha256.update(view[:count])
            self.size += count
            view = view[count:]
        return len(data)


class _HashingReader:
    """A read-only file object that feeds every byte read to SHA-256."""

    def __init__(self, stream):
        self.stream = stream
        self.sha256 = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.sha256.update(data)
        self.size += len(data)
        return data


ENGINE = EngineInfo(
    name="pgbackrest",
    backend=PgBackRest,
    description="pgBackRest full backups exported as one tar",
    family="postgres",
)
