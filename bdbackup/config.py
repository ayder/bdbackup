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
from bdbackup.metrics.settings import MetricsSettings, parse_metrics
from bdbackup.mongodb.pbm import PBM
from bdbackup.mysql.mysqldump import MySQLBackup
from bdbackup.postgres.pgbackrest import PgBackRest
from bdbackup.ssh_copy import CopySettings, parse_copy_settings, safe_artifact_name
from bdbackup.utils import lexical_path, real_path


@dataclass
class Job:
    """A single named backup job parsed from config."""

    name: str
    type: str
    params: dict[str, Any]
    restore_root: Path | None = None
    active: bool = True
    copy: CopySettings | None = None


class ConfigError(Exception):
    """Raised when a configuration file is invalid."""


# Keys removed in 0.7.0, by the job types that had them (None: every type).
_REMOVED_KEYS = {"schedule": None, "apply": {"gfs"}, "jobs": {"mysqldump"}}
_COPY_ENGINES = {"mysqldump": MySQLBackup, "pgbackrest": PgBackRest, "pbm": PBM}
_PBM_PATHS = ("storage_path", "out_dir")
_PBM_KEYS = frozenset((*_PBM_PATHS, "backup_type", "binary", "mongodb_uri"))


def supported_types() -> set[str]:
    """Return all valid job types: builtins plus every registered engine name."""
    return {"file", "gfs"} | set(list_engines())


class Config:
    """Load and validate a TOML job configuration file.

    With ``lexical`` the paths of the shares (gfs stages, engine outputs, ``repo_path``,
    ``storage_path``, ``out_dir`` and the restore roots) are normalised from their text without
    filesystem access on a share that may hang. ``bdbackup metrics`` reads its
    configuration so; a path written through a link then stays as written. The configuration
    file itself and ``[history] database`` are local and always resolved.
    """

    def __init__(self, path: str | Path, *, lexical: bool = False):
        self._resolve = lexical_path if lexical else real_path
        self.path = Path(path).expanduser().resolve()
        self.jobs: dict[str, Job] = {}
        self.history: HistorySettings | None = None
        self.metrics: MetricsSettings | None = None
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
                resolve = real_path if key == "database" else self._resolve
                paths[key] = resolve(self.path.parent / Path(value).expanduser())
            self.history = HistorySettings(**paths)

        if "metrics" in data:
            section = data.pop("metrics")
            try:
                settings = parse_metrics(section, self.path.parent)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            if self.history is None:
                raise ConfigError("[metrics] requires a [history] section")
            self.metrics = settings

        destinations: dict[tuple[str, str | None, str], str] = {}
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
            copy = None
            if "copy" in section:
                try:
                    copy, destination = self._load_copy(job_type, section)
                    if destination in destinations:
                        other = destinations[destination]
                        raise ValueError(f"copy.destination is already used by job {other!r}")
                    destinations[destination] = name
                except ValueError as exc:
                    raise ConfigError(f"Job {name!r}: {exc}") from exc
            if job_type == "gfs":
                try:
                    params = parse_gfs_job(section, self.path.parent, self._resolve)
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
                restore_root = self._resolve(self.path.parent / Path(value).expanduser())
            params = {k: v for k, v in section.items() if k not in {"type", "restore_root"}}
            if job_type == "pbm":
                self._check_pbm_settings(name, params)
            else:
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
                        if self._resolve is lexical_path:
                            params[key] = lexical_path(params[key])
            self.jobs[name] = Job(
                name=name, type=job_type, params=params, restore_root=restore_root,
                active=active, copy=copy,
            )

        if any(job.type == "gfs" for job in self.jobs.values()):
            try:
                check_gfs_config(self.jobs, self.history, self._resolve)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        self._check_pgbackrest()
        self._check_pbm_layouts()

    def _load_copy(
        self, job_type: str, section: dict[str, Any],
    ) -> tuple[CopySettings, tuple[str, str | None, str]]:
        if job_type != "file" and (
            job_type not in _COPY_ENGINES
            or get_engine(job_type).backend is not _COPY_ENGINES[job_type]
        ):
            raise ValueError(
                "copy is supported only by built-in file, mysqldump, pgbackrest and pbm jobs"
            )
        if self.history is None:
            raise ValueError("copy requires a [history] section")
        if job_type == "file":
            if section.get("timestamp") is not True:
                raise ValueError("copy requires timestamp = true")
            destination = section.get("backup_dst")
            if not isinstance(destination, str) or not safe_artifact_name(Path(destination).name):
                raise ValueError("copy requires a shell-safe backup_dst name")
        if job_type == "pgbackrest" and not safe_artifact_name(section.get("stanza")):
            raise ValueError("copy requires a shell-safe stanza name")
        # Mysqldump percent-encodes the database label before using it in the archive name.
        return parse_copy_settings(section.pop("copy"), self.path.parent, self._resolve)

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
                job.params["first_stage"] = pgbackrest_first_stage(job, self.jobs, self._resolve)
            except ValueError as exc:
                raise ConfigError(f"Job {name!r}: {exc}") from exc
            self._check_pgbackrest_layout(name, job)
            job.params["lock_file"] = Path(f"{self.history.database}.pgbackrest-{name}.lock")
        self._check_pgbackrest_jobs_apart()

    def _check_pgbackrest_jobs_apart(self) -> None:
        """Refuse two pgbackrest jobs that export one stanza into one ``out_dir``.

        They hold different locks, and each run removes the stanza's temporary files in
        ``out_dir``, so one could remove the file the other is writing.
        """
        seen: dict[tuple[Path, str], str] = {}
        for name, job in self.jobs.items():
            stanza = job.params.get("stanza")
            if job.type != "pgbackrest" or not isinstance(stanza, str):
                continue  # the engine refuses a stanza that is not a string when it is built
            key = (self._resolve(Path(job.params["out_dir"])), stanza)
            if key in seen:
                raise ConfigError(
                    f"Job {name!r}: job {seen[key]!r} already exports stanza {stanza!r} into "
                    "this out_dir; give each job its own out_dir"
                )
            seen[key] = name

    def _check_pgbackrest_layout(self, name: str, job: Job) -> None:
        """Keep the repository apart from everything bdbackup writes or locks.

        bdbackup never writes under ``repo_path``; the units, the gfs rotation, the history
        database with its lock and journal files, and the restores do.
        """
        if "repo_path" not in job.params:
            return  # the engine refuses the job when it is built
        repo = self._resolve(Path(job.params["repo_path"]))
        out_dir = self._resolve(Path(job.params["out_dir"]))
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

    def _check_pbm_settings(self, name: str, params: dict[str, Any]) -> None:
        if self.history is None:
            raise ConfigError(f"Job {name!r}: pbm jobs require a [history] section")
        if not safe_artifact_name(name):
            raise ConfigError(f"Job {name!r}: pbm job name must be shell-safe")
        unknown = params.keys() - _PBM_KEYS
        if unknown:
            raise ConfigError(f"Job {name!r}: unknown pbm key(s): {', '.join(sorted(unknown))}")
        for key in _PBM_PATHS:
            if key not in params:
                raise ConfigError(f"Job {name!r}: {key} is required for pbm")
            value = params[key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"Job {name!r}: {key} must be a nonempty path string")
            params[key] = self._resolve(self.path.parent / Path(value).expanduser())
        if params.get("backup_type", "logical") not in ("logical", "physical"):
            raise ConfigError(f"Job {name!r}: backup_type must be logical or physical")
        for key in ("binary", "mongodb_uri"):
            if key in params and (not isinstance(params[key], str) or not params[key].strip()):
                raise ConfigError(f"Job {name!r}: {key} must be a nonempty string")

    def _check_pbm_layouts(self) -> None:
        stage_paths = [
            ("gfs stage path", path)
            for job in self.jobs.values() if job.type == "gfs"
            for stage in job.params["stage"]
            for path in stage.paths
        ]
        for name, job in self.jobs.items():
            if job.type != "pbm":
                continue
            storage = job.params["storage_path"]
            for key, path in (
                ("out_dir", job.params["out_dir"]),
                ("[history] database", self.history.database),
                ("[history] restore_root", self.history.restore_root),
                ("restore_root", job.restore_root),
                *stage_paths,
            ):
                if path is not None and (
                    path.is_relative_to(storage) or storage.is_relative_to(path)
                ):
                    raise ConfigError(f"Job {name!r}: {key} and storage_path must not overlap")

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
