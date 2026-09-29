"""GFS state in the history database: managed units, their locations and every step.

A unit's members are the successful ``backup_runs`` rows that share its ``unit`` path; their
checksums live only there. ``gfs_units`` names the units GFS manages, ``gfs_locations`` says
where each copy is and under which configured stage path it was written, and ``gfs_steps``
records every action, refusal, deferral and hold.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bdbackup.history import BackupRecord, History

_TABLES = {"gfs_units", "gfs_locations", "gfs_steps"}


@dataclass(frozen=True)
class Location:
    stage_path: Path
    path: Path


@dataclass(frozen=True)
class ManagedUnit:
    id: int | None
    unit: Path
    series: str
    relative: Path
    kind: str
    time: datetime
    members: tuple[BackupRecord, ...]
    locations: tuple[Location, ...]
    size: int
    # Records of this unit GFS has not judged yet, with whether each is available now (S2).
    seen: tuple[tuple[int, bool], ...] = ()


def _has_tables(db) -> bool:
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return _TABLES <= names


def _read(history: History):
    """Successful records, unit rows and locations; reading never migrates or writes."""
    if not history.settings.database.exists():
        return [], {}, defaultdict(list)
    with history._connect() as db:
        records = [
            BackupRecord(**dict(row))
            for row in db.execute("SELECT * FROM backup_runs WHERE status='success' ORDER BY id")
        ]
        units, locations = {}, defaultdict(list)
        if _has_tables(db):
            for row in db.execute("SELECT * FROM gfs_units"):
                units[row["unit"]] = row
            for row in db.execute("SELECT * FROM gfs_locations ORDER BY id"):
                locations[row["unit_id"]].append(
                    Location(Path(row["stage_path"]), Path(row["path"]))
                )
    return records, units, locations


def _local(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp).astimezone()


def _unit(unit_id, path, series, relative, members, locations) -> ManagedUnit:
    kind = json.loads(members[0].restore_info).get("kind", "")
    return ManagedUnit(
        id=unit_id, unit=path, series=series, relative=relative, kind=kind,
        time=min(_local(m.completed_at) for m in members),
        members=tuple(members), locations=tuple(locations),
        size=sum(m.size_bytes or 0 for m in members),
    )


def load(history: History, first_stage: Path) -> tuple[list[ManagedUnit], int]:
    """Return the units this chain manages and the count of unmanaged records.

    A record is seen only if it is available when GFS first sees it (spec 2 r7, S2).
    """
    records, rows, locations = _read(history)
    by_unit: dict[str, list[BackupRecord]] = defaultdict(list)
    unmanaged = 0
    for record in records:
        if record.unit and record.checksum:
            by_unit[record.unit].append(record)
        elif record.path and Path(record.path).is_relative_to(first_stage):
            unmanaged += 1
    units = []
    for unit_text, members in by_unit.items():
        path = Path(unit_text)
        row = rows.get(unit_text)
        if row is not None:
            if row["deleted_at"] is None and path.is_relative_to(first_stage):
                units.append(_unit(row["id"], path, row["series"], Path(row["relative"]),
                                   members, locations[row["id"]]))
            continue
        if not path.is_relative_to(first_stage):
            continue
        seen = [m for m in members if m.available]
        unmanaged += len(members) - len(seen)
        if seen:
            units.append(_unit(None, path, seen[0].job_name, path.relative_to(first_stage),
                               seen, [Location(first_stage, path)]))
    units.sort(key=lambda u: (u.time, str(u.unit)))
    return units, unmanaged


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_unit(history: History, unit: ManagedUnit) -> int:
    with history._connect(create=True) as db:
        unit_id = db.execute(
            "INSERT INTO gfs_units (unit, series, relative) VALUES (?, ?, ?)",
            (str(unit.unit), unit.series, unit.relative.as_posix()),
        ).lastrowid
        db.executemany(
            "INSERT INTO gfs_locations (unit_id, stage_path, path) VALUES (?, ?, ?)",
            [(unit_id, str(loc.stage_path), str(loc.path)) for loc in unit.locations],
        )
    return unit_id


def record_members(history: History, unit_id: int,
                   seen: Sequence[tuple[int, bool]]) -> None:
    """Store each record's first judgment; a stored judgment is never changed."""


def record_move(history: History, unit_id: int, new: Sequence[Location],
                removed: Sequence[Location]) -> None:
    with history._connect(create=True) as db:
        db.executemany(
            "INSERT INTO gfs_locations (unit_id, stage_path, path) VALUES (?, ?, ?)",
            [(unit_id, str(loc.stage_path), str(loc.path)) for loc in new],
        )
        db.executemany(
            "DELETE FROM gfs_locations WHERE unit_id=? AND path=?",
            [(unit_id, str(loc.path)) for loc in removed],
        )


def record_delete(history: History, unit_id: int) -> None:
    with history._connect(create=True) as db:
        db.execute("DELETE FROM gfs_locations WHERE unit_id=?", (unit_id,))
        db.execute("UPDATE gfs_units SET deleted_at=? WHERE id=?", (_now(), unit_id))


def record_step(history: History, gfs_job: str, unit_id: int | None, action: str,
                source: str | None, destination: str | None, outcome: str,
                reason: str | None) -> None:
    with history._connect(create=True) as db:
        db.execute(
            "INSERT INTO gfs_steps (run_at, gfs_job, unit_id, action, source, destination,"
            " outcome, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), gfs_job, unit_id, action, source, destination, outcome, reason),
        )


def listing_suffixes(history: History) -> dict[str, str]:
    """``unit path -> " | stage … | locations …"`` or ``" | deleted …"`` for the history list."""
    _, rows, locations = _read(history)
    suffixes = {}
    for unit_text, row in rows.items():
        if row["deleted_at"] is not None:
            suffixes[unit_text] = f" | deleted {row['deleted_at']}"
        elif locations[row["id"]]:
            places = locations[row["id"]]
            suffixes[unit_text] = (
                f" | stage {places[0].stage_path} | locations "
                + ", ".join(str(loc.path) for loc in places)
            )
    return suffixes
