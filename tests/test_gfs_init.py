"""`run -j <gfs job> --initialize-structure`: stage directories and destination markers."""

import os
import shutil
import tomllib
from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

import bdbackup
from bdbackup.cli import main
from bdbackup.validation import os_user
from tests.test_gfs_run import Env

MARKER = ".bdbackup-destination"
REMINDER = ("Run --initialize-structure only while every destination's filesystem is mounted, "
            "and as the OS user that runs GFS.")
STAGES = [("BACKUP", "daily", "5d"), ("NFS/daily", "daily", "20d"),
          ("NFS/weekly", "weekly", "8w")]


def invoke(*args):
    return CliRunner().invoke(main, list(map(str, args)))


class Layout:
    def __init__(self, root, stages=STAGES, job="", extra=""):
        self.root = root.resolve()
        (self.root / "NFS").mkdir()
        text = f'[history]\ndatabase="state/h.sqlite3"\n{extra}[gfs-main]\ntype="gfs"\n{job}\n'
        for path, period, keep in stages:
            text += (f'[[gfs-main.stage]]\npaths = ["{path}"]\nperiod = "{period}"\n'
                     f'keep = "{keep}"\n')
        self.config = self.root / "config.toml"
        self.config.write_text(text)

    def path(self, relative):
        return self.root / relative

    def initialize(self, *extra):
        return invoke("run", "-c", self.config, "-j", "gfs-main", "--initialize-structure",
                      *extra)

    def marker(self, relative):
        return self.path(relative) / MARKER


@pytest.fixture
def layout(tmp_path):
    return Layout(tmp_path)


def non_root():
    assert os.geteuid() != 0, "permission tests need a non-root user"


def test_initialize_creates_stages_and_markers(layout):
    result = layout.initialize()
    lines = result.output.splitlines()
    assert f"{layout.path('BACKUP')}: created" in lines, result.output
    assert f"{layout.path('NFS/daily')}: created, marker written" in lines, result.output
    assert f"{layout.path('NFS/weekly')}: created, marker written" in lines, result.output
    assert REMINDER in lines, result.output
    assert result.exit_code == 0, result.output
    assert not layout.marker("BACKUP").exists()
    for relative, stage in (("NFS/daily", 2), ("NFS/weekly", 3)):
        content = tomllib.loads(layout.marker(relative).read_text())
        assert content["bdbackup_version"] == bdbackup.__version__
        assert content["gfs_job"] == "gfs-main"
        assert content["stage"] == stage
        written = datetime.fromisoformat(content["written_at"])
        assert written.utcoffset() == UTC.utcoffset(None)
    assert not layout.path("state/h.sqlite3").exists()
    assert not layout.path("state").exists()


def test_initialize_twice_rewrites_marker_in_place(layout):
    first = layout.initialize()
    assert f"{layout.path('NFS/daily')}: created, marker written" in first.output, first.output
    marker = layout.marker("NFS/daily")
    marker.write_text("old")
    inode = marker.stat().st_ino
    result = layout.initialize()
    lines = result.output.splitlines()
    assert result.exit_code == 0, result.output
    assert f"{layout.path('BACKUP')}: exists" in lines, result.output
    assert f"{layout.path('NFS/daily')}: exists, marker written" in lines, result.output
    assert marker.stat().st_ino == inode
    assert tomllib.loads(marker.read_text())["gfs_job"] == "gfs-main"


def test_initialize_missing_parent(tmp_path):
    layout = Layout(tmp_path, stages=[*STAGES[:2], ("GONE/weekly", "weekly", "8w")])
    result = layout.initialize()
    assert result.exit_code == 1, result.output
    assert (f"FAIL {layout.path('GONE/weekly')}: parent does not exist: {layout.path('GONE')}"
            in result.output.splitlines()), result.output
    assert not layout.path("GONE").exists()
    assert layout.marker("NFS/daily").is_file()


@pytest.mark.parametrize("target", ["directory", "marker"])
def test_initialize_not_writable(layout, target):
    non_root()
    first = layout.initialize()
    assert f"{layout.path('NFS/daily')}: created, marker written" in first.output, first.output
    marker = layout.marker("NFS/daily")
    before = marker.read_text()
    locked = layout.path("NFS/daily") if target == "directory" else marker
    mode = locked.stat().st_mode
    locked.chmod(0o555 if target == "directory" else 0o444)
    try:
        result = layout.initialize()
    finally:
        locked.chmod(mode)
    lines = result.output.splitlines()
    assert result.exit_code == 1, result.output
    if target == "directory":
        assert (f"FAIL {layout.path('NFS/daily')}: not writable by OS user {os_user()}"
                in lines), result.output
    else:
        failed = [line for line in lines if line.startswith(f"FAIL {layout.path('NFS/daily')}: ")]
        assert len(failed) == 1 and "Permission denied" in failed[0], result.output
    assert marker.read_text() == before
    assert f"{layout.path('NFS/weekly')}: exists, marker written" in lines, result.output


def test_initialize_path_is_a_file(layout):
    layout.path("NFS/weekly").write_text("not a stage")
    result = layout.initialize()
    assert result.exit_code == 1, result.output
    assert f"FAIL {layout.path('NFS/weekly')}: not a directory" in result.output.splitlines()
    assert layout.path("NFS/weekly").read_text() == "not a stage"


FILE_JOB = ('[files]\ntype="file"\ntemplate_filename="paths"\nchdir="src"\n'
            'backup_dst="out/files"\n')
USAGE = {
    "no-job": ((), "--initialize-structure requires --job NAME"),
    "unknown-job": (("-j", "nope"), "Job 'nope' not found"),
    "not-gfs": (("-j", "files"), "--initialize-structure applies only to gfs jobs: files"),
    "inactive": (("-j", "gfs-main"), "Job 'gfs-main' is inactive (active = false)"),
    "full": (("-j", "gfs-main", "--full"),
             "--initialize-structure cannot be combined with --full"),
    "incremental": (("-j", "gfs-main", "--incremental"),
                    "--initialize-structure cannot be combined with --incremental"),
    "verify": (("-j", "gfs-main", "--verify"),
               "--initialize-structure cannot be combined with --verify"),
    "no-verify": (("-j", "gfs-main", "--no-verify"),
                  "--initialize-structure cannot be combined with --no-verify"),
    "dry-run": (("-j", "gfs-main", "--dry-run"),
                "--initialize-structure cannot be combined with --dry-run"),
    "validate": (("-j", "gfs-main", "--validate"),
                 "--initialize-structure cannot be combined with --validate"),
    "bad-config": (("-j", "gfs-main"), "a gfs job accepts only type, active and stage"),
}


@pytest.mark.parametrize("case", list(USAGE))
def test_initialize_usage_errors(tmp_path, case):
    job = {"inactive": "active = false", "bad-config": 'restore_root = "r"'}.get(case, "")
    layout = Layout(tmp_path, job=job, extra=FILE_JOB if case == "not-gfs" else "")
    args, message = USAGE[case]
    result = invoke("run", "-c", layout.config, "--initialize-structure", *args)
    assert result.exit_code == 2, result.output
    assert message in " ".join(result.output.split()), result.output
    for relative in ("BACKUP", "NFS/daily", "NFS/weekly", "state", "out"):
        assert not layout.path(relative).exists(), relative


def test_initialized_structure_validates_and_receives_units(tmp_path):
    env = Env(tmp_path)
    shutil.rmtree(tmp_path / "NFS")
    (tmp_path / "NFS").mkdir()
    shutil.rmtree(tmp_path / "BACKUP")
    prepared = invoke("run", "-c", env.config_path, "-j", "gfs-main", "--initialize-structure")
    assert prepared.exit_code == 0, prepared.output
    validated = invoke("run", "-c", env.config_path, "--validate")
    assert validated.exit_code == 0, validated.output
    assert (f"[OK] Stage path {env.abs('NFS/daily')}: writable, marker present and writable"
            in validated.output), validated.output
    a = env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    result = env.run_cli()
    assert result.exit_code == 0, result.output
    assert not a.exists()
    assert (env.abs("NFS/daily") / "files" / "a.tar.gz").is_file()
