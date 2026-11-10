"""The pgBackRest engine: pick the newest full backup of a stanza to export as one tar.

This module holds the engine's keys, the pgBackRest commands it runs and the choice of which
full to export. Reading the repository is :mod:`bdbackup.postgres.repository`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo
from bdbackup.history import History
from bdbackup.postgres.repository import (
    FullBackup,
    archive_copy,
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
        raise NotImplementedError

    def require_marker(self) -> None:
        raise NotImplementedError

    def export(self, full: FullBackup) -> BackupResult:
        raise NotImplementedError

    def verify(self, result: BackupResult) -> BackupResult:
        raise NotImplementedError

    def backup(self, name: str | None = None) -> BackupResult:
        raise NotImplementedError

    def prune(self) -> list[Path]:
        raise NotImplementedError


class _HashingWriter:
    def write(self, data) -> int:
        raise NotImplementedError


class _HashingReader:
    def read(self, size: int = -1) -> bytes:
        raise NotImplementedError


ENGINE = EngineInfo(
    name="pgbackrest",
    backend=PgBackRest,
    description="pgBackRest full backups exported as one tar",
    family="postgres",
)
