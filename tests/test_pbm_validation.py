"""PBM dry runs and validation exercise the CLI with only read-only tool fakes."""

import json
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from bdbackup import validation
from bdbackup.cli import main

URI_ENV = "PBM_MONGODB_URI"
URI = "mongodb://fixture-user:fixture-pass@localhost/"
COMMANDS = {
    ("version", "--out", "json"): "version",
    ("status", "--sections", "cluster", "--out", "json"): "status",
    ("config", "--list", "--out", "json"): "storage",
}
WARNING = (
    "WARNING: This PBM works with MongoDB and PSMDB v7.0, v8.0, v8.3 and you are running v6.0. "
    "Please upgrade your MongoDB to a supported version."
)
PRIVATE_CONFIG = "private-storage-credential"


class Site:
    def __init__(self, root, monkeypatch):
        self.root = root.resolve()
        self.config = self.root / "backup.toml"
        self.storage = self.root / "store"
        self.output = self.root / "out"
        self.storage.mkdir()
        self.marker = self.storage / ".pbm.init"
        self.marker.write_text("PBM marker")
        self.executable = "/fixture/pbm"
        self.calls = []
        self.replies = {
            "version": {"Version": "2.10.0"},
            "status": {"cluster": [{"rs": "rs0", "nodes": [
                {"host": "primary:27017", "agent": "v2.10.0", "ok": False},
                {"host": "secondary:27017", "agent": "v2.10.0", "ok": True},
                {"host": "other:27017", "agent": "", "ok": False},
            ]}]},
            "storage": {"storage": {"type": "filesystem", "private": PRIVATE_CONFIG}},
        }
        self.stderr = {}
        self.write()
        monkeypatch.setenv(URI_ENV, URI)
        monkeypatch.setattr(shutil, "which", self.which)
        monkeypatch.setattr(subprocess, "run", self.run_tool)

    def which(self, binary):
        assert binary == "pbm"
        return self.executable

    def run_tool(self, argv, **kwargs):
        assert argv[0] == "pbm"
        args = tuple(argv[1:])
        self.calls.append((args, kwargs["env"].get(URI_ENV)))
        command = COMMANDS.get(args)
        assert command is not None, f"validation ran an unapproved command: {args}"
        reply = self.replies[command]
        stderr = self.stderr.get(command, "")
        if isinstance(reply, tuple):
            return subprocess.CompletedProcess(argv, *reply, stderr)
        if isinstance(reply, str):
            return subprocess.CompletedProcess(argv, 1, "", stderr + reply)
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), stderr)

    def write(self, extra=""):
        self.config.write_text(
            '[history]\ndatabase="state/history.sqlite3"\n'
            '[mongo]\ntype="pbm"\nstorage_path="store"\nout_dir="out"\n' + extra,
        )

    @property
    def nodes(self):
        return self.replies["status"]["cluster"][0]["nodes"]

    def invoke(self, flag):
        return CliRunner().invoke(main, ["run", "-c", str(self.config), "-j", "mongo", flag])

    def snapshot(self):
        return {
            path.relative_to(self.root): (
                path.stat().st_mode, path.stat().st_ctime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.root.rglob("*")
        }


@pytest.fixture
def site(tmp_path, monkeypatch):
    return Site(tmp_path, monkeypatch)


@pytest.mark.parametrize("copy", [False, True], ids=["local", "copy"])
def test_pbm_dry_run_prints_the_plan_and_does_not_write_or_execute(site, copy):
    additional = 'backup_type="physical"\n' if copy else ""
    if copy:
        additional += '[mongo.copy]\ndestination="ssh://fixture/volume"\nmethod="scp"\n'
    site.write(additional)
    before = site.snapshot()
    result = site.invoke("--dry-run")
    assert result.exit_code == 0, result.output
    kind = "physical" if copy else "logical"
    expected = f"Dry run; would take a {kind} PBM backup and export it into {site.output}\n"
    if copy:
        expected += "Dry run; configured copy: ssh://fixture/volume (scp)\n"
    assert result.stdout == expected
    assert not site.calls
    assert site.snapshot() == before


@pytest.mark.parametrize(("version", "line"), [
    ("2.10.0", "[OK] PBM 2.10.0"),
    ("2.17.0", "[OK] PBM 2.17.0"),
    ("2.18.0", "[OK] PBM 2.18.0 (not a tested version: 2.10.0, 2.17.0)"),
])
def test_pbm_validation_reports_versions_and_accepts_one_healthy_agent(site, version, line):
    site.replies["version"]["Version"] = version
    for node in site.nodes[:2]:
        node["agent"] = f"v{version}"
    before = site.snapshot()
    result = site.invoke("--validate")
    assert result.exit_code == 0, result.output
    assert line in result.stdout.splitlines()
    assert "[OK] Executable: /fixture/pbm" in result.stdout
    assert "[OK] PBM connection string is set." in result.stdout
    assert "[OK] PBM status answered." in result.stdout
    assert f"[OK] PBM command and agents use version {version}." in result.stdout
    assert "[OK] PBM agents: 1 of 3 OK. Not OK: primary:27017, other:27017." in result.stdout
    assert "[OK] PBM storage type: filesystem." in result.stdout
    assert "[OK] PBM storage:" in result.stdout and "[OK] PBM storage marker:" in result.stdout
    assert "[OK] Backup destination:" in result.stdout
    assert "MongoDB version" not in result.stdout
    assert URI not in result.output
    assert {call for call, uri in site.calls if uri == URI} == COMMANDS.keys()
    assert len(site.calls) == len(COMMANDS)
    assert site.snapshot() == before


def test_pbm_validation_accepts_configured_uri_without_exposing_it(site, monkeypatch):
    monkeypatch.delenv(URI_ENV)
    site.write(f"mongodb_uri={json.dumps(URI)}\n")
    result = site.invoke("--validate")
    assert result.exit_code == 0, result.output
    assert URI not in result.output
    assert all(uri == URI for _, uri in site.calls)
    assert set(args for args, _ in site.calls) == COMMANDS.keys()


@pytest.mark.parametrize(("failure", "diagnostic"), [
    ("binary", "Install Percona Backup for MongoDB"),
    ("uri", "set mongodb_uri or PBM_MONGODB_URI"),
    ("status", "Check that the URI reaches the replica set"),
    ("version", "matches the agents"),
    ("mismatch", "Install matching PBM and agent versions"),
    ("agents", "Start or repair a pbm-agent"),
    ("storage", "set PBM storage.type to filesystem"),
    ("storage-query", "Check PBM's storage configuration"),
    ("marker", "Point storage_path at PBM's initialized filesystem storage"),
    ("source", "directory is required"),
    ("unreadable", "needs read/search on directory"),
    ("outdir", "needs read/write/search on directory"),
])
def test_pbm_validation_reports_actionable_failures(site, monkeypatch, failure, diagnostic):
    if failure == "binary":
        site.executable = None
    elif failure == "uri":
        monkeypatch.delenv(URI_ENV)
    elif failure == "status":
        site.replies["status"] = f"could not connect using {URI}"
    elif failure == "version":
        site.replies["version"] = {}
    elif failure == "mismatch":
        site.nodes[0]["agent"] = "v2.17.0"
    elif failure == "agents":
        site.nodes[1]["ok"] = False
    elif failure == "storage":
        site.replies["storage"]["storage"]["type"] = "s3"
    elif failure == "storage-query":
        site.replies["storage"] = "configuration is unavailable"
    elif failure == "marker":
        site.marker.unlink()
    elif failure == "source":
        site.marker.unlink()
        site.storage.rmdir()
    elif failure == "unreadable":
        access = validation._access
        monkeypatch.setattr(validation, "_access",
                            lambda path, flags: path != site.storage and access(path, flags))
    else:
        site.output.touch()
    before = site.snapshot()
    result = site.invoke("--validate")
    assert result.exit_code == 1, result.output
    assert any(line.startswith("[FAIL]") and diagnostic in line
               for line in result.stdout.splitlines()), result.output
    assert URI not in result.output
    if failure in {"binary", "uri"}:
        expected = [] if failure == "binary" else [("version", "--out", "json")]
        assert [args for args, _ in site.calls] == expected
    assert site.snapshot() == before



def test_pbm_validation_repeats_pbm_warnings_once_in_report(site, caplog):
    site.replies["version"]["Version"] = "2.17.0"
    for node in site.nodes[:2]:
        node["agent"] = "v2.17.0"
    site.stderr["status"] = WARNING + "\n"
    site.stderr["storage"] = WARNING + f"\nWARNING: Check connection to {URI}\n"
    result = site.invoke("--validate")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "[OK] PBM 2.17.0" in lines
    assert lines.count(f"[NOTE] {WARNING}") == 1, result.stdout
    assert lines.count("[NOTE] WARNING: Check connection to <redacted>") == 1
    assert WARNING in caplog.text
    assert URI not in result.output + caplog.text
    assert not any(line.startswith("[FAIL]") for line in lines)


def test_pbm_warning_does_not_become_a_failure_diagnostic(site):
    site.replies["status"] = (1, "")
    site.stderr["status"] = WARNING + "\n"
    result = site.invoke("--validate")
    assert result.exit_code == 1, result.output
    lines = result.stdout.splitlines()
    assert lines.count(f"[NOTE] {WARNING}") == 1, result.stdout
    assert any("pbm status failed: exit status 1" in line for line in lines)
    assert not any(WARNING in line for line in lines if line.startswith("[FAIL]"))


@pytest.mark.parametrize("output", ["valid", "invalid", "failed"])
def test_pbm_validation_uses_measured_storage_config_without_exposing_payload(site, caplog, output):
    if output != "valid":
        site.replies["storage"] = (int(output == "failed"), f"unexpected {PRIVATE_CONFIG}")
    result = site.invoke("--validate")
    assert result.exit_code == (0 if output == "valid" else 1), result.output
    assert PRIVATE_CONFIG not in result.output + caplog.text
    assert (("config", "--list", "--out", "json"), URI) in site.calls
