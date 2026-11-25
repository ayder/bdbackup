"""Host-side witnesses for the PBM probe with Docker calls replaced."""

import hashlib
import subprocess
import tarfile

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
