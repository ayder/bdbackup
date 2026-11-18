"""Write the textfile atomically, for node_exporter's textfile collector to read."""

from __future__ import annotations

from pathlib import Path


def write(path: Path, text: str) -> None:
    """Not yet."""
