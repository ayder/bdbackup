"""The pgBackRest collector for ``bdbackup metrics``: the last export and the repository.

The export series come from the history database. The repository series come from one
``pgbackrest info --output=json`` call per active job, made through :meth:`PgBackRest.info`, so
``subprocess.run`` stays inside :meth:`PgBackRest._run`. Nothing here touches the repository itself
(D8); the paths it builds are lexical.

``PgBackRest`` is imported where it is used: ``pgbackrest.py`` imports this module to register
the collector, so importing it back at the top would be circular.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bdbackup.metrics.collector import Context, MetricError, Request, Results
from bdbackup.metrics.jobs import restore_label
from bdbackup.metrics.registry import Family
from bdbackup.metrics.render import Sample

if TYPE_CHECKING:
    from bdbackup.config import Job

_clock = time.monotonic  # the timer of the info call, a module attribute so tests can set it

SOURCE = "pgbackrest"
_PREFIX = "bdbackup_pgbackrest_"
JS = ("job_name", "stanza")
_TYPES = ("full", "diff", "incr")

FAMILIES = (
    Family(
        f"{_PREFIX}export_info", "1; the label of the newest successful export.", (*JS, "label")
    ),
    Family(
        f"{_PREFIX}export_full_age_seconds",
        "Seconds from the full's stop time to the start of its export.",
        JS,
    ),
    Family(f"{_PREFIX}info_duration_seconds", "Seconds the pgbackrest info call took.", JS),
    Family(f"{_PREFIX}stanza_status", "The status code of the stanza; 0 is ok.", JS),
    Family(
        f"{_PREFIX}last_backup_timestamp_seconds",
        "Stop time of the newest backup of the type.",
        (*JS, "type"),
    ),
    Family(
        f"{_PREFIX}last_backup_bytes",
        "Database size of the newest backup of the type.",
        (*JS, "type"),
    ),
    Family(
        f"{_PREFIX}last_backup_repo_bytes",
        "Repository size of the newest backup of the type.",
        (*JS, "type"),
    ),
    Family(f"{_PREFIX}backups", "Backups of the type held in the repository.", (*JS, "type")),
)


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _dig(value: object, *path: str) -> object:
    """``value[path[0]][path[1]]...``, or ``None`` when any step is missing or not a dict."""
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


@dataclass(frozen=True)
class _Info:
    """What the info call of one job came to: the stanza's entry or why not."""

    entry: dict | None
    reason: str | None = None
    seconds: float | None = None


class PgBackRestCollector:
    """The pgbackrest job's export series and repository series."""

    def families(self) -> Iterable[Family]:
        return FAMILIES

    def describes(self, facts: dict, record) -> bool:
        """Whether the facts of the job's last run are those of ``record``: the same label."""
        label = facts.get("label")
        return label is not None and label == restore_label(record)

    def probes(self, job: Job, ctx: Context) -> list[Request]:
        """Make the info call, once per job and run; it asks the probes for nothing.

        The info calls run one after another, each limited by ``timeout_seconds``.
        """
        self._info(job, ctx)
        return []

    def collect(self, job: Job, ctx: Context, results: Results) -> Iterator[Sample | MetricError]:
        labels = {"job_name": job.name, "stanza": job.params["stanza"]}
        yield from self._export(job, ctx, labels)
        info = ctx.scratch.get((SOURCE, job.name))
        if info is None:  # probes() did not run for this job
            return
        if info.reason is not None:
            yield MetricError(SOURCE, job.name, info.reason)
            return
        yield from self._repository(info, labels)

    def _info(self, job: Job, ctx: Context) -> _Info:
        from bdbackup.postgres.pgbackrest import PgBackRest, PgBackRestError

        engine = PgBackRest(**job.params)  # as build_backend does; it only normalises paths
        started = _clock()
        try:
            document = engine.info(timeout=ctx.timeout_seconds)
        except PgBackRestError as exc:
            info = _Info(None, exc.reason)
        else:
            entry = next((s for s in document if s.get("name") == engine.stanza), None)
            if entry is None:
                info = _Info(None, "output_invalid")
            else:
                info = _Info(entry, None, _clock() - started)
        ctx.scratch[(SOURCE, job.name)] = info
        return info

    def _export(self, job: Job, ctx: Context, labels: dict) -> Iterator[Sample]:
        newest = next((r for r in reversed(ctx.data.records) if r.job_name == job.name), None)
        label = restore_label(newest) if newest is not None else None
        if not isinstance(label, str):
            return
        yield Sample(f"{_PREFIX}export_info", 1, {**labels, "label": label})
        run = ctx.data.last_runs.get(job.name)
        if run is not None and run.facts and self.describes(run.facts, newest):
            age = run.facts.get("full_age_seconds")
            if _number(age):
                yield Sample(f"{_PREFIX}export_full_age_seconds", age, labels)

    @staticmethod
    def _repository(info: _Info, labels: dict) -> Iterator[Sample]:
        entry = info.entry
        yield Sample(f"{_PREFIX}info_duration_seconds", info.seconds, labels)
        code = _dig(entry, "status", "code")
        if _number(code):
            yield Sample(f"{_PREFIX}stanza_status", code, labels)
        for kind in _TYPES:
            typed = [
                b
                for b in entry.get("backup") or ()
                if isinstance(b, dict) and b.get("type") == kind
            ]
            yield Sample(f"{_PREFIX}backups", len(typed), {**labels, "type": kind})
            dated = [
                b for b in typed
                if _number(_dig(b, "timestamp", "stop")) and isinstance(b.get("label"), str)
            ]
            if not dated:
                continue
            newest = max(dated, key=lambda b: (b["timestamp"]["stop"], b["label"]))
            with_type = {**labels, "type": kind}
            yield Sample(
                f"{_PREFIX}last_backup_timestamp_seconds", newest["timestamp"]["stop"], with_type
            )
            for name, path in (
                ("last_backup_bytes", ("info", "size")),
                ("last_backup_repo_bytes", ("info", "repository", "size")),
            ):
                value = _dig(newest, *path)
                if _number(value):
                    yield Sample(f"{_PREFIX}{name}", value, with_type)
