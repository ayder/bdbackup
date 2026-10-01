"""GFS runs against temporary stage directories and a real SQLite history database."""

import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import tarfile
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupResult
from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.gfs import actions, runner
from bdbackup.history import History
from bdbackup.ledger import checksum
from bdbackup.mysql import XtraBackup
from bdbackup.recovery import backup_restore_info
from bdbackup.utils import process_lock
from tests.conftest import write_checkpoints

DEFAULT_STAGES = [
    (["BACKUP"], "daily", "5d"),
    (["NFS/daily"], "daily", "20d"),
    (["NFS/weekly"], "weekly", "8w"),
]


def invoke(*args):
    return CliRunner().invoke(main, list(map(str, args)))


def tree(path: Path, exclude=()):
    """Sorted (relative path, content hash or symlink target) of everything under path."""
    items = []
    for entry in sorted(path.rglob("*")):
        relative = entry.relative_to(path).as_posix()
        if any(relative == e or relative.startswith(e + "/") for e in exclude):
            continue
        if entry.is_symlink():
            items.append((relative, "->" + os.readlink(entry)))
        elif entry.is_file():
            items.append((relative, hashlib.sha256(entry.read_bytes()).hexdigest()))
        else:
            items.append((relative, "dir"))
    return items


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.config_path = root / "config.toml"
        for name in ("BACKUP", "NFS/daily", "NFS/daily2", "NFS/weekly"):
            (root / name).mkdir(parents=True, exist_ok=True)
            if name != "BACKUP":
                (root / name / ".bdbackup-destination").touch()
        self.write(DEFAULT_STAGES)
        self.history = History(Config(self.config_path).history)

    def write(self, stages, apply=True):
        text = ('[history]\ndatabase="state/history.sqlite3"\n'
                f'[gfs-main]\ntype="gfs"\napply={"true" if apply else "false"}\n')
        for paths, period, keep in stages:
            quoted = ", ".join(f'"{p}"' for p in paths)
            text += (f"[[gfs-main.stage]]\npaths = [{quoted}]\nperiod = \"{period}\"\n"
                     f"keep = \"{keep}\"\n")
        self.config_path.write_text(text)

    @property
    def database(self):
        return Config(self.config_path).history.database

    def abs(self, relative):
        return (self.root / relative).resolve()

    def _record(self, path, info, series, day, backup_type):
        self.history.run(
            series, backup_type,
            lambda: BackupResult(path, size_bytes=_size(path), success=True), info,
        )
        completed = datetime.combine(day, time(12)).astimezone().astimezone(UTC).isoformat()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE backup_runs SET completed_at=? WHERE id=(SELECT max(id) FROM "
                       "backup_runs)", (completed,))

    def file_unit(self, name, *, days_ago=None, day=None, series="files"):
        day = day or (date.today() - timedelta(days=days_ago))
        path = self.root / "BACKUP/files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{name} {day}".encode())
        self._record(path, {"kind": "file"}, series, day, "file")
        return path

    def xtrabackup_unit(self, days_ago, incrementals=2, series="xb"):
        day = date.today() - timedelta(days=days_ago)
        root = self.root / "BACKUP/mysql/prod"
        info = {"kind": "xtrabackup", "backup_root": str(root.resolve())}
        unit = root / day.isoformat()
        full = unit / f"Full_{day:%Y%m%d}"
        write_checkpoints(full, 0, 100)
        (full / "data.ibd").write_bytes(b"pages " + day.isoformat().encode())
        (unit / "Full_Latest").symlink_to(full.name, target_is_directory=True)
        (unit / ".full_success").touch()
        self._record(full, info, series, day, "xtrabackup-full")
        for index in range(1, incrementals + 1):
            incremental = unit / "Incremental" / full.name / f"inc{index}"
            write_checkpoints(incremental, index * 100, index * 100 + 100)
            (incremental / "data.delta").write_bytes(b"delta %d" % index)
            self._record(incremental, info, series, day, "xtrabackup-incremental")
        return unit

    def archive_unit(self, name, days_ago):
        """A real gzip tar holding ``hello``, recorded as a file backup."""
        path = self.root / "BACKUP/files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = f"recover {name}".encode()
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("hello")
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text))
        self._record(path, {"kind": "file"}, "files", date.today() - timedelta(days=days_ago),
                     "file")
        return path

    def backdate(self, record_id, day):
        completed = datetime.combine(day, time(12)).astimezone().astimezone(UTC).isoformat()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE backup_runs SET completed_at=? WHERE id=?", (completed, record_id))

    def engine_chain(self, days_ago):
        """A real engine's full and two incrementals, plus a newer full in the next day's
        directory; returns the chain's three records. Run under ``prepare_runner``."""
        xb = XtraBackup((self.root / "BACKUP/mysql/prod").resolve(), retention_days=0)
        info = backup_restore_info(xb)
        day = date.today() - timedelta(days=days_ago)
        for kind, action in (("full", xb.full_backup), ("incremental", xb.incremental_backup),
                             ("incremental", xb.incremental_backup)):
            self.history.run("xb", f"xtrabackup-{kind}", action, info)
            self.backdate(self.history.records()[0].id, day)
        chain = self.history.records()[:3][::-1]
        tomorrow = xb._utc_now() + timedelta(days=1)
        with patch.object(xb, "_utc_now", return_value=tomorrow):
            self.history.run("xb", "xtrabackup-full", xb.full_backup, info)
        return chain

    def run_cli(self):
        return invoke("run", "--config", self.config_path, "gfs-main")

    def run_api(self, now):
        config = Config(self.config_path)
        lines = []
        code = runner.run_job(config, config.get("gfs-main"), now=now, out=lines.append)
        return code, "\n".join(lines)

    def query(self, sql, *args):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(sql, args).fetchall()


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def prepare_runner(physical_runner):
    """``physical_runner`` plus a ``--prepare`` branch (E-R1); collects every command."""
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        if "--prepare" not in cmd:
            return physical_runner(cmd, **kwargs)
        target = Path(next(a.split("=", 1)[1] for a in cmd if a.startswith("--target-dir=")))
        incremental = next(
            (Path(a.split("=", 1)[1]) for a in cmd if a.startswith("--incremental-dir=")), None,
        )
        end = XtraBackup._checkpoints(incremental or target)["to_lsn"]
        write_checkpoints(target, end=end, kind="full-prepared")
        return subprocess.CompletedProcess(cmd, 0)

    run.commands = commands
    return run


def stage_files(env, name):
    """Files GFS placed as units: the marker and ledger snapshots are not units."""
    return [p for p in (env.root / name).rglob("*")
            if p.is_file() and p.name != ".bdbackup-destination"
            and not p.name.endswith(".ledger.sqlite3")]


def test_file_unit_moves_to_mirrored_path(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    original = a.read_bytes()
    b = env.file_unit("b.tar.gz", days_ago=0)
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    moved = env.root / "NFS/daily/files/a.tar.gz"
    assert moved.read_bytes() == original
    assert not a.exists()
    assert b.exists()
    unit_id, = env.query("SELECT id FROM gfs_units WHERE unit=?", str(a.resolve()))[0]
    assert env.query("SELECT stage_path, path FROM gfs_locations WHERE unit_id=?", unit_id) == [
        (str(env.abs("NFS/daily")), str(moved.resolve()))
    ]
    assert env.query("SELECT action, outcome FROM gfs_steps WHERE unit_id=?", unit_id) == [
        ("move", "ok")
    ]


def test_xtrabackup_unit_moves_whole(env):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    before = tree(unit)
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    moved = env.root / "NFS/daily/mysql/prod" / unit.name
    assert tree(moved) == before
    assert (moved / "Full_Latest").is_symlink()
    assert not unit.exists()
    for member_path, digest in env.query(
        "SELECT path, checksum FROM backup_runs WHERE unit=?", str(unit.resolve())
    ):
        relative = Path(member_path).relative_to(unit.resolve())
        assert checksum(moved / relative) == digest


def test_newest_unit_stays_in_first_stage(env):
    unit = env.xtrabackup_unit(30)
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert unit.is_dir()
    assert not stage_files(env, "NFS")


def test_period_stage_keeps_newest_of_week(env):
    week = [date(2026, 8, 24) + timedelta(n) for n in range(7)]
    paths = [env.file_unit(f"{d}.tar.gz", day=d) for d in week]
    env.file_unit("2026-09-29.tar.gz", day=date(2026, 9, 29))
    code, output = env.run_api(datetime(2026, 9, 29, 12))
    assert code == 0, output
    assert [p.name for p in stage_files(env, "NFS/weekly")] == ["2026-08-30.tar.gz"]
    assert not any(p.exists() for p in paths)
    deleted = env.query("SELECT count(*) FROM gfs_units WHERE deleted_at IS NOT NULL")[0][0]
    assert deleted == 6
    assert output.count("not newest of 2026-W35") == 6


def test_incomplete_bucket_is_held(env):
    env.write([(["BACKUP"], "daily", "2d"), (["NFS/weekly"], "weekly", "8w")])
    held = None
    for day in [date(2026, 9, 26) + timedelta(n) for n in range(5)]:
        path = env.file_unit(f"{day}.tar.gz", day=day)
        held = path if day == date(2026, 9, 28) else held
    code, output = env.run_api(datetime(2026, 9, 30, 12))
    assert held.exists()
    assert "held: 2026-W40 not complete" in output, output


def test_move_refuses_modified_mirror(env):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/daily", "NFS/daily2"], "daily", "20d"),
               (["NFS/weekly"], "weekly", "8w")])
    env.file_unit("a.tar.gz", day=date(2026, 8, 30))  # a Sunday, alone in ISO week 35
    env.file_unit("z.tar.gz", day=date(2026, 9, 29))
    assert env.run_api(datetime(2026, 9, 5, 12))[0] == 0
    mirror = env.root / "NFS/daily2/files/a.tar.gz"
    mirror.write_bytes(b"tampered")
    code, output = env.run_api(datetime(2026, 9, 29, 12))  # a's target is weekly
    assert code == 1, output
    assert "refused: checksum mismatch" in output
    assert mirror.read_bytes() == b"tampered"
    assert (env.root / "NFS/daily/files/a.tar.gz").exists()
    assert not (env.root / "NFS/weekly/files").exists()


def test_failed_path_leaves_no_copy(env):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/daily", "NFS/daily2"], "daily", "20d"),
               (["NFS/weekly"], "weekly", "8w")])
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    (env.root / "NFS/daily2").chmod(0o500)
    try:
        first = env.run_cli()
    finally:
        (env.root / "NFS/daily2").chmod(0o700)
    assert first.exit_code == 1, first.output
    assert not stage_files(env, "NFS/daily") and not stage_files(env, "NFS/daily2")
    assert a.exists()
    assert env.query("SELECT count(*) FROM gfs_locations WHERE stage_path != ?",
                     str(env.abs("BACKUP")))[0][0] == 0
    assert env.query("SELECT count(*) FROM gfs_steps WHERE outcome='failed'")[0][0] == 2
    second = env.run_cli()
    assert second.exit_code == 0, second.output
    assert (env.root / "NFS/daily/files/a.tar.gz").exists()
    assert (env.root / "NFS/daily2/files/a.tar.gz").exists()


def test_checksum_mismatch_refused_until_accepted(env):
    # AC9 is about a managed unit: GFS records it first, then the file changes on disk.
    a = env.file_unit("a.tar.gz", day=date(2026, 9, 25))
    env.file_unit("b.tar.gz", day=date(2026, 9, 29))
    code, output = env.run_api(datetime(2026, 9, 29, 12))
    assert code == 0 and a.exists(), output
    a.write_bytes(b"changed on disk")
    code, output = env.run_api(datetime(2026, 10, 2, 12))
    assert code == 1, output
    assert "refused: checksum mismatch" in output
    assert a.read_bytes() == b"changed on disk"
    assert not stage_files(env, "NFS")
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute("UPDATE backup_runs SET checksum=? WHERE path=?",
                   (hashlib.sha256(b"changed on disk").hexdigest(), str(a.resolve())))
    code, output = env.run_api(datetime(2026, 10, 2, 12))
    assert code == 0, output
    assert (env.root / "NFS/daily/files/a.tar.gz").read_bytes() == b"changed on disk"


TWO_DAILY_PATHS = [(["BACKUP"], "daily", "5d"), (["NFS/daily", "NFS/daily2"], "daily", "20d"),
                   (["NFS/weekly"], "weekly", "8w")]


class _FailingCopy(actions.LocalTransport):
    def copy(self, source, temp, on_file):
        raise OSError("injected copy failure")


def test_steps_record_paths_and_failure_reason(env):
    env.write(TWO_DAILY_PATHS)
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    config = Config(env.config_path)
    lines = []
    code = runner.run_job(config, config.get("gfs-main"), transport=_FailingCopy(),
                          out=lines.append)
    assert code == 1, lines
    finals = [str(env.abs(f"{stage}/files/a.tar.gz")) for stage in ("NFS/daily", "NFS/daily2")]
    assert env.query("SELECT action, source, destination, reason FROM gfs_steps"
                     " WHERE outcome='failed' ORDER BY id") == [
        ("move", str(a.resolve()), final, "injected copy failure") for final in finals
    ]
    code, output = env.run_api(datetime.now())
    assert code == 0, output
    assert env.query("SELECT action, source, destination FROM gfs_steps"
                     " WHERE outcome='ok' ORDER BY id") == [
        ("move", str(a.resolve()), final) for final in finals
    ]


def test_delete_step_names_every_location(env):
    env.write(TWO_DAILY_PATHS)
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_api(datetime.now())[0] == 0
    code, output = env.run_api(datetime.now() + timedelta(days=60))  # a is expired
    assert code == 0, output
    assert env.query("SELECT source, destination FROM gfs_steps WHERE action='delete'"
                     " AND outcome='ok' ORDER BY id") == [
        (str(env.abs(f"{stage}/files/a.tar.gz")), None) for stage in ("NFS/daily", "NFS/daily2")
    ]


def test_unexpected_entry_refused(env):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    full_name = next(p.name for p in unit.iterdir() if p.name.startswith("Full_2"))
    (unit / "Incremental" / full_name / "leftover.tmp").mkdir()
    before = tree(unit)
    result = env.run_cli()
    assert result.exit_code == 1, result.output
    assert "refused: unexpected entry" in result.output
    assert tree(unit) == before


def test_symlink_inside_member_refused(env):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    full_name = next(p.name for p in unit.iterdir() if p.name.startswith("Full_2"))
    (unit / full_name / "evil").symlink_to("/etc/passwd")
    before = tree(unit)
    result = env.run_cli()
    assert result.exit_code == 1, result.output
    assert f"refused: unexpected entry {full_name}/evil" in result.output
    assert tree(unit) == before
    assert not (env.root / "NFS/daily/mysql").exists()


def _directory_modes(root: Path) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): p.stat().st_mode & 0o7777
            for p in [root, *root.rglob("*")] if p.is_dir() and not p.is_symlink()}


def test_move_keeps_directory_modes(env):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    full_name = next(p.name for p in unit.iterdir() if p.name.startswith("Full_2"))
    unit.chmod(0o750)
    (unit / full_name).chmod(0o700)
    (unit / "Incremental").chmod(0o710)
    before = _directory_modes(unit)
    umask = os.umask(0o022)
    try:
        result = env.run_cli()
    finally:
        os.umask(umask)
    assert result.exit_code == 0, result.output
    moved = env.root / "NFS/daily" / unit.relative_to(env.root / "BACKUP")
    assert _directory_modes(moved) == before


def test_unrecorded_files_untouched(env):
    env.file_unit("b.tar.gz", days_ago=0)
    old = env.root / "BACKUP/files/old.tar.gz"
    old.write_bytes(b"unrecorded")
    stamp = (datetime.now() - timedelta(days=60)).timestamp()
    os.utime(old, (stamp, stamp))
    other = env.root / "NFS/daily/other"
    other.mkdir()
    fixed = env.root / "BACKUP/files/fixed.tar"
    fixed.write_bytes(b"first run")
    env._record(fixed, {"kind": "file"}, "fixed", date.today() - timedelta(days=1), "file")
    fixed.write_bytes(b"second run, a replacement")
    env._record(fixed, {"kind": "file"}, "fixed", date.today(), "file")
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert old.exists() and other.is_dir() and fixed.exists()
    assert "unmanaged 1" in result.output, result.output


def test_missing_marker_blocks_writes(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    (env.root / "NFS/daily/.bdbackup-destination").unlink()
    result = env.run_cli()
    assert result.exit_code == 1, result.output
    assert "refused: destination not ready" in result.output, result.output
    assert list((env.root / "NFS/daily").iterdir()) == []
    assert a.exists()


def test_dry_run_changes_nothing(env):
    env.write(DEFAULT_STAGES, apply=False)
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("c.tar.gz", days_ago=70)
    env.file_unit("b.tar.gz", days_ago=0)
    database = env.database
    before_tree = tree(env.root, exclude=("state",))
    before_db = database.read_bytes()
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert tree(env.root, exclude=("state",)) == before_tree
    assert database.read_bytes() == before_db
    assert "would move" in result.output and "would delete" in result.output, result.output


def test_removed_stage_is_refused(env):
    for day in [date(2026, 8, 24) + timedelta(n) for n in range(7)] + [date(2026, 9, 29)]:
        env.file_unit(f"{day}.tar.gz", day=day)
    code, output = env.run_api(datetime(2026, 9, 29, 12))
    assert code == 0, output
    weekly = env.root / "NFS/weekly/files/2026-08-30.tar.gz"
    assert weekly.exists()
    env.write(DEFAULT_STAGES[:2])
    code, output = env.run_api(datetime(2026, 9, 29, 12))
    assert code == 1, output
    assert "refused: stage not configured" in output
    assert weekly.exists()


def test_changed_first_stage_keeps_managing_cold_copies(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    cold = env.root / "NFS/daily/files/a.tar.gz"
    (env.root / "HOT").mkdir()
    env.write([(["HOT"], "daily", "5d"), *DEFAULT_STAGES[1:]])
    later = datetime.now() + timedelta(days=60)
    code, output = env.run_api(later)
    assert code == 0, output
    assert cold.exists()
    assert f"Stage {env.abs('NFS/daily')}: 1 unit" in output, output
    new = env.root / "HOT/files/n.tar.gz"
    new.parent.mkdir()
    new.write_bytes(b"new backup")
    env._record(new, {"kind": "file"}, "files", later.date(), "file")
    code, output = env.run_api(later)
    assert code == 0, output
    assert "delete files files/a.tar.gz" in output, output
    assert not cold.exists()
    assert env.query("SELECT deleted_at IS NOT NULL FROM gfs_units WHERE unit=?",
                     str(a.resolve())) == [(1,)]
    assert new.exists()


def test_copy_holds_no_engine_lock_and_commit_defers(env):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    lock = env.abs("BACKUP/mysql/prod") / ".bdbackup.lock"
    with process_lock(lock):
        deferred = env.run_cli()
    assert deferred.exit_code == 3, deferred.output
    assert "deferred: locked" in deferred.output
    assert not [p for p in (env.root / "NFS/daily").rglob("*") if ".gfs-tmp-" in p.name]
    assert unit.is_dir()

    acquired = []
    original = actions.LocalTransport.copy

    def copy(self, source, temp, on_file):
        with process_lock(lock):
            acquired.append(True)
        return original(self, source, temp, on_file)

    with patch.object(actions.LocalTransport, "copy", copy):
        moved = env.run_cli()
    assert moved.exit_code == 0, moved.output
    assert acquired
    assert not unit.exists()


def test_gfs_run_is_recorded(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert env.query(
        "SELECT gfs_job, status, exit_code, moved, deleted, held, deferred, refused, failed,"
        " unmanaged, started_at IS NOT NULL, finished_at IS NOT NULL FROM gfs_runs"
    ) == [("gfs-main", "completed", 0, 1, 0, 0, 0, 0, 0, 0, 1, 1)]
    run_ids = {row[0] for row in env.query("SELECT run_id FROM gfs_steps")}
    assert run_ids == {env.query("SELECT id FROM gfs_runs")[0][0]}


def test_gfs_run_crash_is_recorded_failed(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    config = Config(env.config_path)
    with patch("bdbackup.gfs.runner.decide", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            runner.run_job(config, config.get("gfs-main"), out=lambda _line: None)
    assert env.query("SELECT status, finished_at IS NOT NULL FROM gfs_runs") == [("failed", 1)]


def test_gfs_run_snapshot_failure_sets_exit_code(env):
    (env.root / "NFS/weekly/.bdbackup-destination").unlink()
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    result = env.run_cli()
    assert result.exit_code == 1, result.output
    assert env.query("SELECT status, exit_code, failed FROM gfs_runs") == [("completed", 1, 1)]
    snapshot = env.root / "NFS/daily/gfs-main.ledger.sqlite3"
    with closing(sqlite3.connect(snapshot)) as db:
        assert db.execute("SELECT status FROM gfs_runs").fetchall() == [("completed",)]


def test_second_run_exits_3(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    config = Config(env.config_path)
    before_tree = tree(env.root, exclude=("state",))
    before_db = env.database.read_bytes()
    with process_lock(runner.job_lock_path(config, "gfs-main")):
        result = env.run_cli()
    assert result.exit_code == 3, result.output
    assert "another run of gfs-main is in progress" in result.output
    assert tree(env.root, exclude=("state",)) == before_tree
    assert env.database.read_bytes() == before_db


def test_interrupted_move_completes(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    moved = env.root / "NFS/daily/files/a.tar.gz"
    shutil.copy2(moved, a)
    unit_id, = env.query("SELECT id FROM gfs_units WHERE unit=?", str(a.resolve()))[0]
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute("INSERT INTO gfs_locations (unit_id, stage_path, path) VALUES (?, ?, ?)",
                   (unit_id, str(env.abs("BACKUP")), str(a.resolve())))
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert not a.exists()
    assert moved.exists()
    assert env.query("SELECT count(*) FROM gfs_locations WHERE unit_id=?", unit_id)[0][0] == 1


def test_interrupted_move_keeps_earlier_copy_when_later_is_missing(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    original = a.read_bytes()
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    moved = env.root / "NFS/daily/files/a.tar.gz"
    shutil.copy2(moved, a)
    unit_id, = env.query("SELECT id FROM gfs_units WHERE unit=?", str(a.resolve()))[0]
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute("INSERT INTO gfs_locations (unit_id, stage_path, path) VALUES (?, ?, ?)",
                   (unit_id, str(env.abs("BACKUP")), str(a.resolve())))
    moved.unlink()
    result = env.run_cli()
    assert result.exit_code == 1, result.output
    assert f"refused: missing location {env.abs('NFS/daily/files/a.tar.gz')}" in result.output
    assert a.read_bytes() == original
    assert env.query("SELECT path FROM gfs_locations WHERE unit_id=? ORDER BY id", unit_id) == [
        (str(moved.resolve()),), (str(a.resolve()),)
    ]


def test_history_shows_stage_locations_and_deleted(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    c = env.file_unit("c.tar.gz", days_ago=70)
    b = env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    result = invoke("history", "--config", env.config_path)
    assert result.exit_code == 0, result.output
    by_path = {}
    for line in result.output.splitlines():
        for path in (a, b, c):
            if f"| {path.resolve()} |" in line:
                by_path[path.name] = line
    assert by_path["a.tar.gz"].endswith(
        f"| stage {env.abs('NFS/daily')} | locations {env.abs('NFS/daily/files/a.tar.gz')}"
    ), by_path["a.tar.gz"]
    assert " | deleted 2" in by_path["c.tar.gz"], by_path["c.tar.gz"]
    assert by_path["b.tar.gz"].endswith(
        f"| stage {env.abs('BACKUP')} | locations {env.abs('BACKUP/files/b.tar.gz')}"
    )


def test_replaced_record_stays_unmanaged_after_first_run(env):
    # Spec 2 r7 S2: a record unavailable when GFS first sees it is never managed.
    day = date.today() - timedelta(days=3)
    fixed = env.file_unit("fixed.tar.gz", day=day)
    fixed.write_bytes(b"second content")
    env._record(fixed, {"kind": "file"}, "files", day, "file")
    env.file_unit("new.tar.gz", days_ago=0)
    now = datetime.now()
    code, first = env.run_api(now)
    assert code == 0, first
    code, second = env.run_api(now + timedelta(days=5))
    assert code == 0, second
    assert "unmanaged 1" in first and "unmanaged 1" in second, (first, second)
    assert "move files files/fixed.tar.gz" in second, second
    assert (env.root / "NFS/daily/files/fixed.tar.gz").read_bytes() == b"second content"
    ids = [row[0] for row in env.query(
        "SELECT id FROM backup_runs WHERE path=? ORDER BY id", str(fixed.resolve()))]
    assert env.query("SELECT record_id, managed FROM gfs_members WHERE record_id IN (?, ?)"
                     " ORDER BY record_id", *ids) == [(ids[0], 0), (ids[1], 1)]


def test_first_unavailable_judgment_is_remembered_without_managed_unit(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    record_id = env.query("SELECT id FROM backup_runs WHERE path=?", str(a.resolve()))[0][0]
    original, stats = a.read_bytes(), a.stat()
    env.file_unit("b.tar.gz", days_ago=0)
    a.write_bytes(b"not the original backup")
    result = env.run_cli()
    assert result.exit_code == 0 and "unmanaged 1" in result.output, result.output
    assert env.query("SELECT record_id, managed FROM gfs_members WHERE record_id=?",
                     record_id) == [(record_id, 0)]
    a.write_bytes(original)
    os.utime(a, ns=(stats.st_atime_ns, stats.st_mtime_ns))  # its identity matches again
    result = env.run_cli()
    assert result.exit_code == 0 and "unmanaged 1" in result.output, result.output
    assert a.exists()
    assert not (env.root / "NFS/daily/files/a.tar.gz").exists()


def test_held_unit_judgment_is_stored(env):
    env.write([(["BACKUP"], "daily", "2d"), (["NFS/weekly"], "weekly", "8w")])
    held = None
    for day in [date(2026, 9, 26) + timedelta(n) for n in range(5)]:
        path = env.file_unit(f"{day}.tar.gz", day=day)
        held = path if day == date(2026, 9, 28) else held
    held_id = env.query("SELECT id FROM backup_runs WHERE path=?", str(held.resolve()))[0][0]
    code, output = env.run_api(datetime(2026, 9, 30, 12))
    assert "held: 2026-W40 not complete" in output, output
    assert env.query("SELECT record_id, managed FROM gfs_members WHERE record_id=?",
                     held_id) == [(held_id, 1)]


def test_record_added_to_managed_unit_is_member(env):
    # S2 is judged per record: a later incremental of the newest unit joins its unit.
    unit = env.xtrabackup_unit(3, incrementals=1)
    now = datetime.now()
    code, output = env.run_api(now)
    assert code == 0 and unit.is_dir(), output
    full = next(p for p in unit.iterdir() if p.name.startswith("Full_2"))
    inc2 = unit / "Incremental" / full.name / "inc2"
    write_checkpoints(inc2, 200, 300)
    (inc2 / "data.delta").write_bytes(b"delta 2")
    info = {"kind": "xtrabackup", "backup_root": str((env.root / "BACKUP/mysql/prod").resolve())}
    env._record(inc2, info, "xb", date.today() - timedelta(days=3), "xtrabackup-incremental")
    env.xtrabackup_unit(0)
    code, output = env.run_api(now + timedelta(days=5))
    assert code == 0, output
    moved = env.root / "NFS/daily/mysql/prod" / unit.name
    assert (moved / "Incremental" / full.name / "inc2").is_dir(), output
    assert not unit.exists()


def history_line(env, path):
    result = invoke("history", "--config", env.config_path)
    assert result.exit_code == 0, result.output
    return next(line for line in result.output.splitlines() if f"| {path.resolve()} |" in line)


def test_moved_copy_availability_detects_replacement(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    b = env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    moved = env.root / "NFS/daily/files/a.tar.gz"
    assert "| success | available |" in history_line(env, a)
    assert "| success | available |" in history_line(env, b)
    sibling = moved.with_name("sibling")
    sibling.write_bytes(moved.read_bytes())
    os.replace(sibling, moved)
    assert "| success | unavailable |" in history_line(env, a)
    assert "| success | available |" in history_line(env, b)
    moved.unlink()
    assert "| success | unavailable |" in history_line(env, a)
    assert "| success | available |" in history_line(env, b)


def test_location_without_identities_judged_by_existence(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute("UPDATE gfs_locations SET identities = NULL")
    assert "| success | available |" in history_line(env, a)
    (env.root / "NFS/daily/files/a.tar.gz").unlink()
    assert "| success | unavailable |" in history_line(env, a)


def test_restore_moved_file_unit(env):
    a = env.archive_unit("a.tar.gz", 7)
    env.archive_unit("b.tar.gz", 0)
    assert env.run_cli().exit_code == 0
    record_id = env.query("SELECT id FROM backup_runs WHERE path=?", str(a.resolve()))[0][0]
    out = env.root / "out"
    result = invoke("restore", "--config", env.config_path, "--backup-id", record_id, "--yes",
                    "-d", out)
    assert result.exit_code == 0, result.output
    assert (out / "hello").read_text() == "recover a.tar.gz"
    moved = env.abs("NFS/daily/files/a.tar.gz")
    assert f"Locations: {moved}" in result.output, result.output
    assert f"Backup: {moved}" in result.output, result.output


def test_restore_moved_xtrabackup_tip_uses_stage_root(env, prepare_runner):
    with patch("subprocess.run", side_effect=prepare_runner):
        full, _, tip = env.engine_chain(7)
        hot = Path(full.unit)
        assert env.run_cli().exit_code == 0
        prepare_runner.commands.clear()
        out = env.root / "out"
        result = invoke("restore", "--config", env.config_path, "--backup-id", tip.id, "--yes",
                        "-d", out)
    assert result.exit_code == 0, result.output
    moved = env.abs("NFS/daily/mysql/prod") / hot.name
    assert f"Backup: {moved / Path(tip.path).relative_to(hot)}" in result.output, result.output
    assert f"Prepared recovery directory: {out}" in result.output, result.output
    assert sum(any(a.startswith("--incremental-dir=") for a in cmd)
               for cmd in prepare_runner.commands) == 2
    assert not hot.exists()


def corrupt_in_place(path):
    """Different bytes of the same length and mtime: the identity still matches."""
    stat = path.stat()
    path.write_bytes(bytes(b ^ 0xFF for b in path.read_bytes()))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def test_restore_verifies_and_uses_first_path_that_verifies(env, caplog):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/daily", "NFS/daily2"], "daily", "20d"),
               (["NFS/weekly"], "weekly", "8w")])
    a = env.archive_unit("a.tar.gz", 7)
    env.archive_unit("b.tar.gz", 0)
    assert env.run_cli().exit_code == 0
    record_id = env.query("SELECT id FROM backup_runs WHERE path=?", str(a.resolve()))[0][0]
    first, second = env.abs("NFS/daily/files/a.tar.gz"), env.abs("NFS/daily2/files/a.tar.gz")
    corrupt_in_place(first)
    out = env.root / "out"
    result = invoke("restore", "--config", env.config_path, "--backup-id", record_id, "--yes",
                    "-d", out)
    assert f"Backup: {second}" in result.output, result.output
    assert f"Backup: {first}" not in result.output, result.output
    assert result.exit_code == 0, result.output
    assert (out / "hello").read_text() == "recover a.tar.gz"
    corrupt_in_place(second)
    out2 = env.root / "out2"
    caplog.clear()
    result = invoke("restore", "--config", env.config_path, "--backup-id", record_id, "--yes",
                    "-d", out2)
    assert "checksum mismatch" in caplog.text, caplog.text
    assert result.exit_code == 1, result.output
    assert not out2.exists()


def test_restore_xtrabackup_verifies_members_used(env, prepare_runner, caplog):
    with patch("subprocess.run", side_effect=prepare_runner):
        full, first, tip = env.engine_chain(7)
        hot = Path(full.unit)
        assert env.run_cli().exit_code == 0
        moved = env.abs("NFS/daily/mysql/prod") / hot.name
        corrupt_in_place(moved / Path(tip.path).relative_to(hot) / "data.ibd")
        result = invoke("restore", "--config", env.config_path, "--backup-id", first.id,
                        "--yes", "-d", env.root / "out1")
        assert result.exit_code == 0, result.output
        corrupt_in_place(moved / Path(full.path).relative_to(hot) / "data.ibd")
        caplog.clear()
        result = invoke("restore", "--config", env.config_path, "--backup-id", first.id,
                        "--yes", "-d", env.root / "out2")
    assert "checksum mismatch" in caplog.text, caplog.text
    assert result.exit_code == 1, result.output
    assert not (env.root / "out2").exists()


SNAPSHOT_TABLES = {"backup_runs": "id", "gfs_units": "id", "gfs_locations": "id",
                   "gfs_members": "record_id", "gfs_steps": "id"}


def ledger_rows(database):
    with closing(sqlite3.connect(database)) as db:
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall()  # noqa: S608
                for table, key in SNAPSHOT_TABLES.items()}


def test_snapshot_written_to_every_later_stage_path(env):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/daily", "NFS/daily2"], "daily", "20d"),
               (["NFS/weekly"], "weekly", "8w")])
    env.file_unit("a", days_ago=7)
    env.file_unit("b", days_ago=0)
    for run in range(2):
        if run:
            c = env.file_unit("c", days_ago=7)
        result = env.run_cli()
        assert result.exit_code == 0, result.output
        live = ledger_rows(env.database)
        for stage in ("NFS/daily", "NFS/daily2", "NFS/weekly"):
            snapshot = env.abs(stage) / "gfs-main.ledger.sqlite3"
            assert snapshot.is_file(), result.output
            assert ledger_rows(snapshot) == live, stage
            assert snapshot.stat().st_mode & 0o777 == 0o600
        assert not (env.root / "BACKUP/gfs-main.ledger.sqlite3").exists()
        assert not [p for p in (env.root / "NFS").rglob("*") if ".gfs-tmp-" in p.name]
        assert not Path(f"{env.database}.gfs-gfs-main.snapshot").exists()
        assert f"snapshot {env.abs('NFS/daily')}/gfs-main.ledger.sqlite3" in result.output
    assert not c.exists()
    assert str(env.abs("NFS/daily/files/c")) in [row[3] for row in live["gfs_locations"]]


def test_snapshot_restores_moved_unit(env):
    a = env.archive_unit("a.tar.gz", 7)
    env.archive_unit("b.tar.gz", 0)
    assert env.run_cli().exit_code == 0
    snapshot = env.root / "NFS/daily/gfs-main.ledger.sqlite3"
    assert snapshot.is_file()
    record_id = env.query("SELECT id FROM backup_runs WHERE path=?", str(a.resolve()))[0][0]
    (env.root / "elsewhere").mkdir()
    shutil.copy(snapshot, env.root / "elsewhere/h.sqlite3")
    other = env.root / "other.toml"
    other.write_text('[history]\ndatabase = "elsewhere/h.sqlite3"\n')
    out = env.root / "out"
    result = invoke("restore", "--config", other, "--backup-id", record_id, "--yes", "-d", out)
    assert result.exit_code == 0, result.output
    assert (out / "hello").read_text() == "recover a.tar.gz"


def test_snapshot_missing_marker_fails_run(env):
    (env.root / "NFS/weekly/.bdbackup-destination").unlink()
    env.file_unit("a", days_ago=7)
    env.file_unit("b", days_ago=0)
    result = env.run_cli()
    assert result.exit_code == 1, result.output
    assert (env.root / "NFS/daily/gfs-main.ledger.sqlite3").is_file()
    assert not [p for p in (env.root / "NFS/weekly").rglob("*") if p.is_file()]
    assert f"failed snapshot {env.abs('NFS/weekly')}: destination not ready" in result.output
    assert "failed 1" in result.output, result.output


def test_report_lists_per_stage_totals(env, tmp_path):
    a = env.file_unit("a", days_ago=7)
    b = env.file_unit("b", days_ago=0)
    sizes = {a.name: a.stat().st_size, b.name: b.stat().st_size}
    dry = Env(tmp_path / "dry")
    dry.write(DEFAULT_STAGES, apply=False)
    dry.file_unit("a", days_ago=7)
    dry.file_unit("b", days_ago=0)
    for run in (env, dry):
        result = run.run_cli()
        assert result.exit_code == 0, result.output
        after = result.output.split("Summary:", 1)[1].splitlines()[1:]
        stages = [line for line in after if line.startswith("Stage ")]
        assert stages == [
            f"Stage {run.abs('BACKUP')}: 1 unit ({sizes['b']} bytes)",
            f"Stage {run.abs('NFS/daily')}: 1 unit ({sizes['a']} bytes)",
            f"Stage {run.abs('NFS/weekly')}: 0 units (0 bytes)",
        ], result.output
