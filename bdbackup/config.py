"""Configuration-driven job definitions for bdbackup."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bdbackup.engines import EngineError, get_engine, list_engines
from bdbackup.filebackup import FileBackup
from bdbackup.gfs.stages import check_gfs_config, parse_gfs_job
from bdbackup.history import HistorySettings


@dataclass
class Job:
    """A single named backup job parsed from config."""

    name: str
    type: str
    params: dict[str, Any]
    restore_root: Path | None = None
    active: bool = True


class ConfigError(Exception):
    """Raised when a configuration file is invalid."""


# Keys removed in 0.7.0, by the job types that had them (None: every type).
_REMOVED_KEYS = {"schedule": None, "apply": {"gfs"}, "jobs": {"mysqldump"}}


def supported_types() -> set[str]:
    """Return all valid job types: builtins plus every registered engine name."""
    return {"file", "gfs"} | set(list_engines())


class Config:
    """Load and validate a TOML job configuration file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.jobs: dict[str, Job] = {}
        self.history: HistorySettings | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise ConfigError(f"Config file not found: {self.path}")
        try:
            with self.path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Cannot read config {self.path}: {exc}") from exc

        if "history" in data:
            section = data.pop("history")
            if not isinstance(section, dict) or set(section) - {"database", "restore_root"}:
                raise ConfigError("[history] accepts only database and restore_root")
            paths = {}
            for key, default in (("database", None), ("restore_root", "restores")):
                value = section.get(key, default)
                if not isinstance(value, str) or not value.strip() or value == ":memory:":
                    raise ConfigError(f"[history] {key} must be a nonempty filesystem path")
                paths[key] = (self.path.parent / Path(value).expanduser()).resolve()
            self.history = HistorySettings(**paths)

        for name, section in data.items():
            if not isinstance(section, dict):
                raise ConfigError(f"Job {name!r} must be a table")
            job_type = section.get("type")
            if job_type not in supported_types():
                raise ConfigError(
                    f"Job {name!r} has unsupported type {job_type!r}; "
                    f"expected one of {sorted(supported_types())}"
                )
            for key, types in _REMOVED_KEYS.items():
                if key in section and (types is None or job_type in types):
                    raise ConfigError(f"Job {name!r}: key {key!r} was removed in 0.7.0")
            section = dict(section)
            active = section.pop("active", True)
            if not isinstance(active, bool):
                raise ConfigError(f"Job {name!r}: active must be true or false")
            if job_type == "gfs":
                try:
                    params = parse_gfs_job(section, self.path.parent)
                except ValueError as exc:
                    raise ConfigError(f"Job {name!r}: {exc}") from exc
                self.jobs[name] = Job(name=name, type=job_type, params=params, active=active)
                continue
            restore_root = None
            if "restore_root" in section:
                value = section["restore_root"]
                if not isinstance(value, str) or not value.strip():
                    raise ConfigError(f"Job {name!r}: restore_root must be a nonempty path string")
                if self.history is None:
                    raise ConfigError(f"Job {name!r}: restore_root requires a [history] section")
                restore_root = (self.path.parent / Path(value).expanduser()).resolve()
            params = {k: v for k, v in section.items() if k not in {"type", "restore_root"}}
            # Config paths are relative to the config file; file-template entries
            # and excludes remain relative to the explicitly selected source path.
            for key in (
                "template_filename",
                "backup_dst",
                "chdir",
                "out_dir",
                "backup_root",
                "encrypt_key_file",
            ):
                if key in params:
                    try:
                        params[key] = self.path.parent / Path(params[key]).expanduser()
                    except TypeError as exc:
                        raise ConfigError(f"Job {name!r}: {key} must be a path string") from exc
            self.jobs[name] = Job(
                name=name, type=job_type, params=params, restore_root=restore_root,
                active=active,
            )

        if any(job.type == "gfs" for job in self.jobs.values()):
            try:
                check_gfs_config(self.jobs, self.history)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc

    def get(self, name: str) -> Job:
        if name not in self.jobs:
            raise ConfigError(f"Job {name!r} not found in {self.path}")
        return self.jobs[name]


def build_backend(job: Job):
    """Instantiate the appropriate backend for a parsed job via the engine registry."""
    if job.type == "file":
        return FileBackup(**job.params)
    try:
        return get_engine(job.type).backend(**job.params)
    except EngineError as exc:
        raise ConfigError(str(exc)) from exc
