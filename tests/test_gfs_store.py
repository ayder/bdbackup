"""History schema 4: the GFS unit, location and step tables."""

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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gfs_units", "gfs_locations", "gfs_steps"} <= tables
    _, old_row = history.records()
    assert (old_row.unit, old_row.checksum) == (str(old), "abc")
