"""Run every collector for one metrics run, each isolated from the others."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from bdbackup.metrics.collector import MetricError, Request, Results
from bdbackup.metrics.render import Sample

if TYPE_CHECKING:
    from bdbackup.config import Job
    from bdbackup.history import History


@dataclass
class Collected:
    """The samples of every collector that worked, and the sources that failed."""

    samples: list[Sample] = field(default_factory=list)
    errors: list[MetricError] = field(default_factory=list)


def collect_all(
    jobs: Mapping[str, Job],
    history: History,
    *,
    now: datetime,
    timeout_seconds: int,
    probe: Callable[[list[Request], int], Results],
) -> Collected:
    """Not yet."""
    return Collected()
