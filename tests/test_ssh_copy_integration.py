"""Host-side witnesses for the SSH Docker harness without executing Docker."""

import subprocess

import pytest

from scripts import integration_ssh_copy as harness


def test_wheel_source_hash_covers_python_shell_and_packaging(tmp_path):
    package = tmp_path / "bdbackup"
    package.mkdir()
    files = [package / "copy.py", package / "_ssh_status.sh", tmp_path / "pyproject.toml"]
    for path in files:
        path.write_text(path.name)
    baseline = harness.source_hash(tmp_path)
    assert len(baseline) == 64, "the wheel needs a source fingerprint"
    for path in files:
        original = path.read_text()
        path.write_text(original + "\nchange")
        assert harness.source_hash(tmp_path) != baseline, f"{path.name} must affect provenance"
        path.write_text(original)
    (package / "copy.pyc").write_bytes(b"generated")
    assert harness.source_hash(tmp_path) == baseline


@pytest.fixture
def docker(monkeypatch):
    calls = []
    state = {"label": "candidate"}
    monkeypatch.setattr(harness, "source_hash", lambda: "candidate")
    monkeypatch.setattr(harness.shutil, "which", lambda name: "/fixture/docker")

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        if tuple(argv[1:3]) == ("image", "inspect"):
            return subprocess.CompletedProcess(argv, 0, state["label"] + "\n", "")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return calls, state


def test_full_gate_checks_and_runs_both_docker_images(docker):
    calls, _ = docker
    harness.run_integration()
    assert [command[1] for command in calls] == ["image", "run", "image", "run"]
    for command, ubuntu in zip(calls[1::2], harness.FIXTURES, strict=True):
        assert command[-2:] == ("--inside-integration", ubuntu)
        assert command[command.index("--network") + 1] == "none"


def test_one_case_can_run_on_one_fixture(docker):
    calls, _ = docker
    harness.run_integration(case="checksum", ubuntu="24.04")
    assert len(calls) == 2, "one case on one image needs one inspect and one run"
    assert calls[-1][-4:] == ("--inside-integration", "24.04", "--case", "checksum")


def test_stale_image_is_rejected_before_running_a_container(docker):
    calls, state = docker
    state["label"] = "stale"
    with pytest.raises(SystemExit, match="other sources"):
        harness.run_integration()
    assert [command[1] for command in calls] == ["image"]


def test_assertions_print_pass_and_name_the_failure(capsys):
    harness.expect("the file is published", True)
    assert capsys.readouterr().out == "PASS: the file is published\n"
    with pytest.raises(AssertionError, match="the checksum matches"):
        harness.expect("the checksum matches", False)
