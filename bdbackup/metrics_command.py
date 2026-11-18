"""``bdbackup metrics -c CONFIG``: write the Prometheus textfile; the driver of one metrics run."""

from __future__ import annotations

from pathlib import Path

from bdbackup.config import Config


def _read_config(path: Path) -> Config:
    """The full configuration, read in lexical mode; the one place a test makes it block."""
    raise NotImplementedError


def run_metrics(config_path: Path) -> int:
    """Not yet."""
    return 0
