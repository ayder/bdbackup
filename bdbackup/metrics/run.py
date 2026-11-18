"""Run every collector for one metrics run, each isolated from the others."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bdbackup.engines import list_engines
from bdbackup.history import History, HistoryError
from bdbackup.metrics import families as f
from bdbackup.metrics.collector import (
    DESTINATION,
    Collector,
    Context,
    ListPrefixRequest,
    MetricError,
    Request,
    Results,
    StatRequest,
)
from bdbackup.metrics.data import HistoryData, read_history
from bdbackup.metrics.gfs import GfsCollector
from bdbackup.metrics.jobs import LAST_RUN_JOB_TYPES, JobCollector
from bdbackup.metrics.probe import run_probes
from bdbackup.metrics.registry import Registry, engine_prefix
from bdbackup.metrics.render import Sample, render
from bdbackup.metrics.units import HistoryUnitsCollector

if TYPE_CHECKING:
    from bdbackup.config import Job

logger = logging.getLogger("bdbackup.metrics")


@dataclass
class Collected:
    """The samples of every collector that worked, and the sources that failed.

    The samples include ``bdbackup_metrics_error`` for each error.
    """

    samples: list[Sample] = field(default_factory=list)
    errors: list[MetricError] = field(default_factory=list)


@dataclass
class _Unit:
    """One collector run for one job (or for the whole run, with no job)."""

    job: Job | None
    collector: Collector
    prefix: str | None = None  # an engine's samples must be in its own families

    @property
    def name(self) -> str:
        return self.job.name if self.job is not None else ""


def _units(jobs: Mapping[str, Job], *, history_ok: bool) -> list[_Unit]:
    """The collectors to run: built-ins for the active jobs, then each engine's own."""
    units: list[_Unit] = []
    active = [job for job in jobs.values() if job.active]
    if history_ok:
        for job in active:
            if job.type in LAST_RUN_JOB_TYPES:
                units.append(_Unit(job, JobCollector()))
        units.append(_Unit(None, HistoryUnitsCollector()))
        for job in active:
            if job.type == "gfs":
                units.append(_Unit(job, GfsCollector()))
    engines = list_engines()
    for job in active:
        engine = engines.get(job.type)
        if engine is not None and engine.metrics is not None:
            units.append(_Unit(job, engine.metrics, engine_prefix(engine.name)))
    return units


def _declare(registry: Registry, units: list[_Unit]) -> dict[int, Exception]:
    """Add each engine collector's families to ``registry``; the failures, by collector."""
    failed: dict[int, Exception] = {}
    done: set[int] = set()
    for unit in units:
        key = id(unit.collector)
        if unit.prefix is None or key in done:
            continue
        done.add(key)
        try:
            for family in list(unit.collector.families()):
                if not family.name.startswith(unit.prefix):
                    raise ValueError(f"{family.name} is not in {unit.prefix}*")
                registry.declare(family.name, family.help, family.labels)
        except Exception as exc:
            failed[key] = exc
    return failed


def _check(registry: Registry, samples: list[Sample], unit: _Unit, seen: set) -> None:
    """Raise ValueError unless ``samples`` fit the registry, the collector's prefix and the run."""
    render(registry, samples)  # undeclared family, other labels, bad value, repeated series
    if unit.prefix is not None:
        for sample in samples:
            if not sample.family.startswith(unit.prefix):
                raise ValueError(f"{sample.family} is not in {unit.prefix}*")
    keys = {(s.family, tuple(sorted(s.labels.items()))) for s in samples}
    if keys & seen:
        raise ValueError("a series of another collector")
    seen |= keys


def _check_errors(unit: _Unit, errors: list[MetricError]) -> None:
    """An engine's collector may report failures of its own source and job only."""
    for error in errors:
        if unit.prefix is None or error.source != unit.job.type or error.job_name != unit.name:
            raise ValueError(f"{unit.name or 'a collector'} may not report {error}")


def collect_all(
    jobs: Mapping[str, Job],
    history: History,
    *,
    now: datetime,
    timeout_seconds: int,
    probe: Callable[[list[Request], int], Results] = run_probes,
) -> Collected:
    """Collect every family; no collector can stop another.

    A collector that raises, or whose samples do not fit the registry, is one
    ``source="collector"`` error and its samples are dropped. An unreadable history is one
    ``source="history"`` error, and the collectors that read it are left out. The destination
    probe's timeout or failure is one ``source="destination"`` error.
    """
    collected = Collected()

    def fail(source: str, job_name: str, reason: str, why: Exception | None = None) -> None:
        error = MetricError(source, job_name, reason)
        if error not in collected.errors:
            collected.errors.append(error)
        logger.warning("metrics source %s %s failed (%s): %s", source, job_name, reason, why)

    history_ok = True
    try:
        data = read_history(history)
    except (HistoryError, OSError) as exc:
        fail("history", "", "unreadable", exc)
        history_ok, data = False, HistoryData()
    ctx = Context(now, jobs, history, data, timeout_seconds)
    units = _units(jobs, history_ok=history_ok)
    registry = f.builtin_registry()
    broken = _declare(registry, units)
    survivors = []
    for unit in units:
        if id(unit.collector) in broken:
            fail("collector", unit.name, "failed", broken[id(unit.collector)])
        else:
            survivors.append(unit)

    requests: list[Request] = []
    probed = []
    for unit in survivors:
        try:
            asked = list(unit.collector.probes(unit.job, ctx))
            for request in asked:
                if not isinstance(request, StatRequest | ListPrefixRequest):
                    raise ValueError(f"not a probe request: {request!r}")
        except Exception as exc:
            fail("collector", unit.name, "failed", exc)
            continue
        requests.extend(asked)
        probed.append(unit)
    requests = list(dict.fromkeys(requests))
    results = Results()
    if requests:
        try:
            results = probe(requests, timeout_seconds)
        except Exception as exc:
            logger.warning("the metrics probe raised: %s", exc)
            results = Results({}, {request.probe: "failed" for request in requests})
    status = results.probe_status(DESTINATION)
    if status in ("timeout", "failed"):
        fail("destination", "", status)

    seen: set[Any] = set()
    for unit in probed:
        try:
            items = list(unit.collector.collect(unit.job, ctx, results))
            samples = [item for item in items if isinstance(item, Sample)]
            reported = [item for item in items if isinstance(item, MetricError)]
            if len(samples) + len(reported) != len(items):
                raise ValueError("a collector yields samples and errors only")
            _check(registry, samples, unit, seen)
            _check_errors(unit, reported)
        except Exception as exc:
            fail("collector", unit.name, "failed", exc)
        else:
            collected.samples.extend(samples)
            for error in reported:
                fail(error.source, error.job_name, error.reason)
    for error in collected.errors:
        collected.samples.append(Sample(f.ERROR, 1, {
            "source": error.source, "job_name": error.job_name, "reason": error.reason,
        }))
    return collected
