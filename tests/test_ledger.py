"""Ledger foundation: every backup records its unit and a checksum taken at backup time."""

import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupResult
from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.history import History, HistorySettings, artifact_identity
from bdbackup.mysql import MySQLBackup


def invoke(*args):
    return CliRunner().invoke(main, list(map(str, args)))


@pytest.fixture
def ledger_config(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/hello").write_text("recover me")
    (tmp_path / "paths").write_text("hello\n")
    path = tmp_path / "config.toml"
    path.write_text(
        '[history]\ndatabase="state/history.sqlite3"\nrestore_root="recovery"\n'
        '[daily]\ntype="file"\ntemplate_filename="paths"\n'
        'chdir="source"\nbackup_dst="archives/daily"\nformat="tar.gz"\n'
        '[sql]\ntype="mysqldump"\ndatabase="app"\n'
        '[xb]\ntype="xtrabackup"\nbackup_root="physical/db"\nbinary="mariadb-backup"\n'
    )
    return path


def records(config):
    return History(Config(config).history).records()


def database(config):
    return Config(config).history.database


def expected_checksum(path: Path) -> str:
    """Spec 2 §2 Checksum, computed without bdbackup.ledger."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    lines = sorted(
        (p.relative_to(path).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in path.rglob("*")
        if p.is_file()
    )
    lines.sort(key=lambda item: item[0].encode())
    manifest = "".join(f"{digest}  {name}\n" for name, digest in lines)
    return hashlib.sha256(manifest.encode()).hexdigest()


def mysqldump_backup(root):
    def backup(self, database=None):
        path = root / "app-dump.sql.gz"
        with gzip.open(path, "wb") as output:
            output.write(b"SELECT 1;\n")
        return BackupResult(path, size_bytes=path.stat().st_size, success=True)

    return backup


@pytest.mark.parametrize(
    "engine", ["file", "mysqldump", "xtrabackup-full", "xtrabackup-incremental"]
)
def test_backup_records_unit_and_checksum(ledger_config, physical_runner, engine):
    root = ledger_config.parent
    with (
        patch("subprocess.run", side_effect=physical_runner),
        patch.object(MySQLBackup, "backup", mysqldump_backup(root)),
    ):
        if engine == "file":
            result = invoke("run", "--config", ledger_config, "daily")
        elif engine == "mysqldump":
            result = invoke("run", "--config", ledger_config, "sql")
        else:
            result = invoke("run", "--config", ledger_config, "xb")
            if engine == "xtrabackup-incremental":
                assert result.exit_code == 0, result.output
                result = invoke("run", "--config", ledger_config, "xb", "--incremental")
    assert result.exit_code == 0, result.output
    record = records(ledger_config)[0]
    assert record.checksum == expected_checksum(Path(record.path))
    if engine.startswith("xtrabackup"):
        date_dir, = (root / "physical/db").resolve().glob("????-??-??")
        assert record.unit == str(date_dir)
    else:
        assert record.unit == record.path


def test_member_with_symlink_fails_backup(ledger_config, physical_runner):
    def runner(cmd, **kwargs):
        result = physical_runner(cmd, **kwargs)
        if "--backup" in cmd:
            target = Path(next(a.split("=", 1)[1] for a in cmd if a.startswith("--target-dir=")))
            (target / "link").symlink_to("data.ibd")
        return result

    with patch("subprocess.run", side_effect=runner):
        result = invoke("run", "--config", ledger_config, "xb")
    assert result.exit_code == 1, result.output
    record, = records(ledger_config)
    assert record.status == "failed"
    assert record.error == "LedgerError"


def test_history_listing_shows_unit(ledger_config):
    assert invoke("run", "--config", ledger_config, "daily").exit_code == 0
    with closing(sqlite3.connect(database(ledger_config))) as db, db:
        db.execute(
            "INSERT INTO backup_runs (job_name, backup_type, started_at, completed_at, status,"
            " path, restore_info) VALUES ('old', 'file', '2020-01-01T00:00:00+00:00',"
            " '2020-01-01T00:00:01+00:00', 'success', '/gone.tar', '{}')"
        )
    result = invoke("history", "--config", ledger_config)
    assert result.exit_code == 0, result.output
    daily = next(r for r in records(ledger_config) if r.job_name == "daily")
    lines = {line.split(":", 1)[0]: line for line in result.output.splitlines()}
    assert lines[str(daily.id)].endswith(f" | unit {daily.unit}"), lines[str(daily.id)]
    old = next(r for r in records(ledger_config) if r.job_name == "old")
    assert lines[str(old.id)].endswith(" | unit -"), lines[str(old.id)]


# The history schema bdbackup 0.6.2 creates (user_version 2).
SCHEMA_2 = """
CREATE TABLE backup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    backup_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed')),
    path TEXT,
    size_bytes INTEGER,
    identity TEXT,
    restore_info TEXT NOT NULL,
    error TEXT,
    encrypted INTEGER NOT NULL DEFAULT 0 CHECK(encrypted IN (0, 1))
);
CREATE INDEX backup_runs_job ON backup_runs(job_name, id DESC);
PRAGMA user_version = 2;
"""


def test_schema_2_database_upgrades_and_stays_readable(tmp_path):
    path = tmp_path / "history.sqlite3"
    old_artifact = tmp_path / "old.tar"
    old_artifact.write_bytes(b"old")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA_2)
        with db:
            db.execute(
                "INSERT INTO backup_runs (job_name, backup_type, started_at, completed_at,"
                " status, path, identity, restore_info) VALUES ('job', 'file',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', 'success', ?, ?,"
                " ?)",
                (str(old_artifact), artifact_identity(old_artifact), json.dumps({"kind": "file"})),
            )
    history = History(HistorySettings(path, tmp_path / "restore"))

    old, = history.records()
    assert old.unit is None and old.checksum is None
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

    new_artifact = tmp_path / "new.tar"
    new_artifact.write_bytes(b"new")
    history.run("job", "file", lambda: BackupResult(new_artifact, success=True), {"kind": "file"})
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    new, old = history.records()
    assert old.unit is None and old.checksum is None
    assert new.unit == str(new_artifact.resolve())
    assert new.checksum == hashlib.sha256(b"new").hexdigest()
