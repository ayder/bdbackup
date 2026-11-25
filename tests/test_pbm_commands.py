"""PBM executable tests, substituting subprocess.run at the process boundary."""

import json
import os
import subprocess

import pytest

from bdbackup.mongodb.pbm import PBM, PBMError

NAME = "2026-11-25T08:59:27Z"
URI = "mongodb://fixture-user:fixture-password@localhost/?authSource=admin"
INHERITED_URI = "mongodb+srv://environment-user:environment-password@fixture.invalid/"
URI_KEY = "PBM_MONGODB_URI"
WARNING = "WARNING: This PBM works with MongoDB and PSMDB v7.0; you are running v6.0."


@pytest.fixture
def tool(monkeypatch, tmp_path):
    calls = []
    responses = []
    monkeypatch.delenv(URI_KEY, raising=False)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, out, err = response
        stdout = out if isinstance(out, str) else json.dumps(out)
        return subprocess.CompletedProcess(argv, status, stdout, err)

    monkeypatch.setattr(subprocess, "run", run)
    return PBM(tmp_path / "pbm", tmp_path / "backups", "physical", "/fixture/pbm"), calls, responses


def test_pbm_command_lines(tool):
    backend, calls, responses = tool
    responses.extend([
        (0, {"name": NAME}, ""),
        (0, {"status": "done", "type": "physical",
             "replsets": [{"name": "rs0", "node": "secondary:27017"}]}, ""),
        (0, {"cluster": []}, ""),
        (0, {"Version": "2.17.0"}, ""),
    ])
    backend.take()
    backend.describe(NAME)
    backend.status()
    backend.version()
    assert [argv for argv, _ in calls] == [
        ["/fixture/pbm", "backup", "--type", "physical", "--wait", "--out", "json"],
        ["/fixture/pbm", "describe-backup", NAME, "--out", "json"],
        ["/fixture/pbm", "status", "--out", "json"],
        ["/fixture/pbm", "version", "--out", "json"],
    ], "each operation uses its PBM command, requesting JSON"
    assert all(options["capture_output"] for _, options in calls)
    assert all("timeout" not in options for _, options in calls)


@pytest.mark.parametrize("release", ["2.10.0", "2.17.0"])
def test_pbm_reads_measured_json_fields(tool, release):
    backend, _, responses = tool
    storage_field = "storage" if release == "2.10.0" else "storagePath"
    status = {
        "cluster": [{"rs": "rs0", "nodes": [
            {"host": "primary:27017", "agent": release, "ok": False},
            {"host": "secondary:27017", "agent": release, "ok": True},
        ]}],
        "backups": {"type": "FS", "path": "/storage"},
    }
    responses.extend([
        (0, {"name": NAME, storage_field: {"deliberately": "unexpected type"}}, ""),
        (0, {
            "status": "done", "type": "logical", "mongodb_version": None,
            "pbm_version": [release], "size": False,
            "replsets": [{"name": "rs0", "node": "secondary:27017", "status": "ignored"}],
        }, ""),
        (0, status, ""),
        (0, {"Version": release, "Platform": "linux/arm64"}, ""),
    ])
    assert backend.take() == NAME, "PBM backup name is independent of the storage field"
    desc = backend.describe(NAME)
    assert (desc.status, desc.backup_type) == ("done", "logical")
    assert [(rs.name, rs.node) for rs in desc.replsets] == [("rs0", "secondary:27017")]
    assert backend.status() == status
    assert backend.version()["Version"] == release


def test_pbm_keeps_an_incomplete_status(tool):
    backend, _, responses = tool
    responses.append((0, {"status": "running", "type": "logical"}, ""))
    reply = backend.describe(NAME)
    assert reply is not None, "an unfinished backup still has a description"
    assert (reply.status, reply.backup_type, reply.replsets) == ("running", "logical", ())


@pytest.mark.parametrize("source", ["config", "environment"])
def test_pbm_puts_uri_only_in_child_environment(tool, monkeypatch, caplog, capsys, source):
    backend, calls, responses = tool
    monkeypatch.setenv(URI_KEY, INHERITED_URI)
    sent = INHERITED_URI
    if source == "config":
        backend.mongodb_uri = URI
        sent = URI
    responses.append((0, {"name": NAME}, f"{WARNING}\nPBM echoed {sent}\n"))
    assert backend.take() == NAME, "stderr cannot affect stdout JSON"
    [(argv, options)] = calls
    assert all(sent not in arg for arg in argv)
    assert options["env"][URI_KEY] == sent
    assert os.environ[URI_KEY] == INHERITED_URI
    assert WARNING in caplog.text and "PBM echoed <redacted>" in caplog.text
    captured = capsys.readouterr()
    assert sent not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize("source", ["config", "environment"])
def test_pbm_failure_reports_only_the_last_sanitized_line(tool, monkeypatch, caplog, source):
    backend, _, responses = tool
    if source == "config":
        backend.mongodb_uri = URI
    else:
        monkeypatch.setenv(URI_KEY, URI)
    responses.append((1, "", f"earlier detail\nfailed to connect with {URI}\n\n"))
    with pytest.raises(PBMError, match=r"^pbm backup failed: failed to connect with <redacted>$"):
        backend.take()
    assert URI not in caplog.text


def test_pbm_failure_uses_stdout_diagnostic_without_stderr_error(tool):
    backend, _, responses = tool
    responses.append((1, {"Error": "another operation in progress"}, WARNING))
    with pytest.raises(PBMError, match="pbm backup failed: .*another operation in progress"):
        backend.take()


@pytest.mark.parametrize("error", [
    FileNotFoundError("missing binary"),
    PermissionError("permission denied"),
], ids=["missing", "not-executable"])
def test_pbm_reports_a_binary_that_cannot_be_started(tool, error):
    backend, _, responses = tool
    responses.append(error)
    with pytest.raises(PBMError, match="pbm status failed: cannot run /fixture/pbm"):
        backend.status()


@pytest.mark.parametrize("stdout", ["not JSON\nlast stdout line", "[]", "null"])
def test_pbm_rejects_invalid_stdout_even_with_stderr_json(tool, stdout):
    backend, _, responses = tool
    responses.append((0, stdout, '{"name": "valid JSON on stderr"}'))
    with pytest.raises(PBMError, match="pbm backup failed: stdout is not a JSON object"):
        backend.take()


def test_pbm_invalid_json_reports_a_sanitized_diagnostic(tool):
    backend, _, responses = tool
    backend.mongodb_uri = URI
    responses.append((0, f"bad response from {URI}", ""))
    with pytest.raises(PBMError, match="bad response from <redacted>") as error:
        backend.take()
    assert URI not in str(error.value)


@pytest.mark.parametrize("name_case", ["absent", "path", "uri"])
def test_pbm_rejects_names_that_cannot_identify_a_backup(tool, name_case):
    backend, _, responses = tool
    names = {"absent": {}, "path": {"name": "../outside"}, "uri": {"name": URI}}
    responses.append((0, names[name_case], ""))
    with pytest.raises(PBMError, match="pbm backup failed: .*backup name") as error:
        backend.take()
    assert URI not in str(error.value)


@pytest.mark.parametrize("defect", ["status", "type", "replicas", "node"])
def test_pbm_rejects_an_incomplete_done_description(tool, defect):
    backend, _, responses = tool
    replica = {"name": "rs0", "node": "secondary:27017"}
    reply = {"status": "done", "type": "logical", "replsets": [replica]}
    if defect == "node":
        del replica["node"]
    else:
        del reply["replsets" if defect == "replicas" else defect]
    responses.append((0, reply, ""))
    with pytest.raises(PBMError, match="pbm describe-backup failed: .*"):
        backend.describe(NAME)
