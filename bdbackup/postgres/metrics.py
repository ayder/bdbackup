"""The pgBackRest collector for ``bdbackup metrics``: the last export and the repository."""

from __future__ import annotations

import time
from collections.abc import Iterable

from bdbackup.metrics.collector import Context, MetricError, Request, Results
from bdbackup.metrics.registry import Family
from bdbackup.metrics.render import Sample

_clock = time.monotonic  # the timer of the info call, a module attribute so tests can set it


class PgBackRestCollector:
    """Not yet."""

    def families(self) -> Iterable[Family]:
        return ()

    def probes(self, job, ctx: Context) -> list[Request]:
        return []

    def collect(self, job, ctx: Context, results: Results) -> Iterable[Sample | MetricError]:
        return ()
