"""Configuration-driven job definitions for bdbackup."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bdbackup.engines import EngineError, get_engine, list_engines
from bdbackup.filebackup import FileBackup
from bdbackup.history import HistorySettings
from bdbackup.scheduling import validate_schedule


@dataclass
class Job:
    """A single named backup job parsed from config."""

    name: str
    type: str
    params: dict[str, Any]
    schedule: str | None = None


class ConfigError(Exception):
    """Raised when a configuration file is invalid."""


def supported_types() -> set[str]:
    """Return all valid job types: builtins plus every registered engine name."""
    return {"file", "retention"} | set(list_engines())


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
            schedule = section.get("schedule")
            if "schedule" in section:
                try:
                    schedule = validate_schedule(schedule)
                except ValueError as exc:
                    raise ConfigError(f"Job {name!r}: {exc}") from exc
            params = {k: v for k, v in section.items() if k not in {"type", "schedule"}}
            # Config paths are relative to the config file; file-template entries
            # and excludes remain relative to the explicitly selected source path.
            for key in (
                "template_filename",
                "backup_dst",
                "chdir",
                "out_dir",
                "backup_root",
                "encrypt_key_file",
                "full_dir",
                "incr_dir",
                "log_dir",
            ):
                if key in params:
                    try:
                        params[key] = self.path.parent / Path(params[key]).expanduser()
                    except TypeError as exc:
                        raise ConfigError(f"Job {name!r}: {key} must be a path string") from exc
            self.jobs[name] = Job(name=name, type=job_type, params=params, schedule=schedule)

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
