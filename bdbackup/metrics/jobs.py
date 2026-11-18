"""The generic job families: ``bdbackup_job_*`` for every job type that records ``last_runs``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from bdbackup.engines import list_engines
from bdbackup.metrics import families as f
from bdbackup.metrics.collector import Context, Request, Results
from bdbackup.metrics.render import Sample

if TYPE_CHECKING:
    from bdbackup.config import Job
    from bdbackup.history import BackupRecord

# The job types whose runs record last_runs in 0.7.3. 0.7.4 adds file, mysqldump and xtrabackup
# here, with their recording and a collector; no family changes.
LAST_RUN_JOB_TYPES = frozenset({"pgbackrest", "gfs"})


def epoch(text: str) -> int:
    """Unix seconds of an ISO 8601 timestamp the history database stores."""
    return int(datetime.fromisoformat(text).timestamp())


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _restore_label(record: BackupRecord) -> object:
    try:
        info = json.loads(record.restore_info)
    except ValueError:
        return None
    return info.get("label") if isinstance(info, dict) else None


def _describes(job: Job, facts: dict, record: BackupRecord) -> bool:
    """Whether the facts of the job's last run describe ``record``, its newest success.

    The job type's collector says so through ``describes(facts, record)``. Without one, the
    facts describe the record when both name the same ``label``.
    """
    engine = list_engines().get(job.type)
    hook = getattr(getattr(engine, "metrics", None), "describes", None)
    if callable(hook):
        return bool(hook(facts, record))
    label = facts.get("label")
    return label is not None and label == _restore_label(record)


class JobCollector:
    """``bdbackup_job_last_run_*``, ``running_since``, ``last_success_*`` and the phases."""

    def families(self):
        return ()

    def probes(self, job: Job | None, ctx: Context) -> list[Request]:
        return []

    def collect(self, job: Job | None, ctx: Context, results: Results) -> Iterator[Sample]:
        labels = {"job_name": job.name, "job_type": job.type}
        data = ctx.data
        run = data.last_runs.get(job.name)
        if run is not None:
            yield Sample(f.JOB_LAST_RUN_TIMESTAMP, epoch(run.finished_at), labels)
            yield Sample(f.JOB_LAST_RUN_EXIT_CODE, run.exit_code, labels)
            yield Sample(f.JOB_LAST_RUN_RESULT, 1, {**labels, "result": run.result})
        if job.type == "gfs":
            yield from self._gfs(job, ctx, labels)
            return
        started = data.running.get(job.name)
        if started is not None:
            yield Sample(f.JOB_RUNNING_SINCE, epoch(started), labels)
        newest = next((r for r in reversed(data.records) if r.job_name == job.name), None)
        if newest is None:
            return
        if newest.completed_at:
            yield Sample(f.JOB_LAST_SUCCESS_TIMESTAMP, epoch(newest.completed_at), labels)
        if newest.size_bytes is not None:
            yield Sample(f.JOB_LAST_SUCCESS_BYTES, newest.size_bytes, labels)
        if run is not None and run.facts and _describes(job, run.facts, newest):
            phases = run.facts.get("phases")
            for phase, seconds in (phases.items() if isinstance(phases, dict) else ()):
                if _number(seconds):
                    yield Sample(f.JOB_LAST_SUCCESS_PHASE, seconds, {**labels, "phase": phase})

    @staticmethod
    def _gfs(job: Job, ctx: Context, labels: dict[str, str]) -> Iterator[Sample]:
        """A gfs job's unfinished and successful runs come from ``gfs_runs``; it has no size."""
        runs = [r for r in ctx.data.gfs_runs if r.gfs_job == job.name]
        running = next((r for r in runs if r.status == "running"), None)
        if running is not None:
            yield Sample(f.JOB_RUNNING_SINCE, epoch(running.started_at), labels)
        done = next(
            (r for r in reversed(runs) if r.status == "completed" and r.exit_code == 0), None
        )
        if done is not None and done.finished_at:
            yield Sample(f.JOB_LAST_SUCCESS_TIMESTAMP, epoch(done.finished_at), labels)
