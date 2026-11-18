"""The GFS families, read from the history database; only the markers are probed (D8, D11)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING

from bdbackup.gfs.stages import MARKER, fed_first_stages
from bdbackup.metrics import families as f
from bdbackup.metrics.collector import Context, Request, Results, StatRequest
from bdbackup.metrics.data import GFS_COUNTS, HistoryData
from bdbackup.metrics.render import Sample

if TYPE_CHECKING:
    from bdbackup.config import Job


def stage_figures(job: Job, data: HistoryData) -> list[tuple[int, int]]:
    """``(units, bytes)`` of each stage, in order, as the GFS report counts them.

    Only the database is read. A unit is in the highest stage that holds a location of it; a
    successful record under the first stage that no ``gfs_units`` row names is a first-stage
    unit. A deleted unit, and one with a location in a stage that is not configured (the run
    refuses it), is left out.
    """
    stages = job.params["stage"]
    first = stages[0].paths[0]

    def index_of(path: Path) -> int | None:
        return next((i for i, stage in enumerate(stages) if path in stage.paths), None)

    by_unit = defaultdict(list)
    for record in data.records:
        if record.unit and record.checksum:
            by_unit[record.unit].append(record)
    figures = [[0, 0] for _ in stages]
    for unit_text, records in by_unit.items():
        row = data.units.get(unit_text)
        if row is not None and row.deleted:
            continue
        # A record not judged yet (it came after the last run) counts, as it will unless gone.
        members = [r for r in records if data.judged is None or data.judged.get(r.id, True)]
        if not members:
            continue
        unit = Path(unit_text)
        if row is None:
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
        figures[index][0] += 1
        figures[index][1] += sum(r.size_bytes or 0 for r in members)
    return [(units, size) for units, size in figures]


def _destination_paths(job: Job, ctx: Context) -> list[tuple[int, Path]]:
    """``(stage index from 1, path)`` of every later stage path, and the first when fed."""
    stages = job.params["stage"]
    fed = stages[0].paths[0] in fed_first_stages(ctx.jobs)
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
        figures = stage_figures(job, ctx.data)
        for index, (stage, (units, size)) in enumerate(zip(stages, figures, strict=True), 1):
            yield Sample(f.GFS_STAGE_INFO, 1, {
                "job_name": name, "stage": str(index), "period": stage.period,
                "keep": str(stage.keep), "path": str(stage.paths[0]),
            })
            yield Sample(f.GFS_STAGE_UNITS, units, {"job_name": name, "stage": str(index)})
            yield Sample(f.GFS_STAGE_BYTES, size, {"job_name": name, "stage": str(index)})
        for index, path in _destination_paths(job, ctx):
            marker = results.stat(path / MARKER)
            ready = 1 if marker is not None and S_ISREG(marker.st_mode) else 0
            yield Sample(
                f.DESTINATION_READY, ready,
                {"job_name": name, "stage": str(index), "path": str(path)},
            )
