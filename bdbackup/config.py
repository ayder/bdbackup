"""Configuration-driven job definitions for bdbackup."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bdbackup.engines import EngineError, get_engine, list_engines
from bdbackup.filebackup import FileBackup


@dataclass
class Job:
    """A single named backup job parsed from config."""

    name: str
    type: str
    params: dict[str, Any]


class ConfigError(Exception):
    """Raised when a configuration file is invalid."""


def supported_types() -> set[str]:
    """Return all valid job types: builtins plus every registered engine name."""
    return {"file", "retention"} | set(list_engines())


class Config:
    """Load and validate a TOML job configuration file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.jobs: dict[str, Job] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise ConfigError(f"Config file not found: {self.path}")
        with self.path.open("rb") as f:
            data = tomllib.load(f)

        for name, section in data.items():
            if not isinstance(section, dict):
                raise ConfigError(f"Job {name!r} must be a table")
            job_type = section.get("type")
            if job_type not in supported_types():
                raise ConfigError(
                    f"Job {name!r} has unsupported type {job_type!r}; "
                    f"expected one of {sorted(supported_types())}"
                )
            params = {k: v for k, v in section.items() if k != "type"}
            self.jobs[name] = Job(name=name, type=job_type, params=params)

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
