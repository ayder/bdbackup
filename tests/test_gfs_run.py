"""GFS runs against temporary stage directories and a real SQLite history database."""

import errno
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupResult
from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.gfs import actions, runner
from bdbackup.history import History, HistoryError, artifact_identity
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
        self.dry_run = False
        self.write(DEFAULT_STAGES)
        self.history = History(Config(self.config_path).history)

    def write(self, stages):
        text = '[history]\ndatabase="state/history.sqlite3"\n[gfs-main]\ntype="gfs"\n'
        for paths, period, keep, *options in stages:  # options: more "key = value" lines
            quoted = ", ".join(f'"{p}"' for p in paths)
            text += (f"[[gfs-main.stage]]\npaths = [{quoted}]\nperiod = \"{period}\"\n"
                     f"keep = \"{keep}\"\n" + "".join(f"{option}\n" for option in options))
        self.config_path.write_text(text)

    def feed_first_stage(self):
        """Add a pgbackrest job writing into the first stage; the config is rewritten once."""
        self.config_path.write_text(
            self.config_path.read_text()
            + '[pg]\ntype = "pgbackrest"\nstanza = "main"\nrepo_path = "repo"\n'
              'out_dir = "BACKUP/pg"\n'
        )

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
        return invoke("run", "-c", self.config_path, "-j", "gfs-main",
                      *(["--dry-run"] if self.dry_run else []))

    def run_api(self, now, *, outcome=None, transport=None):
        config = Config(self.config_path)
        lines = []
        code = runner.run_job(config, config.get("gfs-main"), now=now, out=lines.append,
                              dry_run=self.dry_run, outcome=outcome, transport=transport)
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


class _CopyOnly(actions.LocalTransport):
    """The copy path: this transport never offers a rename (0.7.4 renames within one device)."""

    def same_filesystem(self, a, b):
        return False


@pytest.fixture
def copy_path(monkeypatch):
    """Pin a test to the copy path: every run gets ``_CopyOnly`` instead of ``LocalTransport``."""
    monkeypatch.setattr(runner, "LocalTransport", _CopyOnly)


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


def test_move_refuses_modified_mirror(env, copy_path):
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


def test_failed_path_leaves_no_copy(env, copy_path):
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


def test_checksum_mismatch_refused_until_accepted(env, copy_path):
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


class _FailingCopy(_CopyOnly):
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


def test_move_keeps_directory_modes(env, copy_path):
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


def _squashed(error):
    """What a share that maps every user to one account answers to chmod; EPERM and EACCES
    arrive as ``PermissionError``."""
    return OSError(error, "squashed")


def test_copy_survives_eperm_from_chmod(env, copy_path):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    before = tree(unit)
    with patch("bdbackup.gfs.actions.os.chmod", side_effect=_squashed(errno.EPERM)), \
            patch("bdbackup.gfs.actions.shutil.copymode", side_effect=_squashed(errno.EPERM)):
        code, output = env.run_api(datetime.now())
    assert code == 0, output
    assert env.query("SELECT moved, failed FROM gfs_runs") == [(1, 0)]
    moved = env.root / "NFS/daily" / unit.relative_to(env.root / "BACKUP")
    assert tree(moved) == before
    assert not unit.exists()
    assert (env.root / "NFS/daily/gfs-main.ledger.sqlite3").is_file()
    assert re.search(r"^permission bits not kept on [1-9]\d* entries: "
                     r"the destination refused chmod$", output, re.MULTILINE), output


@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES], ids=["EIO", "EACCES"])
@pytest.mark.parametrize("call", ["os.chmod", "shutil.copymode"])
def test_copy_fails_on_other_chmod_errors(env, copy_path, call, error):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    before = tree(unit)
    with patch(f"bdbackup.gfs.actions.{call}", side_effect=_squashed(error)):
        code, output = env.run_api(datetime.now())
    assert code == 1, output
    assert "squashed" in output
    assert "permission bits not kept" not in output
    assert env.query("SELECT moved FROM gfs_runs") == [(0,)]
    assert tree(unit) == before
    assert not stage_files(env, "NFS/daily")


def test_report_has_no_chmod_line_when_modes_kept(env, copy_path):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    code, output = env.run_api(datetime.now())
    assert code == 0, output
    assert "permission bits not kept" not in output


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
    env.dry_run = True
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


def test_copy_holds_no_engine_lock_and_commit_defers(env, copy_path):
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


def test_running_backup_defers_its_job(env):
    a = env.file_unit("a.tar.gz", days_ago=7)
    c = env.file_unit("c.tar.gz", days_ago=70)  # expired, but its job is still backing up
    env.file_unit("b.tar.gz", days_ago=0)
    env.file_unit("x.tar.gz", days_ago=7, series="other")
    env.file_unit("y.tar.gz", days_ago=0, series="other")
    with closing(sqlite3.connect(env.database)) as db, db:
        running = db.execute(
            "INSERT INTO backup_runs (job_name, backup_type, started_at, status, restore_info)"
            " VALUES ('files', 'file', ?, 'running', '{}')", (datetime.now(UTC).isoformat(),),
        ).lastrowid
    result = env.run_cli()
    assert result.exit_code == 3, result.output
    deferred = [line for line in result.output.splitlines()
                if f"deferred: backup running (record {running}, started " in line]
    assert any("files/a.tar.gz" in line for line in deferred), result.output
    assert any("files/c.tar.gz" in line for line in deferred), result.output
    assert a.exists() and c.exists()
    assert not (env.root / "NFS/daily/files/a.tar.gz").exists()
    assert (env.root / "NFS/daily/files/x.tar.gz").exists()
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute("UPDATE backup_runs SET status='failed' WHERE id=?", (running,))
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert (env.root / "NFS/daily/files/a.tar.gz").exists()
    assert not c.exists()


def ledger_dump(env):
    """Every statement that rebuilds the history database, but those for ``last_runs``."""
    with closing(sqlite3.connect(env.database)) as db:
        return [line for line in db.iterdump() if "last_runs" not in line]


def test_second_run_exits_3(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    config = Config(env.config_path)
    before_tree = tree(env.root, exclude=("state",))
    before_ledger = ledger_dump(env)
    with process_lock(runner.job_lock_path(config, "gfs-main")):
        result = env.run_cli()
    assert result.exit_code == 3, result.output
    assert "another run of gfs-main is in progress" in result.output
    assert tree(env.root, exclude=("state",)) == before_tree
    # The run only notes in last_runs that it found the job locked.
    assert ledger_dump(env) == before_ledger


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


def single_stage_env(tmp_path, *, fed, marker):
    env = Env(tmp_path)
    env.write([(["BACKUP"], "daily", "5d")])
    if fed:
        env.feed_first_stage()
    if marker:
        (env.root / "BACKUP/.bdbackup-destination").touch()
    env.file_unit("a", days_ago=0)
    return env


def test_snapshot_reaches_marked_fed_first_stage(tmp_path):
    env = single_stage_env(tmp_path, fed=True, marker=True)
    result = env.run_cli()
    snapshot = env.abs("BACKUP") / "gfs-main.ledger.sqlite3"
    assert result.exit_code == 0, result.output
    assert snapshot.is_file(), result.output
    assert ledger_rows(snapshot) == ledger_rows(env.database)
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert f"snapshot {snapshot}" in result.output.splitlines(), result.output
    assert not [p for p in env.abs("BACKUP").rglob("*") if ".gfs-tmp-" in p.name]
    assert not Path(f"{env.database}.gfs-gfs-main.snapshot").exists()


def test_snapshot_skips_unfed_first_stage(tmp_path):
    env = single_stage_env(tmp_path, fed=False, marker=False)
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert not (env.abs("BACKUP") / "gfs-main.ledger.sqlite3").exists()
    assert not [line for line in result.output.splitlines()
                if line.startswith(("snapshot ", "failed snapshot ", "no ledger"))], result.output


def test_snapshot_fed_first_stage_without_marker(tmp_path):
    env = single_stage_env(tmp_path, fed=True, marker=False)
    result = env.run_cli()
    assert result.exit_code == 1, result.output
    assert not (env.abs("BACKUP") / "gfs-main.ledger.sqlite3").exists()
    assert (f"failed snapshot {env.abs('BACKUP')}: destination not ready"
            in result.output.splitlines()), result.output
    assert "failed 1" in result.output, result.output


def test_report_lists_per_stage_totals(env, tmp_path):
    a = env.file_unit("a", days_ago=7)
    b = env.file_unit("b", days_ago=0)
    sizes = {a.name: a.stat().st_size, b.name: b.stat().st_size}
    dry = Env(tmp_path / "dry")
    dry.dry_run = True
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


# ---- the outcome of each run, kept for the metrics command ------------------------------------


def last_run(env):
    runs = env.history.last_runs()
    assert "gfs-main" in runs, f"the run recorded no last_runs row: {sorted(runs)}"
    return runs["gfs-main"]


def test_last_run_of_a_completed_gfs_run(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    result = env.run_cli()

    assert result.exit_code == 0, result.output
    row = last_run(env)
    assert (row.job_type, row.result, row.exit_code) == ("gfs", "completed", 0)
    assert (row.facts or {}).get("moves") == {"rename": 1, "copy": 0}  # it now keeps its facts
    assert datetime.fromisoformat(row.finished_at).utcoffset() == timedelta(0)


def test_last_run_of_a_gfs_run_with_a_failure_is_failed(env):
    (env.root / "NFS/weekly/.bdbackup-destination").unlink()  # the snapshot copy fails
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    result = env.run_cli()

    assert result.exit_code == 1, result.output
    row = last_run(env)
    assert (row.result, row.exit_code) == ("failed", 1)


def defer_a_unit(env):
    """A unit whose job is still backing up: the run defers it and exits 3."""
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute(
            "INSERT INTO backup_runs (job_name, backup_type, started_at, status, restore_info)"
            " VALUES ('files', 'file', ?, 'running', '{}')", (datetime.now(UTC).isoformat(),),
        )


def test_last_run_of_a_gfs_run_with_deferred_work_is_deferred(env):
    defer_a_unit(env)

    result = env.run_cli()

    assert result.exit_code == 3, result.output
    row = last_run(env)
    assert (row.result, row.exit_code) == ("deferred", 3)


def test_last_run_of_a_busy_job_lock_is_locked(env):
    env.file_unit("a.tar.gz", days_ago=0)
    config = Config(env.config_path)

    with process_lock(runner.job_lock_path(config, "gfs-main")):
        result = env.run_cli()

    assert result.exit_code == 3, result.output
    row = last_run(env)
    assert (row.result, row.exit_code, row.facts) == ("locked", 3, None)


def test_last_run_of_a_gfs_run_that_raises_is_error_and_it_still_raises(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    with patch("bdbackup.gfs.runner.decide", side_effect=RuntimeError("injected")):
        result = env.run_cli()

    assert isinstance(result.exception, RuntimeError), result.output
    assert result.exit_code == 1
    row = last_run(env)
    assert (row.result, row.exit_code) == ("error", 1)


def test_a_gfs_dry_run_leaves_the_last_run_alone(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    before = last_run(env)
    env.dry_run = True

    result = env.run_cli()

    assert result.exit_code == 0, result.output
    assert env.history.last_runs()["gfs-main"] == before


@pytest.mark.parametrize("error", [HistoryError("disk full"), OSError("read-only")],
                         ids=["history", "os"])
def test_a_gfs_last_run_recording_failure_is_a_warning_and_changes_nothing_else(env, caplog, error):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    caplog.set_level(logging.WARNING)

    with patch.object(History, "record_last_run", side_effect=error):
        result = env.run_cli()

    assert result.exit_code == 0, result.output
    assert "GFS run gfs-main" in result.output
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and r.name == "bdbackup.cli"]
    assert len(warnings) == 1, caplog.text
    assert "last run" in warnings[0].getMessage()
    assert str(error) in warnings[0].getMessage()


def test_last_run_needs_run_job_to_say_whether_exit_3_is_a_busy_lock_or_deferred_work(env):
    config = Config(env.config_path)
    job = config.get("gfs-main")
    quiet = {"out": lambda _line: None}

    busy = runner.RunOutcome()
    with process_lock(runner.job_lock_path(config, "gfs-main")):
        assert runner.run_job(config, job, outcome=busy, **quiet) == 3
    assert busy.locked is True

    defer_a_unit(env)
    deferred = runner.RunOutcome()
    assert runner.run_job(config, job, outcome=deferred, **quiet) == 3
    assert deferred.locked is False


def test_last_run_needs_no_outcome_from_run_job_it_returns_what_it_always_did(env):
    config = Config(env.config_path)
    job = config.get("gfs-main")
    env.file_unit("b.tar.gz", days_ago=0)

    assert runner.run_job(config, job, out=lambda _line: None) == 0
    with process_lock(runner.job_lock_path(config, "gfs-main")):
        assert runner.run_job(config, job, out=lambda _line: None) == 3


# ---- verify = false: no checksum re-reads (0.7.4, D1, D2) ------------------------------------

NO_VERIFY = "verify = false"


@pytest.fixture
def checksums():
    """The paths ``bdbackup.gfs.actions`` checksums: what a verify step reads again."""
    calls = []
    original = actions.checksum

    def spy(path):
        calls.append(Path(path))
        return original(path)

    with patch("bdbackup.gfs.actions.checksum", spy):
        yield calls


def later_stage(*options):
    """A first stage and one later stage, with ``options`` on the later one."""
    return [(["BACKUP"], "daily", "5d"), (["NFS/daily"], "daily", "20d", *options)]


def expire_in_nfs_daily(env, checksums, make_unit, *, options):
    """A unit moved to NFS/daily, which is not the newest of its series, and the day it expires.

    Returns the unit's path at NFS/daily and the clock to run at: 60 days on. The checksums
    the move read are forgotten: a test looks at the run that follows.
    """
    env.write(later_stage(*options))
    unit = make_unit()
    assert env.run_api(datetime.now())[0] == 0
    moved = env.root / "NFS/daily" / unit.relative_to(env.root / "BACKUP")
    assert moved.exists() and not unit.exists()
    checksums.clear()
    return moved, datetime.now() + timedelta(days=60)


def file_pair(env):
    def make():
        a = env.file_unit("a.tar.gz", days_ago=7)
        env.file_unit("b.tar.gz", days_ago=0)
        return a
    return make


@pytest.mark.parametrize("options", [(), (NO_VERIFY,)], ids=["default", "verify-false"])
def test_a_changed_unit_is_deleted_only_in_a_verify_false_stage(env, checksums, options):
    moved, later = expire_in_nfs_daily(env, checksums, file_pair(env), options=options)
    moved.write_bytes(b"changed on disk")

    code, output = env.run_api(later)

    if options:
        assert code == 0, output
        assert "delete files files/a.tar.gz" in output, output
        assert not moved.exists()
        assert checksums == []
        assert env.query("SELECT count(*) FROM gfs_units WHERE deleted_at IS NOT NULL") == [(1,)]
    else:
        assert code == 1, output
        assert "refused: checksum mismatch" in output
        assert moved.read_bytes() == b"changed on disk"
        assert checksums == [moved.resolve()]


def make_missing(moved):
    moved.unlink()
    return f"refused: missing location {moved.resolve()}"


def make_directory(moved):
    moved.unlink()
    moved.mkdir()
    return "refused: unexpected entry a.tar.gz"


@pytest.mark.parametrize("damage", [make_missing, make_directory],
                         ids=["missing-location", "directory-in-place"])
def test_a_verify_false_stage_still_refuses_a_file_unit_that_is_not_there_or_not_a_file(
    env, checksums, damage
):
    moved, later = expire_in_nfs_daily(env, checksums, file_pair(env), options=(NO_VERIFY,))
    reason = damage(moved)

    code, output = env.run_api(later)

    assert code == 1, output
    assert reason in output, output
    assert checksums == []
    assert env.query("SELECT count(*) FROM gfs_units WHERE deleted_at IS NOT NULL") == [(0,)]


def test_a_verify_false_stage_still_refuses_an_unexpected_entry_in_an_xtrabackup_unit(
    env, checksums
):
    def make():
        unit = env.xtrabackup_unit(7)
        env.xtrabackup_unit(0)
        return unit

    moved, later = expire_in_nfs_daily(env, checksums, make, options=(NO_VERIFY,))
    (moved / "stray").mkdir()
    before = tree(moved)

    code, output = env.run_api(later)

    assert code == 1, output
    assert "refused: unexpected entry stray" in output, output
    assert tree(moved) == before
    assert checksums == []


MIRRORED = [(["BACKUP"], "daily", "5d"),
            (["NFS/daily", "NFS/daily2"], "daily", "20d", NO_VERIFY),
            (["NFS/weekly"], "weekly", "8w")]


def mirrored_unit(env):
    """Unit a in both paths of the verify = false stage; the next run moves it to weekly."""
    env.write(MIRRORED)
    env.file_unit("a.tar.gz", day=date(2026, 8, 30))  # a Sunday, alone in ISO week 35
    env.file_unit("z.tar.gz", day=date(2026, 9, 29))
    assert env.run_api(datetime(2026, 9, 5, 12))[0] == 0
    return (env.root / "NFS/daily/files/a.tar.gz", env.root / "NFS/daily2/files/a.tar.gz",
            env.root / "NFS/weekly/files/a.tar.gz")


def test_a_changed_second_copy_in_a_verify_false_stage_does_not_block_the_move(env, checksums):
    first, second, weekly = mirrored_unit(env)
    original = first.read_bytes()
    second.write_bytes(b"tampered")

    code, output = env.run_api(datetime(2026, 9, 29, 12))  # a's target is weekly

    assert code == 0, output
    assert weekly.read_bytes() == original
    assert not first.exists() and not second.exists()
    assert checksums == []


def test_a_changed_first_copy_in_a_verify_false_stage_is_still_refused(env, checksums):
    first, second, weekly = mirrored_unit(env)
    original = second.read_bytes()
    first.write_bytes(b"tampered")  # the copy the move reads: its hash rides on that read (D3)

    code, output = env.run_api(datetime(2026, 9, 29, 12))

    assert code == 1, output
    assert "refused: checksum mismatch" in output
    assert first.read_bytes() == b"tampered" and second.read_bytes() == original
    assert not weekly.exists()
    assert checksums == []


def leave_an_interrupted_move(env, checksums, *, options, first=()):
    """Unit a at NFS/daily, recorded there, with its earlier copy still in BACKUP.

    ``options`` go on the later stage, ``first`` on the first one the earlier copy is left in.
    The checksums the first move read are forgotten.
    """
    env.write([(["BACKUP"], "daily", "5d", *first), *later_stage(*options)[1:]])
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    moved = env.root / "NFS/daily/files/a.tar.gz"
    shutil.copy2(moved, a)
    unit_id, = env.query("SELECT id FROM gfs_units WHERE unit=?", str(a.resolve()))[0]
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute("INSERT INTO gfs_locations (unit_id, stage_path, path) VALUES (?, ?, ?)",
                   (unit_id, str(env.abs("BACKUP")), str(a.resolve())))
    checksums.clear()
    return a, moved, unit_id


@pytest.mark.parametrize("options", [(), (NO_VERIFY,)], ids=["default", "verify-false"])
def test_a_changed_later_copy_blocks_an_interrupted_move_only_in_a_default_stage(
    env, checksums, options
):
    a, moved, unit_id = leave_an_interrupted_move(env, checksums, options=options)
    moved.write_bytes(b"changed later copy")

    result = env.run_cli()

    if options:
        assert result.exit_code == 0, result.output
        assert not a.exists()
        # the later copy is not read; the earlier one is, as its own stage (default) says
        assert checksums == [a.resolve()]
        assert env.query("SELECT count(*) FROM gfs_locations WHERE unit_id=?", unit_id) == [(1,)]
    else:
        assert result.exit_code == 1, result.output
        assert "refused: checksum mismatch" in result.output
        assert a.exists()
        assert env.query("SELECT count(*) FROM gfs_locations WHERE unit_id=?", unit_id) == [(2,)]


def test_an_interrupted_move_between_two_verify_false_stages_reads_neither_copy(env, checksums):
    a, moved, unit_id = leave_an_interrupted_move(
        env, checksums, options=(NO_VERIFY,), first=(NO_VERIFY,)
    )
    moved.write_bytes(b"changed later copy")

    result = env.run_cli()

    assert result.exit_code == 0, result.output
    assert not a.exists()
    assert checksums == []
    assert env.query("SELECT count(*) FROM gfs_locations WHERE unit_id=?", unit_id) == [(1,)]


def test_a_missing_later_copy_still_keeps_the_earlier_one_in_a_verify_false_stage(env, checksums):
    a, moved, unit_id = leave_an_interrupted_move(env, checksums, options=(NO_VERIFY,))
    moved.unlink()

    result = env.run_cli()

    assert result.exit_code == 1, result.output
    assert f"refused: missing location {moved.resolve()}" in result.output
    assert a.exists()
    assert checksums == []
    assert env.query("SELECT count(*) FROM gfs_locations WHERE unit_id=?", unit_id) == [(2,)]


# ---- a move within one filesystem is a rename (0.7.4, D6, D7) ---------------------------------

HINT = 'see docs/README_GFS.md, "Final name exists"'


@pytest.fixture
def transfers():
    """What ``LocalTransport`` is asked to do with a unit: copies (not the ledger snapshot's)
    and relocations."""
    calls = SimpleNamespace(copies=[], relocations=[])
    copy, relocate = actions.LocalTransport.copy, actions.LocalTransport.relocate

    def spy_copy(self, source, temp, on_file):
        if Path(source).suffix != ".snapshot":
            calls.copies.append(Path(source))
        return copy(self, source, temp, on_file)

    def spy_relocate(self, source, final):
        calls.relocations.append((Path(source), Path(final)))
        return relocate(self, source, final)

    with (patch.object(actions.LocalTransport, "copy", spy_copy),
          patch.object(actions.LocalTransport, "relocate", spy_relocate)):
        yield calls


def test_a_move_within_one_filesystem_renames_the_unit(env, checksums, transfers):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    source, inode = a.resolve(), a.stat().st_ino
    moved = env.abs("NFS/daily/files/a.tar.gz")

    code, output = env.run_api(datetime.now())

    assert code == 0, output
    assert moved.stat().st_ino == inode and not a.exists()
    assert transfers.copies == []
    assert transfers.relocations == [(source, moved)]
    assert checksums == [source]  # the source stage verifies: one read, no write
    unit_id, = env.query("SELECT id FROM gfs_units WHERE unit=?", str(source))[0]
    [(stage_path, path, identities)] = env.query(
        "SELECT stage_path, path, identities FROM gfs_locations WHERE unit_id=?", unit_id)
    assert (stage_path, path) == (str(env.abs("NFS/daily")), str(moved))
    member_id, = env.query("SELECT id FROM backup_runs WHERE unit=?", str(source))[0]
    assert json.loads(identities) == {str(member_id): artifact_identity(moved)}
    assert env.query("SELECT action, source, destination, outcome FROM gfs_steps"
                     " WHERE unit_id=?", unit_id) == [("move", str(source), str(moved), "ok")]


def test_a_rename_out_of_a_verify_false_stage_reads_nothing(env, checksums, transfers):
    env.write([(["BACKUP"], "daily", "5d", NO_VERIFY), *later_stage()[1:]])
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    inode = a.stat().st_ino

    code, output = env.run_api(datetime.now())

    assert code == 0, output
    assert env.abs("NFS/daily/files/a.tar.gz").stat().st_ino == inode
    assert transfers.copies == [] and len(transfers.relocations) == 1
    assert checksums == []


def test_a_changed_unit_in_a_verifying_stage_is_refused_and_nothing_is_renamed(
    env, checksums, transfers
):
    # GFS records a unit when it first sees it; the file changes on disk after that
    a = env.file_unit("a.tar.gz", day=date(2026, 9, 25))
    env.file_unit("b.tar.gz", day=date(2026, 9, 29))
    assert env.run_api(datetime(2026, 9, 29, 12))[0] == 0 and a.exists()
    a.write_bytes(b"changed on disk")
    checksums.clear()

    code, output = env.run_api(datetime(2026, 10, 2, 12))  # a is now due for NFS/daily

    assert code == 1, output
    assert "refused: checksum mismatch" in output
    assert a.read_bytes() == b"changed on disk"
    assert not (env.root / "NFS/daily/files/a.tar.gz").exists()
    assert transfers.relocations == [] and transfers.copies == []
    assert checksums == [a.resolve()]  # the verify step read it; the copy path hashes in the copy


def test_exdev_from_the_rename_falls_back_to_the_copy(env, checksums, transfers):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    original, inode, source = a.read_bytes(), a.stat().st_ino, a.resolve()
    moved = env.abs("NFS/daily/files/a.tar.gz")

    with patch.object(actions.LocalTransport, "relocate", autospec=True,
                      side_effect=OSError(errno.EXDEV, "Invalid cross-device link")) as relocate:
        code, output = env.run_api(datetime.now())

    assert code == 0, output
    assert relocate.call_count == 1
    assert moved.read_bytes() == original and not a.exists()
    assert moved.stat().st_ino != inode  # copied, then the source removed
    assert transfers.copies == [source]
    assert checksums == [source]  # the one case that reads a verifying stage's unit twice
    assert "renamed" not in output
    unit_id, = env.query("SELECT id FROM gfs_units WHERE unit=?", str(source))[0]
    assert env.query("SELECT path FROM gfs_locations WHERE unit_id=?", unit_id) == [(str(moved),)]


def test_a_two_path_target_stage_still_copies(env, checksums, transfers):
    env.write(TWO_DAILY_PATHS)
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    inode = a.stat().st_ino

    code, output = env.run_api(datetime.now())

    assert code == 0, output
    assert transfers.relocations == []
    assert transfers.copies == [a.resolve(), a.resolve()]
    assert checksums == []
    assert env.abs("NFS/daily/files/a.tar.gz").stat().st_ino != inode and not a.exists()
    assert "renamed" not in output


def test_a_unit_with_two_locations_still_copies(env, checksums, transfers):
    first, second, weekly = mirrored_unit(env)
    transfers.copies.clear()
    transfers.relocations.clear()

    code, output = env.run_api(datetime(2026, 9, 29, 12))

    assert code == 0, output
    assert transfers.relocations == []
    assert transfers.copies == [first.resolve()]
    assert weekly.exists() and not first.exists() and not second.exists()
    assert "renamed" not in output


def test_an_existing_final_name_is_refused_before_the_rename(env, checksums, transfers):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    original = a.read_bytes()
    final = env.abs("NFS/daily/files/a.tar.gz")
    final.parent.mkdir(parents=True)
    final.write_bytes(b"in the way")

    code, output = env.run_api(datetime.now())

    assert code == 1, output
    assert f"refused: final name exists: {final}; {HINT}" in output
    assert a.read_bytes() == original and final.read_bytes() == b"in the way"
    assert transfers.relocations == [] and transfers.copies == [] and checksums == []


def test_an_xtrabackup_unit_is_renamed_under_both_root_locks(env, transfers):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    before, inode = tree(unit), unit.stat().st_ino
    source_lock = env.abs("BACKUP/mysql/prod") / ".bdbackup.lock"
    target_lock = env.abs("NFS/daily/mysql/prod") / ".bdbackup.lock"
    assert not (env.root / "NFS/daily/mysql").exists()  # the target directory is made on the way
    held = []
    relocate = actions.LocalTransport.relocate

    def relocate_under_locks(self, source, final):
        for lock in (source_lock, target_lock):
            with pytest.raises(BlockingIOError), process_lock(lock):
                pass
            held.append(lock)
        return relocate(self, source, final)

    with patch.object(actions.LocalTransport, "relocate", relocate_under_locks):
        code, output = env.run_api(datetime.now())

    moved = env.root / "NFS/daily/mysql/prod" / unit.name
    assert code == 0, output
    assert held == [source_lock, target_lock]
    assert tree(moved) == before and moved.stat().st_ino == inode and not unit.exists()
    assert transfers.copies == []


@pytest.mark.parametrize("busy", ["BACKUP/mysql/prod", "NFS/daily/mysql/prod"],
                         ids=["source-root", "target-root"])
def test_a_busy_root_lock_defers_the_rename_of_an_xtrabackup_unit(env, transfers, busy):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    before = tree(unit)

    with process_lock(env.abs(busy) / ".bdbackup.lock"):
        code, output = env.run_api(datetime.now())

    assert code == 3, output
    assert "deferred: locked" in output
    assert tree(unit) == before
    assert transfers.relocations == []
    assert not (env.root / "NFS/daily/mysql/prod" / unit.name).exists()


def test_a_crash_after_the_rename_fails_this_run_and_the_next_one_refuses(env, checksums):
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    original, source = a.read_bytes(), a.resolve()
    final = env.abs("NFS/daily/files/a.tar.gz")

    with patch.object(runner.store, "record_move", side_effect=OSError("disk full")):
        code, output = env.run_api(datetime.now())

    assert code == 1, output
    assert "failed: disk full" in output
    assert final.read_bytes() == original and not a.exists()  # renamed; the ledger does not know
    unit_id, = env.query("SELECT id FROM gfs_units WHERE unit=?", str(source))[0]
    locations = env.query("SELECT path FROM gfs_locations WHERE unit_id=?", unit_id)
    assert locations == [(str(source),)]

    code, output = env.run_api(datetime.now())

    assert code == 1, output
    assert f"refused: final name exists: {final}; {HINT}" in output
    assert final.read_bytes() == original and not a.exists()  # both paths are left as they are
    assert env.query("SELECT path FROM gfs_locations WHERE unit_id=?", unit_id) == locations


def test_the_applied_report_line_ends_renamed_and_the_dry_run_line_is_unchanged(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    move = (r"move files files/a\.tar\.gz \d{4}-\d\d-\d\d "
            rf"{re.escape(str(env.abs('BACKUP')))} -> {re.escape(str(env.abs('NFS/daily')))} ")
    env.dry_run = True

    code, output = env.run_api(datetime.now())

    assert code == 0, output
    assert re.search(rf"^  would {move}\(within 20d\)$", output, re.MULTILINE), output

    env.dry_run = False
    code, output = env.run_api(datetime.now())

    assert code == 0, output
    assert re.search(rf"^  {move}\(within 20d, renamed\)$", output, re.MULTILINE), output


@pytest.mark.parametrize("taken", ["file", "empty-directory"])
def test_relocate_refuses_a_final_name_that_exists_and_says_where_to_read(tmp_path, taken):
    source = tmp_path / "a"
    source.write_text("unit")
    final = tmp_path / "stage" / "a"
    final.parent.mkdir()
    final.write_text("taken") if taken == "file" else final.mkdir()  # os.rename replaces both

    with pytest.raises(actions.Refusal) as refusal:
        actions.LocalTransport().relocate(source, final)

    assert refusal.value.reason == f"final name exists: {final}; {HINT}"
    assert source.read_text() == "unit"
    assert final.read_text() == "taken" if taken == "file" else final.is_dir()


def test_the_copy_path_refusal_of_a_final_name_says_where_to_read_too(tmp_path):
    temp, final = tmp_path / "temp", tmp_path / "final"
    temp.write_text("copy")
    final.write_text("taken")

    with pytest.raises(actions.Refusal) as refusal:
        actions.LocalTransport().rename(temp, final)

    assert refusal.value.reason == f"final name exists: {final}; {HINT}"


def test_relocate_makes_the_parent_renames_and_syncs_both_parents(tmp_path):
    source = tmp_path / "from" / "files" / "a"
    source.parent.mkdir(parents=True)
    source.write_text("unit")
    inode = source.stat().st_ino
    final = tmp_path / "to" / "files" / "a"  # neither "to" nor "to/files" exists
    synced = []

    with patch("bdbackup.gfs.actions._fsync_dir", side_effect=synced.append):
        actions.LocalTransport().relocate(source, final)

    assert final.exists() and not source.exists(), "the unit was not renamed"
    assert final.read_text() == "unit" and final.stat().st_ino == inode
    assert sorted(synced) == sorted([final.parent, source.parent])


def test_same_filesystem_compares_the_devices_of_two_live_paths(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    transport = actions.LocalTransport()
    real = os.stat

    def on_another_device(path, *args, **kwargs):
        fields = list(real(path, *args, **kwargs))
        if Path(path) == b:
            fields[2] += 1  # st_dev
        return os.stat_result(fields)

    assert transport.same_filesystem(a, b) is True
    assert transport.same_filesystem(a, tmp_path / "missing") is False
    with patch("bdbackup.gfs.actions.os.stat", on_another_device):
        assert transport.same_filesystem(a, b) is False


# ---- the run report names a stage whose checksums are not re-read (0.7.4, D1) ----------------


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_a_verify_false_stage_says_its_checksums_are_not_re_read(env, dry_run):
    env.write([(["BACKUP"], "daily", "5d"), (["NFS/daily"], "daily", "20d", NO_VERIFY),
               (["NFS/weekly"], "weekly", "8w")])
    size_a = env.file_unit("a", days_ago=7).stat().st_size
    size_b = env.file_unit("b", days_ago=0).stat().st_size
    env.dry_run = dry_run

    result = env.run_cli()

    assert result.exit_code == 0, result.output
    after = result.output.split("Summary:", 1)[1].splitlines()[1:]
    assert [line for line in after if line.startswith("Stage ")] == [
        f"Stage {env.abs('BACKUP')}: 1 unit ({size_b} bytes)",
        f"Stage {env.abs('NFS/daily')}: 1 unit ({size_a} bytes), checksums not re-read",
        f"Stage {env.abs('NFS/weekly')}: 0 units (0 bytes)",
    ], result.output


# ---- the facts of a run: reason classes and move methods (spec 16 section 6.1 and 6.2) --------


class _UnitCopyFails(_CopyOnly):
    """The copy of a unit fails; the ledger snapshot's copy does not."""

    def copy(self, source, temp, on_file):
        if Path(source).suffix == ".snapshot":
            return super().copy(source, temp, on_file)
        raise OSError("injected copy failure")


class _UnitCopyRefused(_CopyOnly):
    def copy(self, source, temp, on_file):
        if Path(source).suffix == ".snapshot":
            return super().copy(source, temp, on_file)
        raise actions.Refusal("a refusal of a kind the classes do not name")


def facts_of(env, now=None, **kwargs):
    """The facts a run leaves for ``last_runs``, taken from its outcome."""
    outcome = runner.RunOutcome()
    code, output = env.run_api(now or datetime.now(), outcome=outcome, **kwargs)
    assert outcome.facts is not None, output
    return outcome.facts


def reasons(env, now=None, **kwargs):
    """``(stage, outcome, reason, units)`` of each reason class the run counted."""
    return [(r["stage"], r["outcome"], r["reason"], r["units"])
            for r in facts_of(env, now, **kwargs)["reasons"]]


def record_then_change(env, change):
    """Unit a is recorded by a first run, in which it stays; ``change`` then touches it."""
    a = env.file_unit("a.tar.gz", day=date(2026, 9, 25))
    env.file_unit("b.tar.gz", day=date(2026, 9, 29))
    assert env.run_api(datetime(2026, 9, 29, 12))[0] == 0 and a.exists()
    change(a)
    return datetime(2026, 10, 2, 12)  # a is now due for NFS/daily


def test_a_run_with_nothing_to_say_counts_no_reason(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert facts_of(env)["reasons"] == []


def test_a_held_unit_is_period_incomplete(env):
    env.write([(["BACKUP"], "daily", "2d"), (["NFS/weekly"], "weekly", "8w")])
    for day in [date(2026, 9, 26) + timedelta(n) for n in range(5)]:
        env.file_unit(f"{day}.tar.gz", day=day)

    # 09-28 belongs to the week 2026-W40, which is not over on 09-30
    assert reasons(env, datetime(2026, 9, 30, 12)) == [(1, "held", "period_incomplete", 1)]


def test_the_units_of_a_job_still_backing_up_are_deferred_as_backup_running(env):
    defer_a_unit(env)

    assert reasons(env) == [(1, "deferred", "backup_running", 2)]  # a and b, both in stage 1


def test_a_unit_whose_engine_lock_is_busy_is_deferred_as_locked(env):
    env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)

    with process_lock(env.abs("BACKUP/mysql/prod") / ".bdbackup.lock"):
        found = reasons(env)

    assert found == [(1, "deferred", "locked", 1)]


def test_a_changed_unit_is_refused_as_checksum_mismatch(env):
    now = record_then_change(env, lambda a: a.write_bytes(b"changed on disk"))

    assert reasons(env, now) == [(1, "refused", "checksum_mismatch", 1)]


def test_a_missing_unit_is_refused_as_missing_location(env):
    now = record_then_change(env, lambda a: a.unlink())

    assert reasons(env, now) == [(1, "refused", "missing_location", 1)]


def test_a_stray_entry_is_refused_as_unexpected_entry(env):
    unit = env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)
    full_name = next(p.name for p in unit.iterdir() if p.name.startswith("Full_2"))
    (unit / "Incremental" / full_name / "leftover.tmp").mkdir()

    assert reasons(env) == [(1, "refused", "unexpected_entry", 1)]


def test_a_unit_in_a_stage_that_is_not_configured_is_stage_0(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_api(datetime.now())[0] == 0
    with closing(sqlite3.connect(env.database)) as db, db:
        db.execute("UPDATE gfs_locations SET stage_path='/not/a/stage'"
                   " WHERE path LIKE '%NFS/daily%'")

    assert reasons(env) == [(0, "refused", "stage_not_configured", 1)]


def test_a_destination_without_its_marker_is_destination_not_ready(env):
    (env.root / "NFS/daily/.bdbackup-destination").unlink()
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    # the same missing marker fails the ledger snapshot of that stage
    assert reasons(env) == [(1, "refused", "destination_not_ready", 1),
                            (2, "failed", "snapshot", 1)]


def test_a_taken_final_name_is_final_name_exists(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    final = env.root / "NFS/daily/files/a.tar.gz"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"in the way")

    assert reasons(env) == [(1, "refused", "final_name_exists", 1)]


def test_a_refusal_of_no_named_kind_is_other(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert reasons(env, transport=_UnitCopyRefused()) == [(1, "refused", "other", 1)]


def test_a_failed_move_is_error(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert reasons(env, transport=_UnitCopyFails()) == [(1, "failed", "error", 1)]


def test_a_failed_ledger_snapshot_is_counted_in_the_stage_it_was_written_to(env):
    (env.root / "NFS/weekly/.bdbackup-destination").unlink()
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert reasons(env) == [(3, "failed", "snapshot", 1)]


def test_units_of_one_class_in_one_stage_are_counted_together(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("c.tar.gz", days_ago=8)
    env.file_unit("b.tar.gz", days_ago=0)
    with closing(sqlite3.connect(env.database)) as db, db:  # a job still backing up: both defer
        db.execute(
            "INSERT INTO backup_runs (job_name, backup_type, started_at, status, restore_info)"
            " VALUES ('files', 'file', ?, 'running', '{}')", (datetime.now(UTC).isoformat(),),
        )

    assert reasons(env) == [(1, "deferred", "backup_running", 3)]


def test_moves_by_rename_and_by_copy(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert facts_of(env)["moves"] == {"rename": 1, "copy": 0}


def test_a_move_by_copy_is_counted_as_a_copy(env, copy_path):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert facts_of(env)["moves"] == {"rename": 0, "copy": 1}


def test_a_move_onto_two_paths_is_one_copy(env):
    env.write(TWO_DAILY_PATHS)
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert facts_of(env)["moves"] == {"rename": 0, "copy": 1}


def test_a_run_computes_its_facts_at_its_end(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    before = datetime.now(UTC)

    facts = facts_of(env)

    computed = datetime.fromisoformat(facts["computed_at"])
    assert computed.utcoffset() == timedelta(0)
    assert before <= computed <= datetime.now(UTC)
    assert set(facts) == {"computed_at", "reasons", "moves", "drift"}


def test_a_dry_run_computes_no_facts(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    env.dry_run = True
    outcome = runner.RunOutcome()

    code, output = env.run_api(datetime.now(), outcome=outcome)

    assert code == 0, output
    assert outcome.facts is None


def test_a_finished_run_records_its_facts_in_last_runs(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    assert env.run_cli().exit_code == 0

    facts = last_run(env).facts
    assert set(facts) == {"computed_at", "reasons", "moves", "drift"}
    assert facts["moves"] == {"rename": 1, "copy": 0}


def finished_run(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    facts = last_run(env).facts
    assert facts is not None, "the run left no facts to keep"
    return facts


def test_a_locked_run_keeps_the_previous_facts(env):
    before = finished_run(env)

    with process_lock(runner.job_lock_path(Config(env.config_path), "gfs-main")):
        result = env.run_cli()

    assert result.exit_code == 3, result.output
    row = last_run(env)
    assert (row.result, row.facts) == ("locked", before)


def test_a_run_that_raised_keeps_the_previous_facts(env):
    before = finished_run(env)

    with patch("bdbackup.gfs.runner.decide", side_effect=RuntimeError("injected")):
        result = env.run_cli()

    assert isinstance(result.exception, RuntimeError), result.output
    row = last_run(env)
    assert (row.result, row.facts) == ("error", before)


def test_a_dry_run_leaves_the_previous_facts(env):
    before = finished_run(env)
    env.dry_run = True

    assert env.run_cli().exit_code == 0

    assert last_run(env).facts == before
