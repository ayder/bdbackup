"""``bdbackup_history_units``: the successful records of every job, by state."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from bdbackup.history import stat_identity
from bdbackup.metrics import families as f
from bdbackup.metrics.collector import DESTINATION, Context, Request, Results, StatRequest
from bdbackup.metrics.render import Sample

if TYPE_CHECKING:
    from bdbackup.config import Job
    from bdbackup.gfs.store import Place
    from bdbackup.history import BackupRecord

STATES = ("available", "unavailable", "deleted")


def _watched(record: BackupRecord, ctx: Context) -> list[Path]:
    """The paths whose ``stat`` tells whether the record is available."""
    known = ctx.data.places.get(record.id)
    if known is not None:
        return [place.member for place in known]  # a deleted unit has none
    return [Path(record.path)] if record.path else []


def _place_available(place: Place, results: Results) -> bool:
    """``gfs.store.place_available`` from the probe's ``stat``: no read of the share here."""
    answer = results.stat(place.member)
    if answer is None:
        return False
    return True if place.identity is None else stat_identity(answer) == place.identity


def _record_available(record: BackupRecord, results: Results) -> bool:
    """``BackupRecord.available`` from the probe's ``stat``."""
    if not record.path:
        return False
    answer = results.stat(Path(record.path))
    return answer is not None and stat_identity(answer) == record.identity


def state_of(record: BackupRecord, ctx: Context, results: Results) -> str:
    """``deleted`` (by GFS, from the database), else ``available`` or ``unavailable``."""
    known = ctx.data.places.get(record.id)
    if known is not None:
        if not known:
            return "deleted"
        return "available" if any(_place_available(p, results) for p in known) else "unavailable"
    return "available" if _record_available(record, results) else "unavailable"


class HistoryUnitsCollector:
    """Counts each job's successful records by state; absent when the probe did not finish."""

    def families(self):
        return ()

    def probes(self, job: Job | None, ctx: Context) -> list[Request]:
        paths = dict.fromkeys(path for r in ctx.data.records for path in _watched(r, ctx))
        return [StatRequest(path) for path in paths]

    def collect(self, job: Job | None, ctx: Context, results: Results) -> Iterator[Sample]:
        if ctx.data.user_version is not None:  # the database is local: it never waits for a share
            yield Sample(f.HISTORY_SCHEMA_VERSION, ctx.data.user_version)
        if results.probe_status(DESTINATION) not in (None, "ok"):
            return
        counts: dict[str, dict[str, int]] = {}
        for record in ctx.data.records:
            by_state = counts.setdefault(record.job_name, dict.fromkeys(STATES, 0))
            by_state[state_of(record, ctx, results)] += 1
        for name, by_state in counts.items():
            for state, count in by_state.items():
                yield Sample(f.HISTORY_UNITS, count, {"job_name": name, "state": state})
