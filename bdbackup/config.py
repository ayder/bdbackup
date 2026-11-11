"""Configuration-driven job definitions for bdbackup."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bdbackup.engines import EngineError, get_engine, list_engines
from bdbackup.filebackup import FileBackup
from bdbackup.gfs.stages import check_gfs_config, parse_gfs_job, pgbackrest_first_stage
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
                "repo_path",
                "config_file",
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
        self._check_pgbackrest()

    def _check_pgbackrest(self) -> None:
        """Rules for pgbackrest jobs; they follow ``check_gfs_config``, which reads the stages."""
        for name, job in self.jobs.items():
            if job.type != "pgbackrest":
                continue
            if self.history is None:
                raise ConfigError(f"Job {name!r}: pgbackrest jobs require a [history] section")
            for key in ("first_stage", "lock_file"):
                if key in job.params:
                    raise ConfigError(f"Job {name!r}: {key} is set by bdbackup, not in the config")
            for key, rule in (
                ("repo", "a whole number of 1 or more"),
                ("max_full_age_hours", "a positive whole number"),
            ):
                value = job.params.get(key, 1)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ConfigError(f"Job {name!r}: {key} must be {rule}")
            try:
                job.params["first_stage"] = pgbackrest_first_stage(job, self.jobs)
            except ValueError as exc:
                raise ConfigError(f"Job {name!r}: {exc}") from exc
            self._check_pgbackrest_layout(name, job)
            job.params["lock_file"] = Path(f"{self.history.database}.pgbackrest-{name}.lock")

    def _check_pgbackrest_layout(self, name: str, job: Job) -> None:
        """Keep the repository apart from everything bdbackup writes or locks.

        bdbackup never writes under ``repo_path``; the units, the gfs rotation, the history
        database with its lock and journal files, and the restores do.
        """
        if "repo_path" not in job.params:
            return  # the engine refuses the job when it is built
        repo = Path(job.params["repo_path"]).expanduser().resolve()
        out_dir = Path(job.params["out_dir"]).expanduser().resolve()
        if out_dir.is_relative_to(repo):
            raise ConfigError(f"Job {name!r}: out_dir must be outside repo_path")
        # Both [history] paths are resolved when the section is read.
        for key, path in (
            ("[history] database", self.history.database),
            ("[history] restore_root", self.history.restore_root),
            ("restore_root", job.restore_root),
        ):
            if path is not None and path.is_relative_to(repo):
                raise ConfigError(f"Job {name!r}: {key} must be outside repo_path")
        for other in self.jobs.values():
            if other.type != "gfs":
                continue
            for stage in other.params["stage"]:
                if any(repo.is_relative_to(path) for path in stage.paths):
                    raise ConfigError(
                        f"Job {name!r}: repo_path must be outside every gfs stage path"
                    )
                if any(path.is_relative_to(repo) for path in stage.paths):
                    raise ConfigError(f"Job {name!r}: a gfs stage path must be outside repo_path")

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
