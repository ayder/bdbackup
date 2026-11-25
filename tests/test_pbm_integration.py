"""Host-side witnesses for the PBM probe with Docker calls replaced."""

import hashlib
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from scripts import integration_pbm as harness


def test_probe_image_fingerprint_covers_candidate_sources(tmp_path):
    package = tmp_path / "bdbackup"
    package.mkdir()
    sources = [package / "client.py", package / "_ssh_status.sh", tmp_path / "pyproject.toml"]
    for path in sources:
        path.write_text(path.name)
    before = harness.source_hash(tmp_path)
    assert len(before) == 64, "the probe image must identify the wheel's source"
    for path in sources:
        content = path.read_text()
        path.write_text(content + "\nchanged")
        assert harness.source_hash(tmp_path) != before, f"{path.name} affects wheel provenance"
        path.write_text(content)


def test_probe_checks_image_before_running_a_selected_case(monkeypatch):
    calls = []
    monkeypatch.setattr(harness, "source_hash", lambda: "candidate")
    monkeypatch.setattr("shutil.which", lambda name: "/fixture/docker")

    def docker(argv, **kwargs):
        calls.append(tuple(argv[1:]))
        return subprocess.CompletedProcess(argv, 0, "stale\n", "")

    monkeypatch.setattr(subprocess, "run", docker)
    try:
        harness.main(["--probe", "mongo7-pbm217", "--case", "pitr"])
    except SystemExit as exc:
        assert "other sources" in str(exc)
    assert len(calls) == 1, "check provenance before launching the selected fixture"
    assert calls[0][:2] == ("image", "inspect")
    assert calls[0][-1] == "bdbackup-pbm:mongo7-pbm217"


def test_probe_names_each_assertion(capsys):
    harness.expect("storage belongs to the secondary", True)
    assert capsys.readouterr().out == "PASS: storage belongs to the secondary\n"
    with pytest.raises(AssertionError, match="restored rows match"):
        harness.expect("restored rows match", False)



@pytest.fixture
def integration_host(monkeypatch):
    monkeypatch.setattr(harness, "source_hash", lambda: "candidate")
    monkeypatch.setattr("shutil.which", lambda name: "/fixture/docker")
    inspections = []
    cases = []
    state = {"label": "candidate"}

    def docker(argv, **kwargs):
        assert argv[1:3] == ("image", "inspect")
        inspections.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, state["label"] + "\n", "")

    def case(docker, fixture, selected):
        assert docker == "/fixture/docker"
        cases.append((fixture, selected))

    monkeypatch.setattr(subprocess, "run", docker)
    monkeypatch.setattr(harness, "run_integration_case", case, raising=False)
    return inspections, cases, state


def test_integration_dispatches_both_fixtures_and_every_case(integration_host, capsys):
    inspections, cases, _ = integration_host
    harness.main([])
    assert inspections == [harness.image(fixture) for fixture in harness.FIXTURES]
    assert cases == [(fixture, case) for fixture in harness.FIXTURES
                     for case in harness.INTEGRATION_CASES]
    assert capsys.readouterr().out.splitlines()[-1] == harness.SUCCESS


def test_integration_can_run_only_one_case_on_one_fixture(integration_host, capsys):
    inspections, cases, _ = integration_host
    harness.main(["--fixture", "mongo7-pbm217", "--case", "physical"])
    assert inspections == [harness.image("mongo7-pbm217")]
    assert cases == [("mongo7-pbm217", "physical")]
    assert capsys.readouterr().out.splitlines()[-1] == harness.SUCCESS


def test_integration_refuses_a_stale_candidate_image(integration_host):
    inspections, cases, state = integration_host
    state["label"] = "stale"
    with pytest.raises(SystemExit, match="other sources"):
        harness.main(["--fixture", "mongo6-pbm210"])
    assert inspections == [harness.image("mongo6-pbm210")]
    assert not cases


def test_integration_failure_names_its_assertion_without_a_success_line(
    integration_host, monkeypatch, capsys,
):
    def fail(*args):
        harness.expect("physical restore matches the source rows", False)

    monkeypatch.setattr(harness, "run_integration_case", fail, raising=False)
    with pytest.raises(AssertionError, match="physical restore matches the source rows failed"):
        harness.main(["--fixture", "mongo7-pbm217", "--case", "physical"])
    assert harness.SUCCESS not in capsys.readouterr().out


@pytest.mark.parametrize("difference", ["none", "bytes", "extra", "duplicate"])
def test_integration_checks_the_exact_archive_members_and_bytes(tmp_path, difference):
    name = "2026-11-25T08:59:27Z"
    source = tmp_path / "storage"
    tree = source / name
    tree.mkdir(parents=True)
    meta = source / f"{name}.pbm.json"
    meta.write_bytes(b"metadata")
    member = tree / "data"
    member.write_bytes(b"original bytes")
    archive = tmp_path / "unit.tar"
    with tarfile.open(archive, "w") as unit:
        unit.add(meta, arcname=meta.name)
        unit.add(tree, arcname=name)
        if difference == "extra":
            unit.add(member, arcname="unrelated")
        elif difference == "duplicate":
            unit.add(member, arcname=f"{name}/data")
    if difference == "bytes":
        member.write_bytes(b"different byte")

    facts = harness.archive_facts(archive, source, name)
    assert facts.get("names_match") is (difference in {"none", "bytes"})
    assert facts["types_match"] is True
    assert facts["different_files"] == ([f"{name}/data"] if difference == "bytes" else [])
    assert facts["size"] == archive.stat().st_size
    assert facts["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()


RESTORE_NAME = "2026-11-25T12:14:37.708524218Z"
BACKUP_NAME = "2026-11-25T12:14:06Z"


class RestoreFixture:
    nodes = ("primary", "secondary", "peer")
    primary = nodes[0]

    def __init__(self):
        self.calls = []
        self.elapsed = 0
        self.restart = False
        self.listed = 0
        self.kind = "physical"
        self.pure_json = False

    def exec(self, *args):
        return subprocess.CompletedProcess(args, 0, "", "")

    def mongo(self, node, js):
        self.calls.append(("mongo", node))
        data = "0" if "countDocuments" in js else json.dumps(harness.ROWS)
        return subprocess.CompletedProcess((node, js), 0, data, "")

    def configure(self, storage, *, wait=False):
        self.calls.append(("configure", storage, wait))

    def wait_agents(self, nodes):
        assert nodes == self.nodes

    def pbm(self, *args, **kwargs):
        self.calls.append(args)
        text = ""
        stderr = ""
        status = 0
        if args[0] == "restore":
            if self.elapsed < 23 and "--yes" in args and self.kind == "physical":
                status = 1
                stderr = "Error: operation staled, last heartbeat: 0\n"
            elif "--out" in args and args[args.index("--out") + 1] == "json":
                payload = json.dumps({"name": RESTORE_NAME})
                text = payload if self.pure_json else f"Started restore.\nWaiting...{payload}\n"
            else:
                text = (f"Waiting to finish......Restore finished! Check pbm describe-restore "
                        f"{RESTORE_NAME}\n")
        elif args[0] == "describe-restore":
            assert args[1] == RESTORE_NAME
            text = '{"status":"done"}'
        elif args[0] == "status":
            running = {"type": "resync"} if self.elapsed < 3 else {}
            text = json.dumps({"running": running, "pitr": {"conf": False, "run": False}})
        elif args == ("list",):
            if self.restart:
                self.listed += 1
            text = BACKUP_NAME if not self.restart or self.listed > 1 else ""
        return subprocess.CompletedProcess(args, status, text, stderr)

    def stop_agent(self, node):
        self.calls.append(("stop-agent", node))

    def start_mongod(self, node):
        self.restart = True
        self.calls.append(("start-mongod", node))

    def wait_members(self):
        self.calls.append(("ready",))

    def start_agent(self, node):
        self.calls.append(("start-agent", node))


@pytest.fixture
def restore_fixture(monkeypatch):
    fixture = RestoreFixture()

    def sleep(seconds):
        fixture.elapsed += seconds

    monkeypatch.setattr(harness, "time", SimpleNamespace(monotonic=lambda: fixture.elapsed,
                                                        sleep=sleep))
    return fixture


@pytest.mark.parametrize(("fixture", "kind"), [
    ("mongo6-pbm210", "logical"),
    ("mongo7-pbm217", "logical"),
    ("mongo6-pbm210", "physical"),
    ("mongo7-pbm217", "physical"),
])
def test_restore_reads_the_text_completion_and_resyncs_after_physical_restart(
    restore_fixture, fixture, kind, capsys,
):
    cluster = restore_fixture
    cluster.kind = kind
    harness.restore_shared(cluster, kind, fixture, BACKUP_NAME)
    restores = [args for args in cluster.calls if args[0] == "restore"]
    flags = ("--yes",) if fixture == "mongo7-pbm217" else ()
    assert restores == [
        ("restore", BACKUP_NAME, *flags, "--wait", "--out", "text"),
    ], "restore must use the text completion form"
    text = capsys.readouterr().out
    assert f"PASS: {kind}: restored rows match every original row" in text
    if kind == "physical":
        assert [args for args in cluster.calls if args[0] == "start-mongod"] == [
            ("start-mongod", node) for node in cluster.nodes
        ]
        assert [args for args in cluster.calls if args[0] == "start-agent"] == [
            ("start-agent", node) for node in cluster.nodes
        ]
        assert [args for args in cluster.calls if args[:2] == ("config", "--force-resync")] == [
            ("config", "--force-resync", "--wait"), ("config", "--force-resync"),
        ]
        assert cluster.listed == 2
        if fixture == "mongo7-pbm217":
            assert cluster.elapsed >= 23
            assert any(args[:3] == ("status", "--sections", "running") for args in cluster.calls)
    else:
        assert not cluster.restart


def test_physical_restart_requests_resync_without_the_hanging_wait_flag(restore_fixture):
    cluster = restore_fixture
    cluster.pure_json = True  # Bypass the old restore-output bug to check the next command.
    harness.restore_shared(cluster, "physical", "mongo6-pbm210", BACKUP_NAME)
    resyncs = [args for args in cluster.calls if args[:2] == ("config", "--force-resync")]
    assert resyncs[-1] == ("config", "--force-resync"), (
        "PBM 2.10 must resync without --wait after restart"
    )
    assert cluster.listed == 2, "wait until PBM lists the backup again"


def test_fixture_uses_an_init_process_and_removes_volumes_on_failure(monkeypatch):
    cluster = harness.ReplicaSet("/fixture/docker", "test")
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, int(args[0] == "run"), "", "")

    monkeypatch.setattr(cluster, "command", command)
    with pytest.raises(AssertionError, match="start isolated container failed"), cluster.started():
        pytest.fail("the simulated container launch should fail")
    attempt, = (args for args in calls if args[0] == "run")
    assert "--init" in attempt, "the container init process must reap mongod after restore"
    assert ("rm", "--force", "--volumes", cluster.primary) in calls
    assert ("volume", "rm", cluster.volume) in calls
    assert ("network", "rm", cluster.network) in calls
