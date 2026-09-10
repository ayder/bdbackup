"""History persistence, partial failures, and recovery through the actual CLI."""

import gzip
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupError, BackupResult
from bdbackup.cli import main
from bdbackup.config import Config, ConfigError
from bdbackup.history import History, HistoryError, HistorySettings
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.recovery import restore_record, suggested_destination


@pytest.fixture
def config_file(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/hello").write_text("recover me")
    (tmp_path / "paths").write_text("hello\n")
    path = tmp_path / "config.toml"
    path.write_text(
        '[history]\ndatabase="state/history.sqlite3"\nrestore_root="recovery"\n'
        '[daily]\ntype="file"\ntemplate_filename="paths"\n'
        'chdir="source"\nbackup_dst="archives/daily"\nformat="tar.gz"\n'
    )
    return path


def invoke(*args, input=None):
    return CliRunner().invoke(main, list(map(str, args)), input=input)


def history_for(config):
    return History(Config(config).history)


def test_config_history_is_not_a_job(config_file):
    config = Config(config_file)
    assert list(config.jobs) == ["daily"]
    assert config.history.database == config_file.parent / "state/history.sqlite3"
    assert config.history.restore_root == config_file.parent / "recovery"


@pytest.mark.parametrize("section", [
    '[history]\nrestore_root="restore"',
    '[history]\ndatabase=""',
    '[history]\ndatabase=12',
    '[history]\ndatabase=":memory:"',
    '[history]\ndatabase="db"\nunknown=true',
    'history="db"',
])
def test_invalid_history_config(tmp_path, section):
    path = tmp_path / "bad.toml"
    path.write_text(section)
    with pytest.raises(ConfigError, match="history"):
        Config(path)


def test_success_persists_job_type_utc_timestamps_and_artifact(config_file):
    result = invoke("run", "--config", config_file, "daily")
    assert result.exit_code == 0, result.output
    record, = history_for(config_file).records()
    assert record.job_name == "daily"
    assert record.backup_type == "file"
    assert record.status == "success"
    assert datetime.fromisoformat(record.started_at).utcoffset().total_seconds() == 0
    assert record.completed_at >= record.started_at
    assert record.available
    assert record.size_bytes == Path(record.path).stat().st_size
    assert json.loads(record.restore_info) == {"kind": "file"}
    assert Config(config_file).history.database.stat().st_mode & 0o777 == 0o600
    listing = invoke("history", "--config", config_file)
    assert listing.exit_code == 0, listing.output
    assert "daily | file" in listing.output
    assert "success | available" in listing.output


def test_failure_and_later_job_are_both_recorded(config_file):
    config_file.write_text(
        config_file.read_text().replace('[daily]', '[broken]\ntype="file"\n'
            'template_filename="missing"\nbackup_dst="bad"\n[daily]')
    )
    result = invoke("run", "--config", config_file, "--all")
    assert result.exit_code == 1
    good, bad = history_for(config_file).records()
    assert (good.job_name, good.status) == ("daily", "success")
    assert (bad.job_name, bad.status) == ("broken", "failed")
    assert bad.completed_at and bad.path is None and bad.error
    assert not bad.available
    assert history_for(config_file).records(successful=True) == [good]


def test_verify_failure_is_not_success(config_file):
    # The backend's mandatory first verification succeeds; the optional second fails.
    with patch("bdbackup.filebackup.FileBackup.verify", side_effect=[None, BackupError("bad")]):
        result = invoke("run", "--config", config_file, "daily")
    assert result.exit_code == 1
    assert history_for(config_file).records()[0].status == "failed"


@pytest.mark.parametrize("exception", [BackupError("password=secret"), KeyboardInterrupt()])
def test_failed_and_interrupted_attempts_do_not_store_exception_secrets(tmp_path, exception):
    history = History(HistorySettings(tmp_path / "history.db", tmp_path / "restore"))

    def fail():
        assert history.records()[0].status == "running"
        raise exception

    with pytest.raises(type(exception)):
        history.run("job", "file", fail)
    record = history.records()[0]
    assert record.status == "failed"
    assert record.error == type(exception).__name__
    assert "secret" not in history.settings.database.read_bytes().decode(errors="ignore")


def test_unwritable_history_stops_backup_before_work(config_file):
    settings = Config(config_file).history
    settings.database.parent.mkdir()
    settings.database.mkdir()
    result = invoke("run", "--config", config_file, "daily")
    assert result.exit_code == 1
    assert not (config_file.parent / "archives").exists()


def test_read_does_not_create_database(tmp_path):
    history = History(HistorySettings(tmp_path / "missing/history.db", tmp_path / "restore"))
    with pytest.raises(HistoryError, match="does not exist"):
        history.records()
    assert not history.settings.database.parent.exists()


def test_future_schema_is_rejected_without_modification(tmp_path):
    path = tmp_path / "db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=99")
    history = History(HistorySettings(path, tmp_path / "restore"))
    with pytest.raises(HistoryError, match="schema version"):
        history.run("job", "file", lambda: pytest.fail("must not execute"))
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_concurrent_writers_keep_every_attempt(tmp_path):
    history = History(HistorySettings(tmp_path / "state/history.db", tmp_path / "restore"))

    def backup(index):
        artifact = tmp_path / str(index)
        artifact.write_text(str(index))
        return history.run(str(index), "file", lambda: BackupResult(artifact, success=True))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(backup, range(24)))
    records = history.records()
    assert len(records) == 24
    assert len({r.id for r in records}) == 24
    assert all(r.status == "success" and r.available for r in records)


def test_completion_write_failure_preserves_artifact_and_never_claims_success(tmp_path):
    history = History(HistorySettings(tmp_path / "history.db", tmp_path / "restore"))
    artifact = tmp_path / "backup.tar"

    def backup():
        artifact.write_bytes(b"completed backup")
        with closing(sqlite3.connect(history.settings.database)) as db:
            db.execute("""CREATE TRIGGER fail_update BEFORE UPDATE ON backup_runs
                       BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
            db.commit()
        return BackupResult(artifact, success=True)

    with pytest.raises(HistoryError, match="simulated disk failure"):
        history.run("job", "file", backup)
    assert artifact.read_bytes() == b"completed backup"
    assert history.records()[0].status == "running"
    assert not history.records(successful=True)


def test_unsuccessful_backend_result_is_recorded_as_failure(tmp_path):
    history = History(HistorySettings(tmp_path / "history.db", tmp_path / "restore"))
    with pytest.raises(BackupError, match="unsuccessful"):
        history.run("job", "file", lambda: BackupResult(tmp_path / "unused"))
    assert history.records()[0].status == "failed"


def test_interactive_history_restore_suggests_path_and_recovers(config_file):
    assert invoke("run", "--config", config_file, "daily").exit_code == 0
    result = invoke("restore", "--config", config_file, input="\n\ny\n")
    assert result.exit_code == 0, result.output
    assert "Successful backups" in result.output
    assert "Restore destination" in result.output
    assert (config_file.parent / "recovery/daily-1/hello").read_text() == "recover me"


def test_restore_decline_has_no_filesystem_effects(config_file):
    invoke("run", "--config", config_file, "daily")
    result = invoke("restore", "--config", config_file, input="\n\nn\n")
    assert result.exit_code == 1
    assert not (config_file.parent / "recovery").exists()


def test_explicit_backup_id_and_destination_restore(config_file):
    invoke("run", "--config", config_file, "daily")
    dst = config_file.parent / "custom"
    result = invoke("restore", "--config", config_file, "--backup-id", "1", "--yes", "-d", dst)
    assert result.exit_code == 0, result.output
    assert (dst / "hello").read_text() == "recover me"
    repeat = invoke("restore", "--config", config_file, "--backup-id", "1", "--yes", "-d", dst)
    assert repeat.exit_code == 1
    assert (dst / "hello").read_text() == "recover me"


def test_restore_rejects_dangling_destination_link(config_file):
    invoke("run", "--config", config_file, "daily")
    dst = config_file.parent / "link"
    dst.symlink_to(config_file.parent / "absent", target_is_directory=True)
    result = invoke("restore", "--config", config_file, "--backup-id", "1", "--yes", "-d", dst)
    assert result.exit_code == 1
    assert not (config_file.parent / "absent").exists()


def test_reused_archive_path_makes_older_run_unavailable(config_file):
    invoke("run", "--config", config_file, "daily")
    (config_file.parent / "source/hello").write_text("new content")
    invoke("run", "--config", config_file, "daily")
    newest, older = history_for(config_file).records()
    assert newest.available
    assert not older.available
    result = invoke("restore", "--config", config_file, "--backup-id", older.id, "--yes")
    assert result.exit_code == 1
    assert not (config_file.parent / "recovery").exists()
    Path(newest.path).unlink()
    assert not newest.available
    assert invoke("restore", "--config", config_file).exit_code == 1


def test_history_filters_jobs_and_restore_requires_explicit_id_for_yes(config_file):
    invoke("run", "--config", config_file, "daily")
    assert history_for(config_file).records(job="other") == []
    assert invoke("restore", "--config", config_file, "--yes").exit_code == 2
    assert invoke("restore", "--config", config_file, "--backup-id", "1",
                  "--job", "other", "--yes").exit_code == 2
    assert invoke("restore", "--config", config_file, "--backup-id", "999", "--yes").exit_code == 1


def test_global_config_records_direct_file_command_and_skips_dry_run(config_file):
    root = config_file.parent
    args = ["--config", config_file, "file", "--template", root / "paths",
            "-c", root / "source", "-d", root / "direct"]
    assert invoke(*args, "--dry-run").exit_code == 0
    assert not Config(config_file).history.database.exists()
    assert invoke(*args).exit_code == 0
    record, = history_for(config_file).records()
    assert record.job_name == "direct"
    assert record.available
    assert invoke("--config", config_file, "run", "daily").exit_code == 0


def test_live_history_database_is_excluded_from_file_backup(config_file):
    config_file.write_text(config_file.read_text().replace("state/history.sqlite3", "source/db"))
    (config_file.parent / "paths").write_text(".\n")
    assert invoke("run", "--config", config_file, "daily").exit_code == 0
    result = invoke("restore", "--config", config_file, "--backup-id", "1", "--yes")
    assert result.exit_code == 0, result.output
    restored = config_file.parent / "recovery/daily-1"
    assert (restored / "hello").exists()
    assert not (restored / "db").exists()


def test_archive_cannot_replace_history_database(config_file):
    config_file.write_text(
        config_file.read_text().replace("state/history.sqlite3", "archives/daily.tar.gz")
    )
    result = invoke("run", "--config", config_file, "daily")
    assert result.exit_code == 1
    assert history_for(config_file).records()[0].status == "failed"


def test_configured_mysql_records_only_safe_restore_metadata(config_file):
    config_file.write_text(config_file.read_text() + '\n[sql]\ntype="mysqldump"\n'
                           'database="app"\npassword="never-store-this"\n')
    artifact = config_file.parent / "app.sql.gz"
    with gzip.open(artifact, "wb") as output:
        output.write(b"SELECT 1;")
    with patch.object(MySQLBackup, "backup", return_value=BackupResult(artifact, success=True)):
        assert invoke("run", "--config", config_file, "sql").exit_code == 0
    record, = history_for(config_file).records()
    assert json.loads(record.restore_info) == {"kind": "mysqldump"}
    raw = Config(config_file).history.database.read_bytes()
    assert b"never-store-this" not in raw


def test_parallel_mysql_partial_failure_and_sql_recovery(config_file):
    root = config_file.parent

    def backup(database):
        if database == "bad":
            raise BackupError("failed")
        path = root / f"{database}.sql.gz"
        with gzip.open(path, "wb") as output:
            output.write(b"CREATE DATABASE example;\n")
        return BackupResult(path, size_bytes=path.stat().st_size, success=True)

    with patch.object(MySQLBackup, "backup", side_effect=backup):
        result = invoke("--config", config_file, "mysqldump", "--database", "good,bad", "-j", "2")
    assert result.exit_code == 1
    records = {r.job_name: r for r in history_for(config_file).records()}
    assert records["bad"].status == "failed"
    assert records["good"].available
    result = invoke("restore", "--config", config_file, "--backup-id", records["good"].id, "--yes")
    assert result.exit_code == 0, result.output
    dst = root / f"recovery/good-{records['good'].id}/backup.sql"
    assert dst.read_text() == "CREATE DATABASE example;\n"
    assert dst.stat().st_mode & 0o777 == 0o600


def test_corrupt_sql_restore_cleans_its_new_directory(tmp_path):
    path = tmp_path / "broken.sql.gz"
    path.write_bytes(b"not gzip")
    history = History(HistorySettings(tmp_path / "history.db", tmp_path / "restore"))
    history.run("sql", "mysqldump", lambda: BackupResult(path, success=True), {"kind": "mysqldump"})
    with pytest.raises(OSError):
        restore_record(history.records()[0], tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


def test_physical_full_incremental_history_and_prepare_dispatch(config_file, physical_runner):
    root = config_file.parent / "physical"
    for kind in ("full", "incremental"):
        with patch("subprocess.run", side_effect=physical_runner):
            result = invoke("--config", config_file, "xtrabackup", kind, "--database", "db",
                            "--root", root, "--binary", "mariadb-backup")
        assert result.exit_code == 0, result.output
    incremental, full = history_for(config_file).records()
    assert full.backup_type == "xtrabackup-full"
    assert incremental.backup_type == "xtrabackup-incremental"
    assert json.loads(incremental.restore_info) == {
        "kind": "xtrabackup", "backup_root": str(root / "db"), "binary": "mariadb-backup",
    }
    assert incremental.available and full.available
    dst = config_file.parent / "prepared"
    with patch.object(XtraBackup, "prepare", return_value=dst) as prepare:
        result = invoke("restore", "--config", config_file, "--backup-id", incremental.id,
                        "--yes", "-d", dst)
    assert result.exit_code == 0, result.output
    prepare.assert_called_once_with(Path(incremental.path), dst)


def test_suggestion_avoids_existing_paths_and_job_path_traversal(tmp_path):
    artifact = tmp_path / "archive"
    artifact.touch()
    history = History(HistorySettings(tmp_path / "history.db", tmp_path / "restore"))
    history.run("../../unsafe/job", "file", lambda: BackupResult(artifact, success=True))
    record = history.records()[0]
    first = suggested_destination(record, tmp_path / "restore")
    assert first.parent == tmp_path / "restore"
    first.mkdir(parents=True)
    second = suggested_destination(record, tmp_path / "restore")
    assert second != first and second.parent == first.parent
