"""Prepare a GFS job's stage directories and destination markers (spec 9)."""

from __future__ import annotations

from collections.abc import Callable

from bdbackup.config import Config, Job


def initialize(config: Config, job: Job, out: Callable[[str], None]) -> int:
    return 0
