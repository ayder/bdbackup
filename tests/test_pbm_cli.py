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


@pytest.mark.parametrize("kind", ["logical", "physical"])
@pytest.mark.parametrize("mode", ["history", "archive"])
def test_pbm_restore_extracts_the_unit_and_prints_the_operator_steps(site, kind, mode):
    site.backup_type = kind
    site.write()
    metadata = site.storage / f"{NAME}.pbm.json"
    metadata.write_text(json.dumps({"name": NAME, "type": kind, "status": "done"}))
    run = site.run()
    assert run.exit_code == 0, run.output
    record, = site.history.records()
    listing = invoke("history", "-c", site.config, "--job", site.job)
    assert listing.exit_code == 0, listing.output
    assert f"{record.id}: {site.job} | pbm-{kind} |" in listing.stdout
    assert " | success | available | " in listing.stdout

    destination = site.root / "restored"
    if mode == "history":
        args = ("-c", site.config, "--backup-id", record.id, "--yes")
    else:
        renamed = site.root / "unrelated-filename.tar"
        site.unit().rename(renamed)
        args = ("--archive", renamed)

    def unexpected(*args, **kwargs):
        pytest.fail("recovering a PBM archive must not run a command")

    site.monkeypatch.setattr(subprocess, "run", unexpected)
    result = invoke("restore", *args, "--dst", destination)
    assert result.exit_code == 0, result.output
    assert {p.name for p in destination.iterdir()} == {NAME, metadata.name}
    for file in site.storage.rglob("*"):
        if file.is_file():
            assert (destination / file.relative_to(site.storage)).read_bytes() == file.read_bytes()
    lines = result.stdout.splitlines()
    assert f"To restore PBM {kind} backup {NAME}, follow these steps." in lines
    steps = [line for line in lines if line[:3] in {"1. ", "2. ", "3. ", "4. "}]
    assert [line[:3] for line in steps] == ["1. ", "2. ", "3. ", "4. "]
    assert f"{NAME}.pbm.json" in steps[0] and f"{NAME}/" in steps[0]
    assert "same path on every node" in steps[0]
    assert "configured to use it" in steps[0]
    assert "The restore fails until every node can read both entries." in result.stdout
    commands = [line.strip() for line in lines if line.strip().startswith("pbm ")]
    expected_commands = [
        "pbm config --force-resync --wait", "pbm list", f"pbm restore {NAME}",
    ]
    if kind == "physical":
        expected_commands.append("pbm config --force-resync")
    assert commands == expected_commands
    assert ('If pbm restore answers "operation staled" just after resync, '
            'wait a few seconds and run it again.') in result.stdout
    assert NAME in steps[2]
    if kind == "physical":
        assert "Restart mongod and the PBM agents on every node" in result.stdout
    else:
        assert "Restart mongod" not in result.stdout


def test_restore_archive_leaves_a_mismatched_pbm_like_archive_as_plain_tar(site):
    (site.storage / f"{NAME}.pbm.json").write_text(
        json.dumps({"name": NEXT, "type": "logical", "status": "done"}),
    )
    backup = site.run()
    assert backup.exit_code == 0, backup.output
    destination = site.root / "plain"
    result = invoke("restore", "--archive", site.unit(), "--dst", destination)
    assert result.exit_code == 0, result.output
    assert (destination / f"{NAME}.pbm.json").is_file()
    assert "pbm restore" not in result.stdout
