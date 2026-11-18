"""The pgBackRest collector for ``bdbackup metrics``: the last export and the repository.

The export series come from the history database. The repository series come from one
``pgbackrest info --output=json`` call per active job, made through :meth:`PgBackRest.info`, so
``subprocess.run`` stays inside :meth:`PgBackRest._run`. The WAL arrival time is the mtime of one
archived file, which the repository probe child reads; this module never touches the repository
itself (D8, D10), and builds paths lexically.

``PgBackRest`` is imported where it is used: ``pgbackrest.py`` imports this module to register
the collector, so importing it back at the top would be circular.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bdbackup.metrics.collector import Context, ListPrefixRequest, MetricError, Request, Results
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
_HEX24 = re.compile(r"[0-9A-Fa-f]{24}")
_ARCHIVE_ID = re.compile(r"[0-9A-Za-z_.-]+")

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
    Family(
        f"{_PREFIX}wal_archive_present",
        "1 when the archive of the current database has both a min and a max, else 0.",
        (*JS, "db_id"),
    ),
    Family(
        f"{_PREFIX}wal_archive_max_timeline",
        "Timeline of the newest archived WAL segment.",
        (*JS, "db_id"),
    ),
    Family(
        f"{_PREFIX}wal_archive_max_segment",
        "Number of the newest archived WAL segment (log << 32 | segment); it only grows.",
        (*JS, "db_id"),
    ),
    Family(
        f"{_PREFIX}wal_archive_max_mtime_seconds",
        "Modification time of the file of the newest archived WAL segment.",
        (*JS, "db_id"),
    ),
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
class _Wal:
    """The newest archived segment of the current database, as ``info`` reports it."""

    db_id: int
    present: bool
    high: str | None = None  # the max, only when it is 24 hex digits
    directory: Path | None = None  # where its file is, when that is known


@dataclass(frozen=True)
class _Info:
    """What the info call of one job came to: the stanza's entry or why not."""

    entry: dict | None
    reason: str | None = None
    seconds: float | None = None
    repo_path: Path | None = None


def _wal(entry: dict, repo_path: Path, stanza: str) -> _Wal | None:
    """The WAL archive of the stanza's current database: the highest ``db[].id``."""
    ids = [
        d["id"]
        for d in entry.get("db") or ()
        if isinstance(d, dict) and isinstance(d.get("id"), int) and not isinstance(d["id"], bool)
    ]
    if not ids:
        return None
    db_id = max(ids)
    archive = next(
        (a for a in entry.get("archive") or () if _dig(a, "database", "id") == db_id), None
    )
    low, high = (archive or {}).get("min"), (archive or {}).get("max")
    if not (isinstance(low, str) and low and isinstance(high, str) and high):
        return _Wal(db_id, False)
    if not _HEX24.fullmatch(high):
        return _Wal(db_id, True)
    archive_id = archive.get("id")
    directory = None
    if isinstance(archive_id, str) and _ARCHIVE_ID.fullmatch(archive_id) and archive_id != "..":
        directory = repo_path / "archive" / stanza / archive_id / high[:16]
    return _Wal(db_id, True, high, directory)


class PgBackRestCollector:
    """The pgbackrest job's export series, repository series and WAL archive freshness."""

    def families(self) -> Iterable[Family]:
        return FAMILIES

    def describes(self, facts: dict, record) -> bool:
        """Whether the facts of the job's last run are those of ``record``: the same label."""
        label = facts.get("label")
        return label is not None and label == restore_label(record)

    def probes(self, job: Job, ctx: Context) -> list[Request]:
        """Make the info call (once per job and run) and ask for the WAL file's directory.

        The call has to come first: the file to look for is named by the ``max`` it prints. The
        probe children start after every collector's ``probes`` has returned, so the probes
        still run together under one deadline; the info calls run one after another, each
        limited by ``timeout_seconds``.
        """
        info = self._info(job, ctx)
        wal = _wal(info.entry, info.repo_path, job.params["stanza"]) if info.entry else None
        if wal is None or wal.directory is None:
            return []
        return [ListPrefixRequest(wal.directory, wal.high + "-")]

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
        wal = _wal(info.entry, info.repo_path, labels["stanza"])
        if wal is not None:
            yield from self._wal(job, wal, labels, results)

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
                info = _Info(entry, None, _clock() - started, engine.repo_path)
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

    @staticmethod
    def _wal(job: Job, wal: _Wal, labels: dict, results: Results) -> Iterator[Sample | MetricError]:
        with_db = {**labels, "db_id": str(wal.db_id)}
        yield Sample(f"{_PREFIX}wal_archive_present", 1 if wal.present else 0, with_db)
        if wal.high is None:  # no max, or one that is not 24 hex digits
            return
        yield Sample(f"{_PREFIX}wal_archive_max_timeline", int(wal.high[:8], 16), with_db)
        segment = (int(wal.high[8:16], 16) << 32) | int(wal.high[16:24], 16)
        yield Sample(f"{_PREFIX}wal_archive_max_segment", segment, with_db)
        listing = results.listing(wal.directory, wal.high + "-") if wal.directory else None
        if listing is not None and len(listing.entries) == 1:
            mtime = int(listing.entries[0][1])
            yield Sample(f"{_PREFIX}wal_archive_max_mtime_seconds", mtime, with_db)
        else:  # no file, two files, a failed listing, or a probe that did not finish
            yield MetricError(SOURCE, job.name, "wal_mtime_unavailable")
