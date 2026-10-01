"""GFS state in the history database: managed units, their locations and every step.

A unit's members are the successful ``backup_runs`` rows that share its ``unit`` path; their
checksums live only there. ``gfs_units`` names the units GFS manages, ``gfs_locations`` says
where each copy is and under which configured stage path it was written, and ``gfs_steps``
records every action, refusal, deferral and hold. ``gfs_members`` keeps each record's S2
judgment: whether it was available when GFS first saw it (spec 2 r7 §4.1).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bdbackup.history import BackupRecord, History, artifact_identity

_TABLES = {"gfs_units", "gfs_locations", "gfs_steps"}


@dataclass(frozen=True)
class Location:
    stage_path: Path
    path: Path


@dataclass(frozen=True)
class Place:
    """Where a record's copy is at one location of its unit."""

    unit: Path
    member: Path
    identity: str | None


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


def _has_tables(db) -> bool:
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return _TABLES <= names


def _has_members(db) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gfs_members'"
    ).fetchone() is not None


def _read(history: History):
    """Successful records, unit rows, locations and S2 judgments.

    Reading never migrates or writes. ``members`` is None on a schema-4 database, where every
    record of a recorded unit is a member, as the schema 5 backfill makes it.
    """
    if not history.settings.database.exists():
        return [], {}, defaultdict(list), {}
    with history._connect() as db:
        records = [
            BackupRecord(**dict(row))
            for row in db.execute("SELECT * FROM backup_runs WHERE status='success' ORDER BY id")
        ]
        units, locations, members = {}, defaultdict(list), {}
        if _has_tables(db):
            for row in db.execute("SELECT * FROM gfs_units"):
                units[row["unit"]] = row
            for row in db.execute("SELECT * FROM gfs_locations ORDER BY id"):
                locations[row["unit_id"]].append(
                    Location(Path(row["stage_path"]), Path(row["path"]))
                )
            if _has_members(db):
                members = {row[0]: bool(row[1])
                           for row in db.execute("SELECT record_id, managed FROM gfs_members")}
            else:
                members = None
    return records, units, locations, members


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


def load(history: History, first_stage: Path,
         stage_paths: Collection[Path] = (),
         ) -> tuple[list[ManagedUnit], int, tuple[tuple[int, bool], ...]]:
    """Return the units this chain manages, the count of unmanaged records, and judgments.

    A record is managed only if it was available when GFS first saw it (spec 2 r7, S2). The
    judgment is made once per record and kept in ``gfs_members``; records not judged yet are
    judged now, including those of units left with no member, and returned as
    ``(record id, available)`` pairs for the run to store.
    """
    records, rows, locations, judged = _read(history)
    by_unit: dict[str, list[BackupRecord]] = defaultdict(list)
    unmanaged = 0
    for record in records:
        if record.unit and record.checksum:
            by_unit[record.unit].append(record)
        elif record.path and Path(record.path).is_relative_to(first_stage):
            unmanaged += 1
    units, judgments = [], []
    for unit_text, records_of_unit in by_unit.items():
        path = Path(unit_text)
        row = rows.get(unit_text)
        if row is not None and row["deleted_at"] is not None:
            continue
        # A recorded unit is taken by any location in the job's stages, so changing the first
        # stage's path keeps managing copies already moved (spec 2 §4.2).
        if not path.is_relative_to(first_stage) and (
            row is None
            or not any(loc.stage_path in stage_paths for loc in locations[row["id"]])
        ):
            continue
        if row is not None and judged is None:
            members = records_of_unit
        else:
            known = judged or {}
            seen = [(m.id, m.available) for m in records_of_unit if m.id not in known]
            judgments.extend(seen)
            fresh = dict(seen)
            members = [m for m in records_of_unit
                       if known.get(m.id, False) or fresh.get(m.id, False)]
            unmanaged += len(records_of_unit) - len(members)
        if not members:
            continue
        if row is not None:
            units.append(_unit(row["id"], path, row["series"], Path(row["relative"]),
                               members, locations[row["id"]]))
        else:
            units.append(_unit(None, path, members[0].job_name, path.relative_to(first_stage),
                               members, [Location(first_stage, path)]))
    units.sort(key=lambda u: (u.time, str(u.unit)))
    return units, unmanaged, tuple(judgments)


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


def record_members(history: History, seen: Sequence[tuple[int, bool]]) -> None:
    """Store each record's first judgment; a stored judgment is never changed."""
    with history._connect(create=True) as db:
        db.executemany(
            "INSERT OR IGNORE INTO gfs_members (record_id, managed) VALUES (?, ?)",
            [(record_id, int(available)) for record_id, available in seen],
        )


def record_move(history: History, unit_id: int, new: Sequence[Location],
                removed: Sequence[Location],
                identities: Mapping[Path, Mapping[int, str]] | None = None) -> None:
    with history._connect(create=True) as db:
        db.executemany(
            "INSERT INTO gfs_locations (unit_id, stage_path, path, identities)"
            " VALUES (?, ?, ?, ?)",
            [(unit_id, str(loc.stage_path), str(loc.path), _identities_text(identities, loc))
             for loc in new],
        )
        db.executemany(
            "DELETE FROM gfs_locations WHERE unit_id=? AND path=?",
            [(unit_id, str(loc.path)) for loc in removed],
        )


def _identities_text(identities, location: Location) -> str | None:
    if not identities or location.path not in identities:
        return None
    return json.dumps({str(record_id): identity
                       for record_id, identity in identities[location.path].items()})


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
    _, rows, locations, _ = _read(history)
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


def places(history: History) -> dict[int, tuple[Place, ...]]:
    """``backup_runs.id -> places`` for the members of recorded units, in location order.

    A deleted unit's members map to ``()``. A record GFS does not manage is absent, and its own
    ``available`` still applies. Reading never migrates or writes.
    """
    if not history.settings.database.exists():
        return {}
    with history._connect() as db:
        if not _has_tables(db):
            return {}
        columns = {row[1] for row in db.execute("PRAGMA table_info(gfs_locations)")}
        query = (
            "SELECT unit_id, path, identities FROM gfs_locations ORDER BY id"
            if "identities" in columns  # schema 4, read without migrating, has none
            else "SELECT unit_id, path, NULL FROM gfs_locations ORDER BY id"
        )
        locations: dict[int, list[tuple[Path, dict]]] = defaultdict(list)
        for unit_id, path, identities in db.execute(query):
            locations[unit_id].append((Path(path), json.loads(identities or "{}")))
        units = {row["unit"]: row for row in db.execute("SELECT * FROM gfs_units")}
        judged = (
            {row[0]: bool(row[1])
             for row in db.execute("SELECT record_id, managed FROM gfs_members")}
            if _has_members(db) else None
        )
        records = [
            BackupRecord(**dict(row))
            for row in db.execute(
                "SELECT * FROM backup_runs WHERE status='success' AND unit IS NOT NULL"
                " ORDER BY id"
            )
        ]
    found: dict[int, list[Place]] = {}
    for record in records:
        row = units.get(record.unit)
        if row is None or (judged is not None and not judged.get(record.id, False)):
            continue
        unit = Path(record.unit)
        relative = Path(record.path).relative_to(unit)
        found[record.id] = [] if row["deleted_at"] is not None else [
            Place(path, path / relative if relative != Path(".") else path,
                  record.identity if path == unit else identities.get(str(record.id)))
            for path, identities in locations[row["id"]]
        ]
    return {record_id: tuple(found_places) for record_id, found_places in found.items()}


def place_available(place: Place) -> bool:
    """Removal and replacement are detected as ``artifact_identity`` does, without hashing."""
    try:
        if place.identity is None:
            return place.member.exists()
        return artifact_identity(place.member) == place.identity
    except OSError:
        return False


def available(record: BackupRecord, known: Mapping[int, tuple[Place, ...]]) -> bool:
    if record.id in known:
        return any(place_available(place) for place in known[record.id])
    return record.available
