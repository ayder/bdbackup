"""One remote delivery, with every subprocess replaced at the execution boundary."""

import re
import subprocess
from collections import deque
from datetime import UTC

import pytest

from bdbackup.ssh_copy import CopyCommandError, CopySettings, copy_artifact

DEST = "/remote/archive%20"
NAME = "daily+1.tar.gz"
FINAL = f"{DEST}/{NAME}"
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "local.tar.gz"
    path.write_bytes(b"local archive")
    path.chmod(0o640)
    return path


@pytest.fixture
def processes(monkeypatch):
    calls = []
    responses = deque()

    def execute(argv, **kwargs):
        calls.append(argv)
        assert responses, f"unexpected command: {argv}"
        status, stdout, stderr = responses.popleft()
        return subprocess.CompletedProcess(argv, status, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", execute)
    return calls, responses


def copy(source, *, method="scp", name=NAME, checksum=DIGEST):
    settings = CopySettings(f"ssh://vault{DEST}", method, "vault", DEST)
    return copy_artifact(settings, source, name, checksum)


def result(stdout="", status=0, stderr=""):
    return status, stdout, stderr


def staged(mode="640", checksum=DIGEST):
    return result(f"{checksum}  /temporary\n{mode}\n")


def assert_order(calls, *commands):
    assert len(calls) == len(commands), "copy must execute all and only the expected calls"
    for argv, expected in zip(calls, commands, strict=True):
        if expected in {"scp", "rsync"}:
            assert argv[0] == expected
        else:
            assert argv[0] == "ssh"
            assert argv[-1].startswith(expected)


def temp_name(transfer):
    target = transfer[-1]
    prefix = f"vault:{DEST}/.bdbackup-tmp-"
    assert target.startswith(prefix) and target.endswith(f"-{NAME}")
    actual = target[len("vault:"):]
    pattern = rf"{re.escape(DEST)}/\.bdbackup-tmp-[a-f0-9]{{32}}-{re.escape(NAME)}"
    assert re.fullmatch(pattern, actual)
    return actual


@pytest.mark.parametrize("method", ["scp", "rsync"])
def test_new_copy_checks_remote_hash_and_mode_before_publish(source, processes, method):
    calls, responses = processes
    responses.extend([result(), result(), staged(), result(), result()])

    outcome = copy(source, method=method)

    assert_order(calls, "test -d ", method, "test -f ", "ln -T -- ", "rm -f -- ")
    temp = temp_name(calls[1])
    assert calls[1][-2] == str(source)
    if method == "rsync":
        assert "--inplace" in calls[1]
        assert "command -v rsync " in calls[0][-1]
    assert f"test -w {DEST}" in calls[0][-1]
    assert "command -v sha256sum " in calls[0][-1]
    assert f"sha256sum -- {FINAL}" in calls[0][-1]
    assert f"sha256sum -- {temp}" in calls[2][-1]
    assert f"stat -c %a -- {temp}" in calls[2][-1]
    assert calls[3][-1].startswith(f"ln -T -- {temp} {FINAL} ")
    assert calls[4][-1] == f"rm -f -- {temp}"
    assert outcome.bytes_sent == source.stat().st_size
    assert not outcome.already_there
    assert outcome.started_at.tzinfo == UTC
    assert outcome.ended_at >= outcome.started_at


def test_attempts_use_different_temporary_names(source, processes):
    calls, responses = processes
    responses.extend([result(), result(), staged(), result(), result()] * 2)
    copy(source)
    copy(source)
    assert len(calls) == 10, "each attempt must send and publish its own temporary file"
    assert temp_name(calls[1]) != temp_name(calls[6])


def test_matching_final_sends_nothing_and_leaves_its_mode_alone(source, processes):
    calls, responses = processes
    responses.append(result(f"{DIGEST}  {FINAL}\n"))

    outcome = copy(source)

    assert_order(calls, "test -d ")
    assert "chmod" not in calls[0][-1] and "stat" not in calls[0][-1]
    assert (outcome.already_there, outcome.bytes_sent) == (True, 0)
    assert outcome.ended_at >= outcome.started_at


def test_different_final_is_name_taken_and_untouched(source, processes):
    calls, responses = processes
    responses.append(result(f"{OTHER_DIGEST}  {FINAL}\n"))

    with pytest.raises(CopyCommandError) as error:
        copy(source)

    assert_order(calls, "test -d ")
    assert error.value.failure_class == "name_taken"


@pytest.mark.parametrize("status,why", [
    (20, "destination_not_ready"),
    (21, "tool_missing"),
    (22, "name_taken"),
    (23, "other"),
    (255, "connect"),
])
def test_preflight_errors_stop_before_transfer_or_cleanup(source, processes, status, why):
    calls, responses = processes
    responses.append(result(status=status, stderr="old\nlast diagnostic\n"))

    with pytest.raises(CopyCommandError) as error:
        copy(source)

    assert_order(calls, "test -d ")
    assert (error.value.failure_class, error.value.diagnostic) == (why, "last diagnostic")
    assert str(error.value) == why


def test_transfer_failure_cleans_only_its_temp_and_keeps_original_diagnostic(source, processes):
    calls, responses = processes
    responses.extend([
        result(), result(status=1, stderr="transfer failure"),
        result(status=255, stderr="cleanup failure"),
    ])

    with pytest.raises(CopyCommandError) as error:
        copy(source)

    assert_order(calls, "test -d ", "scp", "rm -f -- ")
    assert calls[2][-1] == f"rm -f -- {temp_name(calls[1])}"
    assert (error.value.failure_class, error.value.diagnostic) == ("transfer", "transfer failure")


def test_bad_staged_digest_is_removed_without_publish(source, processes):
    calls, responses = processes
    responses.extend([result(), result(), staged(checksum=OTHER_DIGEST), result()])

    with pytest.raises(CopyCommandError) as error:
        copy(source)

    assert_order(calls, "test -d ", "scp", "test -f ", "rm -f -- ")
    assert calls[3][-1] == f"rm -f -- {temp_name(calls[1])}"
    assert error.value.failure_class == "checksum_mismatch"


def test_removed_source_during_transfer_is_not_published(source, processes, monkeypatch):
    calls, responses = processes
    responses.extend([result(), result(), result()])
    execute = subprocess.run

    def remove_source(argv, **kwargs):
        if argv[0] == "scp":
            source.unlink()
        return execute(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", remove_source)
    with pytest.raises(CopyCommandError, match="transfer"):
        copy(source)

    assert_order(calls, "test -d ", "scp", "rm -f -- ")


def test_hash_command_failure_removes_temp(source, processes):
    calls, responses = processes
    responses.extend([result(), result(), result(status=23, stderr="cannot hash"), result()])

    with pytest.raises(CopyCommandError) as error:
        copy(source)

    assert_order(calls, "test -d ", "scp", "test -f ", "rm -f -- ")
    assert error.value.diagnostic == "cannot hash"


@pytest.mark.parametrize("status,why", [(22, "name_taken"), (23, "other")])
def test_publish_failure_cleans_temp_and_never_replaces_final(source, processes, status, why):
    calls, responses = processes
    responses.extend([result(), result(), staged(), result(status=status), result()])

    with pytest.raises(CopyCommandError) as error:
        copy(source)

    assert_order(calls, "test -d ", "scp", "test -f ", "ln -T -- ", "rm -f -- ")
    assert f"test -e {FINAL} || test -L {FINAL}" in calls[3][-1]
    assert calls[4][-1] == f"rm -f -- {temp_name(calls[1])}"
    assert error.value.failure_class == why


def test_wider_mode_is_capped_without_adding_bits_before_publish(source, processes):
    calls, responses = processes
    responses.extend([result(), result(), staged("606"), result("600\n"), result(), result()])

    copy(source)

    assert_order(calls, "test -d ", "scp", "test -f ", "chmod 0600 ", "ln -T -- ", "rm -f -- ")
    assert f"stat -c %a -- {temp_name(calls[1])}" in calls[3][-1]


@pytest.mark.parametrize("cap", [result(status=23, stderr="permission denied"), result("666\n")])
def test_refused_or_ineffective_mode_cap_blocks_publish(source, processes, cap):
    calls, responses = processes
    responses.extend([result(), result(), staged("666"), cap, result()])

    with pytest.raises(CopyCommandError) as error:
        copy(source)

    assert_order(calls, "test -d ", "scp", "test -f ", "chmod 0640 ", "rm -f -- ")
    assert error.value.failure_class == "other"


def test_publish_success_stays_copied_when_unlink_fails(source, processes, caplog):
    calls, responses = processes
    responses.extend([result(), result(), staged(), result(), result(status=23, stderr="denied")])

    outcome = copy(source)

    assert_order(calls, "test -d ", "scp", "test -f ", "ln -T -- ", "rm -f -- ")
    assert outcome.bytes_sent == source.stat().st_size
    assert any(
        record.levelname == "WARNING" and temp_name(calls[1]) in record.message
        for record in caplog.records
    ), "published copy must warn about the leftover temporary name"


@pytest.mark.parametrize("name", ["../bad", "unsafe name", ".", "..", "bad;echo"])
def test_unsafe_artifact_name_runs_no_tool(source, processes, name):
    calls, _ = processes
    with pytest.raises(CopyCommandError, match="other") as error:
        copy(source, name=name)
    assert "artifact" in error.value.diagnostic
    assert calls == []


@pytest.mark.parametrize("missing", [True, False], ids=["missing", "directory"])
def test_unavailable_local_source_runs_no_tool(source, processes, missing):
    calls, _ = processes
    source.unlink()
    if not missing:
        source.mkdir()
    with pytest.raises(CopyCommandError, match="source_unavailable"):
        copy(source)
    assert calls == []
