"""The ``[metrics]`` section of the configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class MetricsSettings:
    """Where the textfile goes, and how long a metrics run may wait on a share or pgBackRest."""

    path: Path
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
