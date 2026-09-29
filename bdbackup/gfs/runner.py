"""One GFS run: lock, load, decide, act, report (spec 2 §4)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from bdbackup.config import Config, Job
from bdbackup.gfs.actions import Transport


def job_lock_path(config: Config, name: str) -> Path:
    return Path(f"{config.history.database}.gfs-{name}.lock")


def run_job(config: Config, job: Job, *, now: datetime | None = None,
            out: Callable[[str], None] = print, transport: Transport | None = None) -> int:
    return 1
