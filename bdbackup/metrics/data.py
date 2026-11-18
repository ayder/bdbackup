"""The history database as the collectors read it: once per run, read-only, without migrating."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bdbackup.gfs import store as gfs_store
from bdbackup.history import BackupRecord, History, LastRun

GFS_COUNTS = ("moved", "deleted", "held", "deferred", "refused", "failed", "unmanaged")


@dataclass(frozen=True)
class GfsRun:
    """One row of ``gfs_runs``: an applying GFS run."""

    id: int
    gfs_job: str
    started_at: str
    finished_at: str | None
    status: str
    exit_code: int | None
    counts: dict[str, int | None]


@dataclass(frozen=True)
class LedgerUnit:
    """One row of ``gfs_units`` and the stage path of each of its locations."""

    unit: str
    deleted: bool
    stages: tuple[Path, ...]


@dataclass(frozen=True)
class HistoryData:
    """Everything the collectors need from the database; empty when there is none yet."""

    records: tuple[BackupRecord, ...] = ()  # successful, oldest first
    running: dict[str, str] = field(default_factory=dict)  # job -> start of its oldest running
    last_runs: dict[str, LastRun] = field(default_factory=dict)
    gfs_runs: tuple[GfsRun, ...] = ()
    units: dict[str, LedgerUnit] = field(default_factory=dict)  # unit path -> ledger row
    judged: dict[int, bool] | None = None  # record id -> managed; None before schema 5
    places: dict[int, tuple[gfs_store.Place, ...]] = field(default_factory=dict)
    user_version: int | None = None  # PRAGMA user_version; None when there is no database
    # (gfs job, from stage, to stage), stages counted from 1 -> run_at of the newest ok move
    last_moves: dict[tuple[str, int, int], str] = field(default_factory=dict)


def _tables(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _stage_case(column: str, stages: Sequence[Sequence[Path]]) -> tuple[str, list]:
    """A SQL ``CASE`` giving the 1-based stage whose path holds ``column``, and its parameters."""
    whens, params = [], []
    for index, paths in enumerate(stages, 1):
        for path in paths:
            whens.append(f"WHEN substr({column}, 1, ?) = ? THEN {index}")
            params += [len(str(path)) + 1, f"{path}/"]
    return "CASE " + " ".join(whens) + " END", params


def _last_moves(db, job: str, stages: Sequence[Sequence[Path]]) -> dict[tuple[str, int, int], str]:
    """The newest ``run_at`` of an ok move of the gfs job, per (from stage, to stage), found with
    one aggregate query; a path is matched to a stage by containment, as the stage figures do.
    A step with no destination (the completion of an interrupted move) is left out."""
    source, source_params = _stage_case("source", stages)
    destination, destination_params = _stage_case("destination", stages)
    rows = db.execute(  # the f-string holds the CASE built above: fixed text and placeholders
        f"SELECT {source}, {destination}, MAX(run_at) FROM gfs_steps"  # noqa: S608
        " WHERE gfs_job = ? AND action = 'move' AND outcome = 'ok' AND destination IS NOT NULL"
        " GROUP BY 1, 2",
        (*source_params, *destination_params, job),
    )
    return {(job, row[0], row[1]): row[2] for row in rows if row[0] and row[1]}


def read_history(
    history: History, stages: Mapping[str, Sequence[Sequence[Path]]] | None = None
) -> HistoryData:
    """Read the history database; raises HistoryError when it cannot be read.

    ``stages`` maps each gfs job to the paths of each of its stages, to find its last moves.
    Nothing is written or migrated: an older database simply lacks some tables, and the data
    that depends on them is empty.
    """
    if not history.settings.database.exists():
        return HistoryData()
    gfs_runs: list[GfsRun] = []
    units: dict[str, LedgerUnit] = {}
    judged = None
    last_moves: dict[tuple[str, int, int], str] = {}
    with history._connect() as db:
        tables = _tables(db)
        user_version = db.execute("PRAGMA user_version").fetchone()[0]
        if "gfs_steps" in tables:
            for job, job_stages in (stages or {}).items():
                last_moves.update(_last_moves(db, job, job_stages))
        records = tuple(
            BackupRecord(**dict(row))
            for row in db.execute("SELECT * FROM backup_runs WHERE status='success' ORDER BY id")
        )
        running: dict[str, str] = {}
        for row in db.execute(
            "SELECT job_name, started_at FROM backup_runs WHERE status='running' ORDER BY id"
        ):
            running.setdefault(row["job_name"], row["started_at"])
        if "gfs_runs" in tables:
            for row in db.execute("SELECT * FROM gfs_runs ORDER BY id"):
                gfs_runs.append(GfsRun(
                    row["id"], row["gfs_job"], row["started_at"], row["finished_at"],
                    row["status"], row["exit_code"], {name: row[name] for name in GFS_COUNTS},
                ))
        if {"gfs_units", "gfs_locations"} <= tables:
            stages: dict[int, list[Path]] = {}
            for row in db.execute("SELECT unit_id, stage_path FROM gfs_locations ORDER BY id"):
                stages.setdefault(row["unit_id"], []).append(Path(row["stage_path"]))
            for row in db.execute("SELECT id, unit, deleted_at FROM gfs_units"):
                units[row["unit"]] = LedgerUnit(
                    row["unit"], row["deleted_at"] is not None, tuple(stages.get(row["id"], ()))
                )
        if "gfs_members" in tables:
            judged = {
                row[0]: bool(row[1])
                for row in db.execute("SELECT record_id, managed FROM gfs_members")
            }
    return HistoryData(
        records, running, history.last_runs(), tuple(gfs_runs), units, judged,
        gfs_store.places(history), user_version, last_moves,
    )
