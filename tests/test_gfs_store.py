"""History schemas 4 and 5: the GFS unit, location, step and member tables."""

import json
import sqlite3
from contextlib import closing

from bdbackup.backends import BackupResult
from bdbackup.history import History, HistorySettings, artifact_identity
from tests.test_ledger import SCHEMA_2

SCHEMA_3 = SCHEMA_2.replace("PRAGMA user_version = 2;", (
    "ALTER TABLE backup_runs ADD COLUMN unit TEXT;\n"
    "ALTER TABLE backup_runs ADD COLUMN checksum TEXT;\n"
    "PRAGMA user_version = 3;"
))


def test_schema_3_database_upgrades_to_4(tmp_path):
    path = tmp_path / "history.sqlite3"
    old = tmp_path / "old.tar"
    old.write_bytes(b"old")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA_3)
        with db:
            db.execute(
                "INSERT INTO backup_runs (job_name, backup_type, started_at, completed_at,"
                " status, path, identity, restore_info, unit, checksum) VALUES ('job', 'file',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', 'success', ?, ?,"
                " ?, ?, 'abc')",
                (str(old), artifact_identity(old), json.dumps({"kind": "file"}), str(old)),
            )
    history = History(HistorySettings(path, tmp_path / "restore"))

    row, = history.records()
    assert row.checksum == "abc"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3

    new = tmp_path / "new.tar"
    new.write_bytes(b"new")
    history.run("job", "file", lambda: BackupResult(new, success=True), {"kind": "file"})
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gfs_units", "gfs_locations", "gfs_steps"} <= tables
    _, old_row = history.records()
    assert (old_row.unit, old_row.checksum) == (str(old), "abc")


SCHEMA_4 = SCHEMA_3.replace("PRAGMA user_version = 3;", (
    "CREATE TABLE gfs_units (id INTEGER PRIMARY KEY AUTOINCREMENT, unit TEXT NOT NULL UNIQUE,"
    " series TEXT NOT NULL, relative TEXT NOT NULL, deleted_at TEXT);\n"
    "CREATE TABLE gfs_locations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " unit_id INTEGER NOT NULL REFERENCES gfs_units(id), stage_path TEXT NOT NULL,"
    " path TEXT NOT NULL UNIQUE);\n"
    "CREATE TABLE gfs_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,"
    " gfs_job TEXT NOT NULL, unit_id INTEGER REFERENCES gfs_units(id), action TEXT NOT NULL,"
    " source TEXT, destination TEXT, outcome TEXT NOT NULL, reason TEXT);\n"
    "PRAGMA user_version = 4;"
))


def test_schema_4_database_upgrades_to_5(tmp_path):
    path = tmp_path / "history.sqlite3"
    old = tmp_path / "old.tar"
    old.write_bytes(b"old")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA_4)
        with db:
            record_id = db.execute(
                "INSERT INTO backup_runs (job_name, backup_type, started_at, completed_at,"
                " status, path, identity, restore_info, unit, checksum) VALUES ('job', 'file',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', 'success', ?, ?,"
                " ?, ?, 'abc')",
                (str(old), artifact_identity(old), json.dumps({"kind": "file"}), str(old)),
            ).lastrowid
            unit_id = db.execute(
                "INSERT INTO gfs_units (unit, series, relative) VALUES (?, 'job', 'old.tar')",
                (str(old),),
            ).lastrowid
            db.execute(
                "INSERT INTO gfs_locations (unit_id, stage_path, path) VALUES (?, ?, ?)",
                (unit_id, str(tmp_path), str(old)),
            )
    history = History(HistorySettings(path, tmp_path / "restore"))

    history.records()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4

    new = tmp_path / "new.tar"
    new.write_bytes(b"new")
    history.run("job", "file", lambda: BackupResult(new, success=True), {"kind": "file"})
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = [r[1] for r in db.execute("PRAGMA table_info(gfs_locations)")]
        assert "identities" in columns
        location = db.execute(
            "SELECT unit_id, stage_path, path, identities FROM gfs_locations"
        ).fetchall()
        members = db.execute("SELECT record_id, unit_id, managed FROM gfs_members").fetchall()
    assert location == [(unit_id, str(tmp_path), str(old), None)]
    assert members == [(record_id, unit_id, 1)]
