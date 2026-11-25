"""Host-side witnesses for the PBM probe with Docker calls replaced."""

import subprocess

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
