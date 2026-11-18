"""The GFS families, read from the history database; only the markers are probed (D8, D11)."""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING

from bdbackup.gfs.policy import bucket_span
from bdbackup.gfs.stages import MARKER, Stage
from bdbackup.metrics import families as f
from bdbackup.metrics.collector import Context, Request, Results, StatRequest
from bdbackup.metrics.data import GFS_COUNTS, HistoryData
from bdbackup.metrics.jobs import epoch
from bdbackup.metrics.render import Sample

if TYPE_CHECKING:
    from bdbackup.config import Job


@dataclass(frozen=True)
class _Fact:
    """One unit of the gfs job, as the database tells it."""

    source: str  # the backup job whose unit it is: the unit's series
    stage: int | None  # index of the highest stage holding a location; None once deleted
    day: date  # the unit day: the local date its oldest member completed
    newest: int  # Unix seconds: its newest member's completion
    oldest: int  # its oldest member's completion
    size: int


def _facts(job: Job, data: HistoryData) -> list[_Fact]:
    """Every unit this gfs job manages, deleted ones too (those have no stage).

    Only the database is read. A unit is in the highest stage that holds a location of it; a
    successful record under the first stage that no ``gfs_units`` row names is a first-stage
    unit. A unit with a location in a stage that is not configured (the run refuses it), and
    one that is another gfs job's, is left out.
    """
    stages = job.params["stage"]
    first = stages[0].paths[0]

    def index_of(path: Path) -> int | None:
        return next((i for i, stage in enumerate(stages) if path in stage.paths), None)

    by_unit = defaultdict(list)
    for record in data.records:
        if record.unit and record.checksum:
            by_unit[record.unit].append(record)
    facts = []
    for unit_text, records in by_unit.items():
        row = data.units.get(unit_text)
        # A record not judged yet (it came after the last run) counts, as it will unless gone.
        members = [r for r in records if data.judged is None or data.judged.get(r.id, True)]
        if not members:
            continue
        unit = Path(unit_text)
        if row is not None and row.deleted:
            if not unit.is_relative_to(first):
                continue
            index = None  # kept only for where the job's first unit was
        elif row is None:
            if not unit.is_relative_to(first):
                continue
            index = 0
        else:
            if not unit.is_relative_to(first) and not any(
                index_of(path) is not None for path in row.stages
            ):
                continue  # another gfs job's unit
            indexes = [index_of(path) for path in row.stages]
            if not indexes or None in indexes:
                continue
            index = max(indexes)
        stamps = [epoch(r.completed_at) for r in members if r.completed_at]
        if not stamps:
            continue
        facts.append(_Fact(
            members[0].job_name, index, datetime.fromtimestamp(min(stamps)).date(),
            max(stamps), min(stamps), sum(r.size_bytes or 0 for r in members),
        ))
    return facts


def _unmanaged(job: Job, data: HistoryData) -> dict[str, list[int]]:
    """``[units, bytes]`` of the records the gfs job does not manage, by reason (spec 4.5).

    The rule is ``store.load``'s: ``no_checksum`` is a successful record under the first stage
    with no unit or no checksum; ``unavailable`` is a record of one of the job's units that was
    missing or replaced when GFS first judged it. A record not judged yet is not counted.
    """
    stages = job.params["stage"]
    first = stages[0].paths[0]
    paths = {path for stage in stages for path in stage.paths}
    found = {"no_checksum": [0, 0], "unavailable": [0, 0]}
    by_unit = defaultdict(list)
    for record in data.records:
        if record.unit and record.checksum:
            by_unit[record.unit].append(record)
        elif record.path and Path(record.path).is_relative_to(first):
            found["no_checksum"][0] += 1
            found["no_checksum"][1] += record.size_bytes or 0
    for unit_text, records in by_unit.items():
        row = data.units.get(unit_text)
        if row is not None and row.deleted:
            continue
        if not Path(unit_text).is_relative_to(first) and (
            row is None or not any(path in paths for path in row.stages)
        ):
            continue  # another gfs job's unit
        for record in records:
            if data.judged is not None and data.judged.get(record.id) is False:
                found["unavailable"][0] += 1
                found["unavailable"][1] += record.size_bytes or 0
    return found


def stage_figures(job: Job, data: HistoryData) -> list[tuple[int, int]]:
    """``(units, bytes)`` of each stage, in order, as the GFS report counts them."""
    figures = [[0, 0] for _ in job.params["stage"]]
    for fact in _facts(job, data):
        if fact.stage is not None:
            figures[fact.stage][0] += 1
            figures[fact.stage][1] += fact.size
    return [(units, size) for units, size in figures]


def _buckets(period: str, after: date, upto: date) -> list[tuple[date, date]]:
    """The buckets of ``period`` wholly inside the days ``after < day <= upto``, oldest first."""
    buckets = []
    day = after + timedelta(days=1)
    while day <= upto:
        first, last = bucket_span(day, period)
        if first > after and last <= upto:
            buckets.append((first, last))
        day = last + timedelta(days=1)
    return buckets


def _holds(days: list[date], first: date, last: date) -> bool:
    """Whether the sorted ``days`` have one from ``first`` to ``last``."""
    at = bisect_left(days, first)
    return at < len(days) and days[at] <= last


def _coverage(
    stages: tuple[Stage, ...], today: date, facts: list[_Fact]
) -> dict[tuple[int, str], tuple[int, int, int | None]]:
    """``(expected, filled, newest missing)`` of each stage index and source job (spec 4.1).

    Stage *i* holds the unit days after ``keep.floor(today)`` and up to the floor of the stage
    before it (today, for the first). Its slots are the buckets wholly inside that window, less
    those that end before the source's oldest unit ever managed and, in the first stage, those
    that start after its newest unit. A slot is filled by a unit of the source in any stage.
    """
    floors = [stage.keep.floor(today) for stage in stages]
    uppers = [today, *floors[:-1]]
    found = {}
    for source in sorted({fact.source for fact in facts if fact.stage is not None}):
        mine = [fact for fact in facts if fact.source == source]
        days = sorted(fact.day for fact in mine if fact.stage is not None)
        oldest = min(fact.day for fact in mine)
        for index, stage in enumerate(stages):
            window = _buckets(stage.period, floors[index], uppers[index])
            slots = [
                (first, last) for first, last in window
                if last >= oldest and (index > 0 or first <= days[-1])
            ]
            missing = [first for first, last in slots if not _holds(days, first, last)]
            found[(index, source)] = (
                len(slots), len(slots) - len(missing),
                int(datetime.combine(missing[-1], time()).astimezone().timestamp())
                if missing else None,
            )
    return found


def _fed_first_stages(jobs: Mapping[str, Job]) -> set[Path]:
    """The first stage each pgbackrest job writes into, as the configuration stored it.

    ``gfs.stages.fed_first_stages`` computes the same set but resolves each ``out_dir``, which
    reads the share; the metrics process never does (D8).
    """
    return {
        job.params["first_stage"]
        for job in jobs.values()
        if job.type == "pgbackrest" and "first_stage" in job.params
    }


def _destination_paths(job: Job, ctx: Context) -> list[tuple[int, Path]]:
    """``(stage index from 1, path)`` of every later stage path, and the first when fed."""
    stages = job.params["stage"]
    fed = stages[0].paths[0] in _fed_first_stages(ctx.jobs)
    return [
        (index, path)
        for index, stage in enumerate(stages, 1)
        if index > 1 or fed
        for path in stage.paths
    ]


class GfsCollector:
    """``bdbackup_gfs_*`` and ``bdbackup_destination_ready`` of one gfs job."""

    def families(self):
        return ()

    def probes(self, job: Job | None, ctx: Context) -> list[Request]:
        return [StatRequest(path / MARKER) for _, path in _destination_paths(job, ctx)]

    def collect(self, job: Job | None, ctx: Context, results: Results) -> Iterator[Sample]:
        name = job.name
        finished = [r for r in ctx.data.gfs_runs if r.gfs_job == name and r.status != "running"]
        if finished and finished[-1].status == "completed":  # a run that raised has no counts
            for action in GFS_COUNTS:
                count = finished[-1].counts[action]
                if count is not None:
                    yield Sample(f.GFS_LAST_RUN_UNITS, count, {"job_name": name, "action": action})
        stages = job.params["stage"]
        facts = _facts(job, ctx.data)
        figures = [[0, 0] for _ in stages]
        for fact in facts:
            if fact.stage is not None:
                figures[fact.stage][0] += 1
                figures[fact.stage][1] += fact.size
        for index, (stage, (units, size)) in enumerate(zip(stages, figures, strict=True), 1):
            yield Sample(f.GFS_STAGE_INFO, 1, {
                "job_name": name, "stage": str(index), "period": stage.period,
                "keep": str(stage.keep), "path": str(stage.paths[0]),
            })
            yield Sample(f.GFS_STAGE_UNITS, units, {"job_name": name, "stage": str(index)})
            yield Sample(f.GFS_STAGE_BYTES, size, {"job_name": name, "stage": str(index)})
        for index, stage in enumerate(stages, 1):
            yield Sample(f.GFS_STAGE_VERIFY, int(stage.verify),
                         {"job_name": name, "stage": str(index)})
        yield from self._sources(job, ctx, facts)
        for (moved_job, source, target), run_at in sorted(ctx.data.last_moves.items()):
            if moved_job == name:
                yield Sample(f.GFS_LAST_MOVE, epoch(run_at), {
                    "job_name": name, "from_stage": str(source), "to_stage": str(target),
                })
        for reason, (count, size) in _unmanaged(job, ctx.data).items():
            labels = {"job_name": name, "stage": "1", "reason": reason}
            yield Sample(f.GFS_UNMANAGED_UNITS, count, labels)
            yield Sample(f.GFS_UNMANAGED_BYTES, size, labels)
        for index, path in _destination_paths(job, ctx):
            marker = results.stat(path / MARKER)
            ready = 1 if marker is not None and S_ISREG(marker.st_mode) else 0
            yield Sample(
                f.DESTINATION_READY, ready,
                {"job_name": name, "stage": str(index), "path": str(path)},
            )

    @staticmethod
    def _sources(job: Job, ctx: Context, facts: list[_Fact]) -> Iterator[Sample]:
        """Coverage, reach and size of each stage and source job (spec 4.1 to 4.3).

        The windows use the local date, as ``run_job`` does; ``ctx.now`` is UTC.
        """
        stages = job.params["stage"]
        coverage = _coverage(stages, ctx.now.astimezone().date(), facts)
        for (index, source), (expected, filled, newest_missing) in coverage.items():
            labels = {"job_name": job.name, "stage": str(index + 1), "source_job": source}
            here = [fact for fact in facts if fact.source == source and fact.stage == index]
            yield Sample(f.GFS_COVERAGE_EXPECTED, expected, labels)
            yield Sample(f.GFS_COVERAGE_FILLED, filled, labels)
            yield Sample(f.GFS_COVERAGE_MISSING, expected - filled, labels)
            if newest_missing is not None:
                yield Sample(f.GFS_COVERAGE_NEWEST_MISSING, newest_missing, labels)
            if here:
                yield Sample(f.GFS_SOURCE_NEWEST, max(fact.newest for fact in here), labels)
                yield Sample(f.GFS_SOURCE_OLDEST, min(fact.oldest for fact in here), labels)
            yield Sample(f.GFS_SOURCE_UNITS, len(here), labels)
            yield Sample(f.GFS_SOURCE_BYTES, sum(fact.size for fact in here), labels)
