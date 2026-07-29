"""Shared utilities for bdbackup."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path


@contextmanager
def process_lock(lock_path: str | Path):
    """Advisory process lock using fcntl (POSIX only).

    Raises BlockingIOError if another process already holds the lock.
    """
    lock_file = Path(lock_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def config_dir() -> Path:
    """Return the bdbackup user configuration directory.

    Resolution order: $BDBACKUP_CONFIG_DIR, then $XDG_CONFIG_HOME/bdbackup,
    then ~/.config/bdbackup. The directory is not created implicitly.
    """
    env = os.environ.get("BDBACKUP_CONFIG_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "bdbackup"
    return Path.home() / ".config" / "bdbackup"


def today_stamp() -> str:
    """Return a UTC YYYY-MM-DD-HHMMSS style timestamp."""
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")


def redact_cmd(cmd: Iterable[str | Path], secrets: Iterable[str]) -> list[str]:
    """Return a copy of *cmd* with any secret value replaced by '***'."""
    secret_set = {str(s) for s in secrets if s}
    return ["***" if str(part) in secret_set else str(part) for part in cmd]
