"""The ``[metrics]`` section of the configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60
_KEYS = {"path", "timeout_seconds"}


@dataclass(frozen=True)
class MetricsSettings:
    """Where the textfile goes, and how long a metrics run may wait on a share or pgBackRest."""

    path: Path
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


def parse_metrics(section: object, base: Path) -> MetricsSettings:
    """The settings of a ``[metrics]`` table; a relative ``path`` is taken from ``base``.

    Raises ValueError with the message to show for a table that is not valid.
    """
    if not isinstance(section, dict) or set(section) - _KEYS:
        raise ValueError("[metrics] accepts only path and timeout_seconds")
    if "path" not in section:
        raise ValueError("[metrics] path is required")
    value = section["path"]
    if not isinstance(value, str) or not value.strip() or not value.endswith(".prom"):
        raise ValueError("[metrics] path must be a nonempty filesystem path ending in .prom")
    timeout = section.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise ValueError("[metrics] timeout_seconds must be a positive whole number")
    return MetricsSettings((base / Path(value).expanduser()).resolve(), timeout)
