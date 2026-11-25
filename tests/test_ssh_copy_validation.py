"""SSH copy preflight witnesses at the CLI and subprocess boundaries."""

import os
import shutil
import sqlite3
import subprocess
from contextlib import closing

import pytest
from click.testing import CliRunner

from bdbackup import validation
from bdbackup.backends import BackupResult
from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.engines import _reset_for_tests
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History
from tests.pgbackrest_fake import fake_pgbackrest, make_repo

DEST = "ssh://backup@vault:2222/remote/archive%20"
REMOTE_PATH = "/remote/archive%20"
FILE = (
    '[backup]\ntype="file"\nbackup_dst="archives/daily"\n'
    'template_filename="paths"\nchdir="input"\ntimestamp=true\n'
)
MYSQL = '[backup]\ntype="mysqldump"\ndatabase="app"\nout_dir="archives"\n'
PG = (
    '[gfs]\ntype="gfs"\n[[gfs.stage]]\npaths=["first"]\nperiod="daily"\nkeep="5d"\n'
    '[backup]\ntype="pgbackrest"\nstanza="main"\nrepo_path="repo"\nout_dir="first/pg"\n'
)
READY = {"destination", "sha256sum", "stat", "rsync"}
MYSQL_GRANTS = "root@localhost\t8.4.0\t/data\nGRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost'\n"


class Probe:
    def __init__(self):
        self.calls = []
        self.ready = set(READY)
        self.missing = set()
        self.disconnect = False
        self.pg = None

    def which(self, name):
        return None if name in self.missing else f"/fixture/bin/{name}"

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        name = argv[0].rsplit("/", maxsplit=1)[-1]
        if name == "ssh":
            if self.disconnect:
                return subprocess.CompletedProcess(argv, 255, "", "synthetic host key refusal")
            return subprocess.CompletedProcess(argv, 0, "\n".join(sorted(self.ready)), "")
        if name == "mysql":
            return subprocess.CompletedProcess(argv, 0, MYSQL_GRANTS, "")
        if name == "pgbackrest":
            return self.pg(argv, **kwargs)
        raise AssertionError(f"unexpected subprocess: {argv}")

    @property
    def ssh(self):
        return [argv for argv in self.calls if argv[0] == "ssh"]


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    fixture = Probe()
    monkeypatch.setattr(shutil, "which", fixture.which)
    monkeypatch.setattr(subprocess, "run", fixture)
    yield fixture
    _reset_for_tests()


def configure(tmp_path, probe, *, kind="file", method="rsync", copy=True, inactive=False, cfg=True):
    (tmp_path / "input").mkdir()
    (tmp_path / "input/data").write_text("data")
    (tmp_path / "paths").write_text("data\n")
    config = '[history]\ndatabase="state/history.db"\n'
    config += {"file": FILE, "mysqldump": MYSQL, "pgbackrest": PG}[kind]
    if kind == "pgbackrest":
        (tmp_path / "first").mkdir()
        (tmp_path / "first" / MARKER).touch()
        probe.pg = fake_pgbackrest(make_repo(tmp_path))
    if inactive:
        config += "active=false\n"
    if copy:
        config += f'[backup.copy]\ndestination="{DEST}"\nmethod="{method}"\n'
        if cfg:
            (tmp_path / "ssh files").mkdir()
            (tmp_path / "ssh files/fixture.conf").write_text("# fixture\n")
            config += 'ssh_config="ssh files/fixture.conf"\n'
    path = tmp_path / "config.toml"
    path.write_text(config)
    return path


def invoke(path, *, select=True):
    args = ["-j", "backup"] if select else []
    return CliRunner().invoke(main, ["run", "--config", str(path), "--validate", *args])


def check_line(result, status, label, remedy=""):
    prefix = f"[{status}] SSH copy {label}"
    lines = [line for line in result.stdout.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, f"expected one {prefix!r} line\n{result.output}"
    assert remedy in lines[0]


@pytest.mark.parametrize("kind,method", [
    ("file", "scp"), ("mysqldump", "rsync"), ("pgbackrest", "scp"),
])
def test_copy_validation_reports_all_read_only_checks(tmp_path, probe, kind, method):
    path = configure(tmp_path, probe, kind=kind, method=method)
    before = set(tmp_path.rglob("*"))

    result = invoke(path)

    assert result.exit_code == 0, result.output
    for label in ("local ssh", f"local {method}", "ssh_config", "connection", "destination",
                  "remote sha256sum", "remote stat -c %a"):
        check_line(result, "OK", label)
    if method == "rsync":
        check_line(result, "OK", "remote rsync")
    else:
        assert "SSH copy remote rsync" not in result.stdout
    assert set(tmp_path.rglob("*")) == before
    argv, = probe.ssh
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
    assert argv[argv.index("-F") + 1] == str(tmp_path / "ssh files/fixture.conf")
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-l") + 1] == "backup"
    assert argv[-3:-1] == ["--", "vault"]
    command = argv[-1]
    assert f"test -d {REMOTE_PATH} && test -w {REMOTE_PATH}" in command
    assert f"stat -c %a -- {REMOTE_PATH}" in command
    assert "command -v sha256sum" in command
    assert ("command -v rsync" in command) == (method == "rsync")
    assert not any(word in command.split() for word in (
        "mkdir", "rm", "ln", "mv", "touch", "chmod", "scp",
    ))


@pytest.mark.parametrize("tool,remedy,connects", [
    ("ssh", "Install OpenSSH", False),
    ("rsync", "Install rsync", True),
    ("scp", "Install OpenSSH", True),
])
def test_missing_copy_binary_has_a_remedy(tmp_path, probe, tool, remedy, connects):
    path = configure(tmp_path, probe, method=tool if tool != "ssh" else "rsync")
    probe.missing.add(tool)

    result = invoke(path)

    assert result.exit_code == 1, "unavailable local copy tooling must fail validation"
    check_line(result, "FAIL", f"local {tool}", remedy)
    assert bool(probe.ssh) == connects
    if not connects:
        check_line(result, "FAIL", "connection", "Install ssh")
        check_line(result, "FAIL", "remote sha256sum", "Restore the SSH connection")


def test_unreadable_copy_config_blocks_ssh_and_reports_remedy(tmp_path, probe, monkeypatch):
    path = configure(tmp_path, probe)
    access = validation._access

    def unreadable(path, mode):
        if path.name == "fixture.conf":
            assert mode == os.R_OK
            return False
        return access(path, mode)

    monkeypatch.setattr(validation, "_access", unreadable)
    result = invoke(path)

    assert result.exit_code == 1, "the configured SSH file must be readable"
    check_line(result, "FAIL", "ssh_config", "readable")
    check_line(result, "FAIL", "connection", "configured SSH file readable")
    assert probe.ssh == []


def test_connection_failure_keeps_other_checks_and_gives_remedies(tmp_path, probe):
    path = configure(tmp_path, probe, cfg=False)
    probe.disconnect = True

    result = invoke(path)

    assert result.exit_code == 1, "SSH connection refusal must fail validation"
    check_line(result, "OK", "local ssh")
    check_line(result, "OK", "local rsync")
    check_line(result, "FAIL", "connection", "known host key")
    for label in ("destination", "remote sha256sum", "remote stat -c %a", "remote rsync"):
        check_line(result, "FAIL", label, "Restore the SSH connection")
    assert len(probe.ssh) == 1
    assert "-F" not in probe.ssh[0]


@pytest.mark.parametrize("token,label,remedy", [
    ("destination", "destination", "Create the directory"),
    ("sha256sum", "remote sha256sum", "Install GNU coreutils or BusyBox"),
    ("stat", "remote stat -c %a", "Install GNU coreutils or BusyBox"),
    ("rsync", "remote rsync", "Install rsync on the SSH server"),
])
def test_remote_check_failure_has_a_specific_remedy(tmp_path, probe, token, label, remedy):
    path = configure(tmp_path, probe)
    probe.ready.remove(token)

    result = invoke(path)

    assert result.exit_code == 1, "a failed remote readiness check must fail validation"
    check_line(result, "OK", "connection")
    check_line(result, "FAIL", label, remedy)


def test_copy_validation_never_migrates_an_existing_database(tmp_path, probe):
    path = configure(tmp_path, probe)
    history = History(Config(path).history)
    artifact = tmp_path / "old.tar"
    artifact.write_bytes(b"old")
    history.run("backup", "file", lambda: BackupResult(artifact, 3, True))
    with closing(sqlite3.connect(history.settings.database)) as db, db:
        db.execute("DROP TABLE deliveries")
        db.execute("PRAGMA user_version = 6")
    before = history.settings.database.read_bytes()

    result = invoke(path)

    assert result.exit_code == 0, result.output
    check_line(result, "OK", "connection")
    assert history.settings.database.read_bytes() == before


@pytest.mark.parametrize("copy,inactive", [(False, False), (True, True)])
def test_copy_validation_never_connects_for_a_copyless_or_inactive_job(
    tmp_path, probe, copy, inactive,
):
    path = configure(tmp_path, probe, copy=copy, inactive=inactive)

    result = invoke(path, select=False)

    assert result.exit_code == 0, result.output
    assert "SSH copy" not in result.stdout and probe.ssh == []
