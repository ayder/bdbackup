"""The collector protocol: what a collector asks the probes for, and what it gets back.

A collector works in two phases. ``probes`` names the filesystem checks it needs, as requests;
the probe children run them, so no collector ever touches a share itself (D8). ``collect`` then
turns the history data and the probes' answers into samples.

This module imports neither :mod:`bdbackup.config` nor :mod:`bdbackup.cli`, because the engine
registry refers to :class:`Collector` and the configuration imports the registry. The types of
those modules are named only for type checkers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from bdbackup.config import Job
    from bdbackup.history import History
    from bdbackup.metrics.data import HistoryData
    from bdbackup.metrics.registry import Family
    from bdbackup.metrics.render import Sample

# The probe children, by name. Each runs its requests in one process under one deadline, so a
# hung destination cannot hide what the repository probe read, and the reverse (D8, D10).
DESTINATION = "destination"
REPOSITORY = "repository"


@dataclass(frozen=True)
class StatRequest:
    """``os.stat`` of ``path``, in the probe named ``probe``."""

    path: Path
    probe: str = DESTINATION


@dataclass(frozen=True)
class StatvfsRequest:
    """``os.statvfs`` of ``path``, in the probe named ``probe``: the capacity of its filesystem."""

    path: Path
    probe: str = DESTINATION


@dataclass(frozen=True)
class ListPrefixRequest:
    """The entries of ``directory`` whose name starts with ``prefix``, with their mtimes."""

    directory: Path
    prefix: str
    probe: str = REPOSITORY


Request = StatRequest | StatvfsRequest | ListPrefixRequest


@dataclass(frozen=True)
class StatAnswer:
    """The facts of a successful ``os.stat``; the field names are ``os.stat_result``'s."""

    st_mode: int
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int


@dataclass(frozen=True)
class StatvfsAnswer:
    """The facts of a successful ``os.statvfs``; the field names are ``os.statvfs_result``'s."""

    f_blocks: int
    f_bavail: int
    f_frsize: int


@dataclass(frozen=True)
class ListAnswer:
    """The ``(name, mtime in seconds)`` of each entry of a listing that starts with the prefix."""

    entries: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class Failure:
    """A request the probe tried and the operating system refused (``errno`` as it reported)."""

    errno: int | None


@dataclass(frozen=True)
class Results:
    """What the probes reported.

    ``answers`` holds one entry per request that got a line; a request without one is absent
    (its probe timed out or failed). ``status`` maps each probe that ran to ``ok``, ``timeout``
    or ``failed``.
    """

    answers: Mapping[Request, StatAnswer | StatvfsAnswer | ListAnswer | Failure] = field(
        default_factory=dict
    )
    status: Mapping[str, str] = field(default_factory=dict)

    def stat(self, path: Path, probe: str = DESTINATION) -> StatAnswer | None:
        """The stat facts of ``path``; ``None`` when it is missing, refused or never reported."""
        answer = self.answers.get(StatRequest(path, probe))
        return answer if isinstance(answer, StatAnswer) else None

    def statvfs(self, path: Path, probe: str = DESTINATION) -> StatvfsAnswer | None:
        """The capacity facts of ``path``; ``None`` when it failed or never reported."""
        answer = self.answers.get(StatvfsRequest(path, probe))
        return answer if isinstance(answer, StatvfsAnswer) else None

    def listing(self, directory: Path, prefix: str, probe: str = REPOSITORY) -> ListAnswer | None:
        """The matching entries of ``directory``; ``None`` when it failed or never reported."""
        answer = self.answers.get(ListPrefixRequest(directory, prefix, probe))
        return answer if isinstance(answer, ListAnswer) else None

    def probe_status(self, probe: str) -> str | None:
        """``ok``, ``timeout`` or ``failed``; ``None`` when that probe did not run."""
        return self.status.get(probe)


class MetricError(NamedTuple):
    """One failed source, written as ``bdbackup_metrics_error{source, job_name, reason}``."""

    source: str
    job_name: str
    reason: str


@dataclass(frozen=True)
class Context:
    """What every collector reads: the clock, the configured jobs and the history, read once.

    ``timeout_seconds`` is ``[metrics] timeout_seconds``: the limit of a command a collector runs.
    ``scratch`` is memory for one run, which a collector may use to hand what its ``probes`` call
    found to its ``collect`` call (an engine's command output, say); key it by engine and job.
    """

    now: datetime
    jobs: Mapping[str, Job]
    history: History
    data: HistoryData
    timeout_seconds: int = 60
    scratch: dict = field(default_factory=dict)


class Collector(Protocol):
    """A job type's metrics (``EngineInfo.metrics``), or a built-in collector.

    ``job`` is the configured job being collected, or ``None`` for a collector that covers the
    whole run. A collector may also have ``describes(facts, record) -> bool``, which says
    whether the ``last_runs`` facts of a job describe its newest successful backup.
    """

    def families(self) -> Iterable[Family]:
        """The families this collector writes, declared before anything is collected."""

    def probes(self, job: Job | None, ctx: Context) -> list[Request]:
        """The filesystem checks the collector needs; it never makes them itself."""

    def collect(
        self, job: Job | None, ctx: Context, results: Results
    ) -> Iterable[Sample | MetricError]:
        """The samples, from ``ctx`` and the probes' answers.

        An engine's collector may also yield a :class:`MetricError` for its own source: the
        ``source`` is the engine's name and the ``job_name`` the job's. That is an expected
        failure (the command timed out), not a failure of the collector.
        """
