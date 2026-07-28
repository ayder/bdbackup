"""Shared utilities for bdbackup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(name: str = "bdbackup", level: int = logging.INFO) -> logging.Logger:
    """Configure a simple stderr logger."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def read_template(path: Path | str) -> list[str]:
    """Read a template file and return non-empty, stripped lines."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Template file not found: {path}")
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def today_stamp() -> str:
    """Return a YYYY-MM-DD-HHMMSS style timestamp."""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d-%H%M%S")
