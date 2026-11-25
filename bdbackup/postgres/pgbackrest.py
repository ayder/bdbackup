"""The pgBackRest engine: pick the newest full backup of a stanza to export as one tar.

This module holds the engine's keys, the pgBackRest commands it runs and the choice of which
full to export. Reading the repository is :mod:`bdbackup.postgres.repository`.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from bdbackup import tar_export
from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History
from bdbackup.postgres.metrics import PgBackRestCollector
from bdbackup.postgres.repository import (
    ENCRYPTED,
    LABEL,
    FullBackup,
    archive_copy,
    full_entries,
    is_encrypted,
    newest_full,
    parse_info_file,
    read_info_pair,
    stanza_problem,
)

TESTED_VERSIONS = ("2.58.0", "2.60.0")

# Keep the existing export's test hooks bound to the classes used by the shared helpers.
_HashingWriter = tar_export.HashingWriter
_HashingReader = tar_export.HashingReader


@dataclass(frozen=True)
class Choice:
    """The full to export and whether this job has not exported it yet."""

    full: FullBackup
    new: bool


@dataclass
class ExportProgress:
    """How far an export got, and how long its phases took; ``PgBackRest.export`` fills it in.

    ``step`` is the phase the export is in, and after a failure the phase it failed in:
    ``start``, ``destination``, ``write``, ``verify``, ``publish`` or ``done``.
    """

    step: str = "start"
    write_seconds: float | None = None
    verify_seconds: float | None = None


_clock = time.monotonic  # the export's timer, a module attribute so tests can set it


class PgBackRestError(BackupError):
    """A pgBackRest command that failed, and why.

    ``reason`` is ``binary_missing`` (not found, or not executable), ``failed`` (an exit
    status, or any other failure to start it), ``timeout`` or ``output_invalid`` (``info``
    printed something that is not a JSON list of stanzas).
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


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

    def command(self, *args: str) -> list[str]:
        """A pgBackRest command line that names the repository and stanza this job reads."""
        return [
            self.binary,
            *([f"--config={self.config_file}"] if self.config_file is not None else []),
            f"--repo={self.repo}",
            f"--repo{self.repo}-path={self.repo_path}",
            # A pgbackrest.conf that makes this index an object store must not redirect info.
            f"--repo{self.repo}-type=posix",
            f"--stanza={self.stanza}",
            *args,
        ]

    def _run(self, cmd: list[str], name: str, timeout: float | None = None) -> str:
        """Run ``cmd`` and return its stdout; a failure carries pgBackRest's last stderr line.

        Every failure is a :class:`PgBackRestError`, a BackupError that says why: a binary that
        cannot be started is never an OSError. ``timeout`` (seconds) is passed to the command
        only when given.
        """
        options = {} if timeout is None else {"timeout": timeout}
        try:
            completed = subprocess.run(  # noqa: S603
                cmd, check=True, capture_output=True, text=True, **options
            )
        except subprocess.CalledProcessError as exc:
            lines = (exc.stderr or "").strip().splitlines()
            detail = lines[-1].strip() if lines else f"exit status {exc.returncode}"
            raise PgBackRestError(f"pgbackrest {name} failed: {detail}", "failed") from exc
        except subprocess.TimeoutExpired as exc:
            raise PgBackRestError(
                f"pgbackrest {name} timed out after {timeout} seconds", "timeout"
            ) from exc
        except OSError as exc:
            raise self._start_failure(cmd[0], name, exc) from exc
        return completed.stdout

    @staticmethod
    def _start_failure(binary: str, name: str, exc: OSError) -> PgBackRestError:
        """The error for a pgBackRest command that could not be started."""
        if isinstance(exc, FileNotFoundError):
            return PgBackRestError(
                f"pgbackrest binary not found: {binary} "
                "(set the job's binary key to its full path)",
                "binary_missing",
            )
        if isinstance(exc, PermissionError):
            return PgBackRestError(
                f"pgbackrest binary is not executable: {binary} (check its permissions)",
                "binary_missing",
            )
        return PgBackRestError(f"pgbackrest {name} failed: {exc.strerror or exc}", "failed")

    def version(self) -> str:
        """The pgBackRest version, for example ``2.58.0`` from ``pgBackRest 2.58.0``."""
        text = self._run([self.binary, "version"], "version").strip()
        match = re.match(r"pgBackRest (\S+)", text)
        if match is None:
            raise BackupError(f"unexpected pgbackrest version output: {text!r}")
        return match.group(1)

    def info(self, timeout: float | None = None) -> list[dict]:
        """The ``info --output=json`` document: one entry per stanza of the repository.

        ``timeout`` (seconds) limits the command; ``None`` lets it run as long as it takes.
        """
        text = self._run(self.command("info", "--output=json"), "info", timeout)
        try:
            document = json.loads(text)
        except ValueError:
            document = None
        if not isinstance(document, list) or not all(isinstance(s, dict) for s in document):
            raise PgBackRestError(
                "pgbackrest info did not print a JSON list of stanzas", "output_invalid"
            )
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

    def preflight(self) -> list[tuple[str, str]]:
        """The checks of ``run --validate`` as ``("OK" | "FAIL", message)`` lines, in order.

        Nothing is written and no lock is taken. A failing check is one FAIL line, never an
        exception, and the checks after it still run, except that the encryption and
        repository directory checks are skipped when ``info`` does not list the stanza or
        reports it with a problem status.
        """
        lines = [self._binary_line(), self._version_line(), self._root_line()]
        stanza_line, entry = self._stanza_line()
        lines.append(stanza_line)
        if entry is not None:
            lines.append(self._encryption_line(entry))
            lines.extend(self._repository_directory_lines())
        lines.append(self._marker_line())
        lines.append(self._out_dir_line())
        return lines

    @staticmethod
    def _attempt(name: str, call):
        """``(result, None)``, or ``(None, why)`` when the pgBackRest command ``name`` fails."""
        try:
            return call(), None
        except BackupError as exc:
            # A binary that cannot be started is reported as the OS says it, as it always was
            # here; ``run`` names the binary and the fix instead.
            if isinstance(exc.__cause__, OSError):
                return None, f"pgbackrest {name} failed: {exc.__cause__.strerror or exc.__cause__}"
            return None, str(exc)

    def _binary_line(self) -> tuple[str, str]:
        found = shutil.which(self.binary)
        if found:
            return "OK", f"Executable: {found}"
        return "FAIL", f"Required executable {self.binary!r} is missing or not executable in PATH."

    def _version_line(self) -> tuple[str, str]:
        version, why = self._attempt("version", self.version)
        if why is not None:
            return "FAIL", why
        note = ""
        if version not in TESTED_VERSIONS:
            note = f" (not a tested version: {', '.join(TESTED_VERSIONS)})"
        return "OK", f"pgBackRest {version}{note}"

    def _root_line(self) -> tuple[str, str]:
        try:
            self.refuse_root()
        except BackupError as exc:
            return "FAIL", str(exc)
        return "OK", "not running as root"

    def _stanza_line(self) -> tuple[tuple[str, str], dict | None]:
        """The line for the info command, and the stanza's entry when it lists one."""
        document, why = self._attempt("info", self.info)
        if why is not None:
            return ("FAIL", why), None
        entry = next((stanza for stanza in document if stanza.get("name") == self.stanza), None)
        if entry is None:
            return ("FAIL", f"stanza {self.stanza!r} is not in the pgbackrest info output"), None
        problem = stanza_problem(entry)
        if problem is not None:
            return ("FAIL", problem), None
        return ("OK", f"stanza {self.stanza} is listed by pgbackrest info"), entry

    @staticmethod
    def _encryption_line(entry: dict) -> tuple[str, str]:
        if is_encrypted(entry):
            return "FAIL", ENCRYPTED
        return "OK", "repository is not encrypted"

    def _repository_directory_lines(self) -> list[tuple[str, str]]:
        lines = []
        for part in ("backup", "archive"):
            path = self.repo_path / part / self.stanza
            if _usable_dir(path, os.R_OK | os.X_OK):
                lines.append(("OK", f"{path}: readable and searchable"))
            else:
                lines.append(("FAIL", f"{path}: not a readable and searchable directory"))
        return lines

    def _marker_line(self) -> tuple[str, str]:
        try:
            self.require_marker()
        except BackupError as exc:
            return "FAIL", str(exc)
        except OSError as exc:  # Python 3.12 raises when the stage cannot be searched
            return "FAIL", f"{self.first_stage}: cannot check for {MARKER}: {exc.strerror or exc}"
        return "OK", f"{self.first_stage}: {MARKER} present"

    def _out_dir_line(self) -> tuple[str, str]:
        """``out_dir`` must be writable, or must be creatable under its nearest existing parent.

        The search for that parent stops at the first stage, so an unmounted share is never
        satisfied by a writable directory above it. It compares resolved paths, so an
        ``out_dir`` configured through a symlink still meets the first stage.
        """
        out_dir = self.out_dir.resolve()
        stop = self.first_stage.resolve()
        target = out_dir
        while not os.path.exists(target) and target != stop and target != target.parent:
            target = target.parent
        writable = _usable_dir(target, os.W_OK | os.X_OK)
        subject = f"Output directory: {self.out_dir}"
        if target == out_dir:
            if writable:
                return "OK", f"{subject} is writable"
            return "FAIL", f"{subject} is not a writable directory"
        if writable:
            return "OK", f"{subject} does not exist; {target} is writable, so it can be created"
        return "FAIL", f"{subject} does not exist and {target} is not a writable directory"

    def export(
        self, full: FullBackup, *, verify: bool = True,
        progress: ExportProgress | None = None,
    ) -> BackupResult:
        """Write ``full`` as one tar in ``out_dir``, hashing its bytes as they are written.

        With ``verify`` the temporary file is read back once before it gets its final name.
        Nothing is published unless the whole tar was written, synced and, with ``verify``,
        read back; on any failure the temporary file is removed. Every file under
        ``repo_path`` is opened read-only.

        ``progress``, when given, is updated as the export goes, so its caller can tell after
        a failure which phase failed and what the phases took (:class:`ExportProgress`).
        """
        progress = progress if progress is not None else ExportProgress()
        progress.step = "destination"
        self.require_marker()
        try:
            return self._export(full, verify, progress)
        except OSError as exc:
            # Not left as an OSError: BlockingIOError would read as a busy lock (exit 3).
            raise BackupError(
                f"cannot export {full.label} to {self.out_dir}: {exc.strerror or exc}"
            ) from exc

    def _export(self, full: FullBackup, verify: bool, progress: ExportProgress) -> BackupResult:
        progress.step = "write"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._remove_stale_temps()
        final = self.unit_path(full.label)
        if final.exists():
            raise BackupError(f"unit already exists: {final}")
        temp = self.out_dir / f".{self.stanza}-{full.label}.tar.tmp-{uuid.uuid4().hex}"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        writer = _HashingWriter(fd)
        try:
            started = _clock()
            names = self._write_tar(writer, full.label)
            os.fsync(fd)
            # Cleared before the close: a failed close has still released the descriptor.
            closing, fd = fd, None
            os.close(closing)
            progress.write_seconds = _clock() - started
            if verify:
                progress.step = "verify"
                started = _clock()
                # As FileBackup.close_archive does: a tar that fails is never published.
                self._read_back(temp, final, writer.sha256.hexdigest(), names)
                progress.verify_seconds = _clock() - started
            progress.step = "publish"
            os.rename(temp, final)
        except BaseException:
            # A failed tar stream flushes its buffer again when it is collected; that write
            # must not reach the closed descriptor, which another file may have reused.
            writer.discard()
            if fd is not None:
                os.close(fd)
            temp.unlink(missing_ok=True)
            raise
        tar_export.fsync_dir(self.out_dir)
        progress.step = "done"
        return BackupResult(
            final, size_bytes=writer.size, success=True, checksum=writer.sha256.hexdigest()
        )

    def _remove_stale_temps(self) -> None:
        """Remove temporary files an earlier run of this stanza left; nobody else's."""
        pattern = re.compile(
            re.escape(f".{self.stanza}-") + LABEL.pattern + r"\.tar\.tmp-[0-9a-f]{32}"
        )
        tar_export.remove_stale_temps(self.out_dir, pattern)

    def _write_tar(self, writer: _HashingWriter, label: str) -> list[str]:
        """Write the full's directory, then its info file pairs; return the member names."""
        root = self.repo_path.resolve()
        names = []
        # Not a ``with`` block: closing after a failed write would write to the file again.
        tar = tarfile.open(fileobj=writer, mode="w|", bufsize=tar_export.CHUNK)
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

    @staticmethod
    def _read_back(path: Path, unit: Path, checksum: str, written: list[str]) -> None:
        tar_export.read_back(path, unit, checksum, written, opener=open)

    def backup(self, name: str | None = None) -> BackupResult:
        """Not used: a pgbackrest job exports through ``bdbackup run`` only."""
        raise BackupError("pgbackrest jobs run only through bdbackup run")

    def prune(self) -> list[Path]:
        """Nothing to prune: the GFS job that owns ``out_dir`` rotates the units."""
        return []


def _usable_dir(path: Path, mode: int) -> bool:
    """Whether ``path`` is a directory the effective user may access with ``mode``.

    ``os.path`` answers False when a parent cannot be searched; ``Path.is_dir`` and
    ``Path.exists`` raise PermissionError there on Python 3.12.
    """
    return os.path.isdir(path) and os.access(path, mode, effective_ids=True)


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


ENGINE = EngineInfo(
    name="pgbackrest",
    backend=PgBackRest,
    description="pgBackRest full backups exported as one tar",
    family="postgres",
    metrics=PgBackRestCollector(),
)
