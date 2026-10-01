"""Prepare a GFS job's stage directories and destination markers (spec 9)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from bdbackup.config import Job
from bdbackup.gfs.stages import MARKER
from bdbackup.validation import _access, os_user

REMINDER = ("Run --initialize-structure only while every destination's filesystem is mounted, "
            "and as the OS user that runs GFS.")


def marker_text(job: Job, stage: int) -> str:
    return (
        "# GFS writes to this path only while this file exists.\n"
        "# Written by bdbackup run --initialize-structure.\n"
        f"bdbackup_version = {json.dumps(version('bdbackup'))}\n"
        f"gfs_job = {json.dumps(job.name)}\n"
        f"stage = {stage}\n"
        f"written_at = {json.dumps(datetime.now(UTC).isoformat())}\n"
    )


def _write_marker(path: Path, text: str) -> None:
    """Rewrite in place, so the marker never disappears while a GFS run checks it."""
    fd = os.open(path / MARKER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare(path: Path, marker: str | None) -> str:
    """Return the report for one stage path; raise ValueError or OSError on failure."""
    if path.is_dir():
        state = "exists"
    elif path.exists():
        raise ValueError("not a directory")
    elif not path.parent.is_dir():
        raise ValueError(f"parent does not exist: {path.parent}")
    else:
        path.mkdir()
        state = "created"
    if not _access(path, os.W_OK | os.X_OK):
        raise ValueError(f"not writable by OS user {os_user()}")
    if marker is None:
        return state
    _write_marker(path, marker)
    return f"{state}, marker written"


def initialize(job: Job, out: Callable[[str], None]) -> int:
    failed = False
    for index, stage in enumerate(job.params["stage"], 1):
        marker = marker_text(job, index) if index > 1 else None
        for path in stage.paths:
            try:
                out(f"{path}: {_prepare(path, marker)}")
            except (OSError, ValueError) as exc:
                failed = True
                out(f"FAIL {path}: {exc}")
    out(REMINDER)
    return 1 if failed else 0
