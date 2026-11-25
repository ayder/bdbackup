"""PBM jobs through bdbackup run and the real history ledger."""

import hashlib
import json
import sqlite3
import subprocess
import tarfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup import tar_export
from bdbackup.backends import BackupError
from bdbackup.cli import main
from bdbackup.config import Config
from bdbackup.gfs import runner, store
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History
from bdbackup.ssh_copy import safe_artifact_name
from bdbackup.utils import process_lock
from tests.pbm_fake import NAME, make_storage
from tests.ssh_copy_fake import Remote

JOB = "mongo-main"
NEXT = "2026-11-26T13:24:55Z"
REPLICAS = [
    {"name": "rs0", "node": "secondary.example:27017"},
    {"name": "rs1", "node": "member.example:27017"},
]
URI = "mongodb://fixture-user:fixture-password@localhost/?authSource=admin&directConnection=true"


@dataclass
class Site:
    root: Path
    monkeypatch: pytest.MonkeyPatch
    job: str = JOB
    out_dir: str = "archives/mongo"
    backup_type: str = "logical"
    name: str = NAME
    reported_status: str = "done"
    reported_type: str | None = None
    backup_error: tuple[str, str] | None = None
    describe_error: bool = False
    calls: list[list[str]] = field(default_factory=list)

    @property
    def config(self):
        return self.root / "config.toml"

    @property
    def history(self):
        return History(Config(self.config).history)

    @property
    def output(self):
        return self.root / self.out_dir

    @property
    def storage(self):
        return self.root / "storage"

    def unit(self, name=NAME):
        return self.output / f"{self.job}-{name.replace(':', '')}.tar"

    def write(self, extra=""):
        kind = "" if self.backup_type == "logical" else f'backup_type="{self.backup_type}"\n'
        self.config.write_text(
            '[history]\ndatabase="state/history.sqlite3"\n'
            f'["{self.job}"]\ntype="pbm"\nstorage_path="storage"\nout_dir="{self.out_dir}"\n'
            f"{kind}{extra}"
        )

    def pbm(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == "backup":
            if self.backup_error:
                stdout, stderr = self.backup_error
                return subprocess.CompletedProcess(argv, 1, stdout, stderr)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"name": self.name}), "")
        assert argv[1] == "describe-backup", f"unexpected PBM command {argv[1]}"
        if self.describe_error:
            return subprocess.CompletedProcess(argv, 1, "", "Error: no backup metadata")
        payload = {
            "type": self.reported_type or self.backup_type,
            "status": self.reported_status,
            "replsets": REPLICAS,
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    def run(self, *flags, job=None):
        return invoke("run", "-c", self.config, "-j", job or self.job, *flags)


def invoke(*args):
    return CliRunner().invoke(main, list(map(str, args)))


@pytest.fixture
def site(tmp_path, monkeypatch):
    site = Site(tmp_path.resolve(), monkeypatch)
    site.write()
    make_storage(site.storage)
    monkeypatch.setattr(subprocess, "run", site.pbm)
    return site


@pytest.mark.parametrize("kind", ["logical", "physical"])
def test_pbm_run_publishes_the_new_archive_and_records_its_restore_facts(site, kind):
    site.backup_type = kind
    site.write()
    make_storage(site.storage, NEXT, b"older backup content")
    result = site.run()
    assert result.exit_code == 0, result.output
    assert result.stdout == f"Job {site.job}: {site.unit()}\n"
    assert site.calls == [
        ["pbm", "backup", "--type", kind, "--wait", "--out", "json"],
        ["pbm", "describe-backup", NAME, "--out", "json"],
    ]
    [record] = site.history.records()
    assert record.backup_type == f"pbm-{kind}" and record.status == "success"
    assert record.path == record.unit == str(site.unit())
    assert record.size_bytes == site.unit().stat().st_size
    assert record.checksum == hashlib.sha256(site.unit().read_bytes()).hexdigest()
    assert json.loads(record.restore_info) == {
        "kind": "pbm", "name": NAME, "backup_type": kind, "replsets": REPLICAS,
    }
    assert not site.history.last_runs()
    assert list(site.output.iterdir()) == [site.unit()]
    assert (site.storage / f"{NEXT}.pbm.json").is_file()


def assert_failed(site, result, diagnostic, caplog):
    assert result.exit_code == 1, result.output
    assert diagnostic in result.output + caplog.text
    [record] = site.history.records()
    assert record.status == "failed" and record.path is None
    assert record.unit is None and record.checksum is None
    assert not list(site.output.glob("*.tar"))
    return record


@pytest.mark.parametrize("shape", ["plain", "multiline", "json"])
def test_pbm_run_reports_the_diagnostic_without_leaking_uri(site, caplog, shape):
    site.monkeypatch.setenv("PBM_MONGODB_URI", URI)
    expected = "cannot connect via <redacted>"
    error = f"cannot connect via {URI}"
    site.backup_error = {
        "plain": ("", f"Error: {error}"),
        "multiline": ("", f"Earlier error\n{error}\n\n"),
        "json": (json.dumps({"Error": error}).replace("&", "\\u0026"), ""),
    }[shape]
    prefix = "Error: " if shape == "plain" else ""
    record = assert_failed(site, site.run(), f"pbm backup failed: {prefix}{expected}", caplog)
    assert "Earlier error" not in caplog.text
    assert '{"Error":' not in caplog.text
    assert URI not in caplog.text
    assert URI not in record.restore_info
    assert "fixture-password" not in site.history.settings.database.read_bytes().decode(
        errors="ignore",
    )
    assert [call[1] for call in site.calls] == ["backup"]


@pytest.mark.parametrize("failure", ["unfinished", "wrong-type", "missing-files", "describe"])
def test_pbm_run_fails_before_publication_when_it_cannot_confirm_or_find_backup(
    site, failure, caplog,
):
    if failure == "unfinished":
        site.reported_status = "running"
        diagnostic = "running"
    elif failure == "wrong-type":
        site.reported_type = "physical"
        diagnostic = "physical"
    elif failure == "missing-files":
        (site.storage / f"{NAME}.pbm.json").unlink()
        diagnostic = f"PBM took backup {NAME} on {REPLICAS[0]['node']}"
    else:
        site.describe_error = True
        diagnostic = "pbm describe-backup failed: Error: no backup metadata"
    assert_failed(site, site.run(), diagnostic, caplog)
    assert [call[1] for call in site.calls] == ["backup", "describe-backup"]


def test_pbm_run_retries_by_taking_a_new_backup_after_a_failed_export(site, caplog):
    def fail(*args, **kwargs):
        raise BackupError("read-back failed")

    with site.monkeypatch.context() as patch:
        patch.setattr(tar_export, "read_back", fail)
        assert_failed(site, site.run(), "read-back failed", caplog)
    failed = site.history.records()[0]
    site.name = NEXT
    make_storage(site.storage, NEXT, b"new content")
    result = site.run()
    assert result.exit_code == 0, result.output
    assert [call[1] for call in site.calls] == ["backup", "describe-backup"] * 2
    assert [call[2] for call in site.calls if call[1] == "describe-backup"] == [NAME, NEXT]
    assert (site.storage / f"{NAME}.pbm.json").is_file()
    assert list(site.output.iterdir()) == [site.unit(NEXT)]
    assert site.history.records()[1] == failed
    with tarfile.open(site.unit(NEXT)) as archive:
        assert all(member.name.startswith(NEXT) for member in archive)


def test_pbm_run_no_verify_omits_read_back(site):
    site.monkeypatch.setattr(tar_export, "read_back", lambda *args: pytest.fail("read-back ran"))
    result = site.run("--no-verify")
    assert result.exit_code == 0, result.output
    assert site.unit().is_file()


def test_pbm_run_busy_lock_exits_three_without_calling_pbm_or_recording(site):
    database = site.history.settings.database
    with process_lock(Path(f"{database}.pbm-{site.job}.lock")):
        result = site.run()
    assert result.exit_code == 3, result.output
    assert site.calls == []
    assert not database.exists()


def test_configuration_without_pbm_never_starts_it(site):
    (site.root / "source").mkdir()
    (site.root / "source/item").write_text("file backup")
    (site.root / "paths").write_text("item\n")
    site.config.write_text(
        '[history]\ndatabase="state/history.sqlite3"\n'
        '[local]\ntype="file"\ntemplate_filename="paths"\nchdir="source"\n'
        'backup_dst="archives/files"\nformat="tar.gz"\n'
    )
    result = site.run(job="local")
    assert result.exit_code == 0, result.output
    assert site.calls == []
    [record] = site.history.records()
    assert (record.backup_type, record.status) == ("file", "success")


GFS = """
[rotation]
type = "gfs"
[[rotation.stage]]
paths = ["daily"]
period = "daily"
keep = "5d"
[[rotation.stage]]
paths = ["later"]
period = "daily"
keep = "20d"
"""


@pytest.mark.parametrize("method,job", [
    ("rsync", "Mongo01._-+=@,%~"),
    ("scp", "-"),
    ("scp", ".."),
])
def test_pbm_copy_publishes_a_safe_unit_and_lists_its_delivery(site, method, job):
    site.job = job
    site.write(f'["{job}".copy]\ndestination="ssh://vault/pbm"\nmethod="{method}"\n')
    peer = Remote()

    def check_published(argv):
        database = site.history.settings.database
        with process_lock(Path(f"{database}.pbm-{job}.lock")):
            [record] = site.history.records()
            assert record.status == "success"

    peer.before = check_published

    def run(argv, **kwargs):
        return site.pbm(argv, **kwargs) if argv[0] == "pbm" else peer(argv, **kwargs)

    site.monkeypatch.setattr(subprocess, "run", run)
    result = site.run("--no-verify")
    assert result.exit_code == 0, result.output
    unit = site.unit()
    assert safe_artifact_name(unit.name) and unit.name not in {".", ".."} and ":" not in unit.name
    assert f"Copy {job}: {unit.name} -> ssh://vault/pbm ({method}," in result.stdout
    assert [(tool, path) for tool, path, _ in peer.transfers] == [(method, unit)]
    assert peer.files == {f"/pbm/{unit.name}": unit.read_bytes()}
    [delivery] = site.history.deliveries()
    assert (delivery.method, delivery.status) == (method, "copied")
    listing = invoke("history", "--config", site.config, "--job", job)
    assert listing.exit_code == 0 and " | copy copied" in listing.stdout


def test_pbm_gfs_rotates_and_expires_the_archive_like_a_file_unit(site):
    site.out_dir = "daily/mongo"
    site.write(GFS)
    init = site.run("--initialize-structure", job="rotation")
    assert init.exit_code == 0, init.output
    assert not (site.root / "daily" / MARKER).exists()
    assert (site.root / "later" / MARKER).is_file()

    first = site.run()
    assert first.exit_code == 0, first.output
    older, = site.history.records()
    old_path = site.unit()
    original = old_path.read_bytes()
    site.name = NEXT
    make_storage(site.storage, NEXT)
    second = site.run()
    assert second.exit_code == 0, second.output
    clock = datetime.now().astimezone()
    with closing(sqlite3.connect(site.history.settings.database)) as db, db:
        db.execute("UPDATE backup_runs SET completed_at=? WHERE id=?",
                   ((clock - timedelta(days=8)).isoformat(), older.id))

    config = Config(site.config)
    job = config.get("rotation")
    lines = []
    assert runner.run_job(config, job, now=clock, out=lines.append) == 0, lines
    moved = site.root / "later" / "mongo" / old_path.name
    assert moved.read_bytes() == original and not old_path.exists()
    assert any(f"move {site.job} mongo/{old_path.name}" in line for line in lines)
    assert site.unit(NEXT).is_file()

    lines.clear()
    assert runner.run_job(config, job, now=clock + timedelta(days=31), out=lines.append) == 0, lines
    assert not moved.exists()
    assert any(f"delete {site.job} mongo/{old_path.name}" in line for line in lines)
    assert store.places(site.history)[older.id] == ()
    assert site.unit(NEXT).is_file()


def test_pbm_out_dir_in_a_later_gfs_stage_is_a_configuration_error(site):
    site.out_dir = "later/mongo"
    site.write(GFS)
    result = site.run()
    assert result.exit_code == 2, result.output
    assert "Job 'mongo-main' writes into stage path" in result.output
    assert "engines write only into the first stage" in result.output
    assert not site.calls
