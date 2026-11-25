"""Percona Backup for MongoDB engine."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo
from bdbackup.mongodb.export import export_unit
from bdbackup.ssh_copy import safe_artifact_name

logger = logging.getLogger(__name__)
URI_ENV = "PBM_MONGODB_URI"
BACKUP_NAME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
_MONGODB_URI = re.compile(r"""mongodb(?:\+srv)?://[^\s"'<>]+""", re.IGNORECASE)


class PBMError(BackupError):
    """A PBM command or its output failed."""


@dataclass(frozen=True)
class ReplicaBackup:
    name: str
    node: str


@dataclass(frozen=True)
class BackupDescription:
    status: str
    backup_type: str
    replsets: tuple[ReplicaBackup, ...]


def _redact(text: str, uri: str | None) -> str:
    if uri:
        text = text.replace(uri, "<redacted>")
    # PBM or the driver can quote or normalise a URI in a diagnostic.
    return _MONGODB_URI.sub("<redacted>", text)


def _is_warning(line: str) -> bool:
    return line.lower().startswith("warning:")


def _last_diagnostic(stdout: str, stderr: str) -> str:
    for raw in reversed(stderr.splitlines()):
        line = raw.strip()
        if line and not _is_warning(line):
            return line
    # In JSON mode PBM can emit errors on stdout. Here the line is a diagnostic,
    # not part of the command's successful JSON response.
    lines = stdout.strip().splitlines()
    if lines:
        return lines[-1].strip()
    return ""


def _failure_diagnostic(stdout: str, stderr: str, uri: str | None) -> str:
    try:
        response = json.loads(stdout)
    except ValueError:
        response = None
    message = response.get("Error") if isinstance(response, dict) else None
    if isinstance(message, str) and message.strip():
        # Decode JSON before redacting so escaped query-string characters are covered too.
        return _redact(message, uri).strip().splitlines()[-1].strip()
    return _last_diagnostic(_redact(stdout, uri), stderr)


def _plain_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _MONGODB_URI.search(value)


class PBM:
    """Configuration and commands for a PBM backup job."""

    def __init__(
        self,
        storage_path: str | Path,
        out_dir: str | Path,
        backup_type: str = "logical",
        binary: str = "pbm",
        mongodb_uri: str | None = None,
    ):
        self.storage_path = Path(storage_path)
        self.out_dir = Path(out_dir)
        self.backup_type = backup_type
        self.binary = binary
        self.mongodb_uri = mongodb_uri

    def _run_json(
        self, command: str, *args: str, on_warning: Callable[[str], None] | None = None,
        include_stdout_diagnostics: bool = True,
    ) -> dict:
        env = os.environ.copy()
        if self.mongodb_uri is not None:
            env[URI_ENV] = self.mongodb_uri
        uri = env.get(URI_ENV)
        try:
            result = subprocess.run(  # noqa: S603
                [self.binary, command, *args, "--out", "json"],
                env=env, capture_output=True, check=False, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            detail = _redact(f"cannot run {self.binary}: {exc.strerror or exc}", uri)
            raise PBMError(f"pbm {command} failed: {detail}") from None

        stderr = _redact(result.stderr, uri)
        for raw in stderr.splitlines():
            line = raw.strip()
            if not line:
                continue
            warning = _is_warning(line)
            if warning or not result.returncode:
                logger.warning("pbm %s: %s", command, line)
            if warning and on_warning is not None:
                on_warning(line)
        diagnostic_stdout = result.stdout if include_stdout_diagnostics else ""
        if result.returncode:
            detail = _failure_diagnostic(diagnostic_stdout, stderr, uri)
            detail = detail or f"exit status {result.returncode}"
            raise PBMError(f"pbm {command} failed: {detail}")

        try:
            response = json.loads(result.stdout)
        except ValueError:
            response = None
        if not isinstance(response, dict):
            detail = _last_diagnostic(_redact(diagnostic_stdout, uri), stderr)
            suffix = f" ({detail})" if detail else ""
            raise PBMError(f"pbm {command} failed: stdout is not a JSON object{suffix}")
        return response

    def take(self) -> str:
        """Take one backup, waiting until PBM returns its name."""
        response = self._run_json("backup", "--type", self.backup_type, "--wait")
        name = response.get("name")
        if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
            raise PBMError("pbm backup failed: JSON did not contain a valid PBM backup name")
        return name

    def describe(self, name: str) -> BackupDescription:
        """Read the backup status and type and the node that backed up each replica set."""
        if not BACKUP_NAME.fullmatch(name):
            raise PBMError("pbm describe-backup failed: invalid PBM backup name")
        response = self._run_json("describe-backup", name)
        status = response.get("status")
        backup_type = response.get("type")
        if not _plain_string(status) or not _plain_string(backup_type):
            raise PBMError("pbm describe-backup failed: JSON is missing a valid status or type")

        replsets = response.get("replsets", [])
        if not isinstance(replsets, list) or (status == "done" and not replsets):
            raise PBMError("pbm describe-backup failed: JSON is missing replica sets")
        replicas = []
        for entry in replsets:
            if not isinstance(entry, dict) or not _plain_string(entry.get("name")):
                raise PBMError("pbm describe-backup failed: invalid replica set in JSON")
            node = entry.get("node")
            if status != "done" and not node:
                continue
            if not _plain_string(node):
                raise PBMError("pbm describe-backup failed: invalid backup node in JSON")
            replicas.append(ReplicaBackup(entry["name"], node))
        return BackupDescription(status, backup_type, tuple(replicas))

    def status(
        self, *, cluster_only: bool = False, on_warning: Callable[[str], None] | None = None,
    ) -> dict:
        """Read PBM's status document."""
        sections = ("--sections", "cluster") if cluster_only else ()
        return self._run_json("status", *sections, on_warning=on_warning)

    def storage_type(self, *, on_warning: Callable[[str], None] | None = None) -> object:
        """Read the configuration without opening storage or exposing its settings."""
        reply = self._run_json(
            "config", "--list", on_warning=on_warning, include_stdout_diagnostics=False,
        )
        storage = reply.get("storage")
        return storage.get("type") if isinstance(storage, dict) else None

    def version(self, *, on_warning: Callable[[str], None] | None = None) -> dict:
        """Read PBM's version document for validation."""
        return self._run_json("version", on_warning=on_warning)

    def export(
        self, job: str, name: str, nodes: tuple[str, ...], *, verify: bool = True,
    ) -> BackupResult:
        """Verify and publish a tar containing only the requested PBM backup."""
        if not safe_artifact_name(job):
            raise BackupError("pbm job name must be shell-safe")
        if not BACKUP_NAME.fullmatch(name):
            raise BackupError("invalid PBM backup name for export")
        return export_unit(self.storage_path, self.out_dir, job, name, nodes, verify)

    def backup(
        self, job: str, *, verify: bool = True, restore_info: dict | None = None,
    ) -> BackupResult:
        """Take one backup and export just that backup after PBM confirms it."""
        name = self.take()
        description = self.describe(name)
        if description.status != "done":
            raise PBMError(f"PBM backup {name} has status {description.status!r}, expected 'done'")
        if description.backup_type != self.backup_type:
            raise PBMError(
                f"PBM backup {name} has type {description.backup_type!r}, "
                f"expected {self.backup_type!r}"
            )
        nodes = tuple(dict.fromkeys(replica.node for replica in description.replsets))
        result = self.export(job, name, nodes, verify=verify)
        if restore_info is not None:
            restore_info.update(
                kind="pbm", name=name, backup_type=description.backup_type,
                replsets=[{"name": replica.name, "node": replica.node}
                          for replica in description.replsets],
            )
        return result


ENGINE = EngineInfo(
    name="pbm",
    backend=PBM,
    description="Export a Percona Backup for MongoDB backup as a tar archive",
    family="mongodb",
)
