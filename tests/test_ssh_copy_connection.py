"""Build SSH commands and classify process failures without launching tools."""

import shlex
import subprocess
from pathlib import Path

import pytest

from bdbackup.ssh_copy import CopyCommandError, CopySettings, SSHConnection

SECURE = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
]
HOST = "2001:db8::1"
HOST_SPEC = f"backup@[{HOST}]"
DEST = "/remote/copies%20+@=,_~"
TEMP = f"{DEST}/.bdbackup-tmp-0123-a%2Fbackup.tar.gz"


def connection(method="scp", **overrides):
    return SSHConnection(CopySettings(**{
        "destination": f"ssh://{HOST_SPEC}:2222{DEST}",
        "method": method,
        "host": HOST,
        "path": DEST,
        "user": "backup",
        "port": 2222,
        **overrides,
    }))


@pytest.fixture
def processes(monkeypatch):
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="remote result\n", stderr="")

    monkeypatch.setattr(subprocess, "run", execute)
    return calls


def test_control_ssh_command_uses_non_interactive_options(tmp_path, processes):
    config = tmp_path / "ssh files/config"
    result = connection(ssh_config=config).run(f"test -d {DEST}")
    assert len(processes) == 1, "control command must go through SSH"
    argv, kwargs = processes[0]
    assert argv == [
        "ssh", *SECURE, "-F", str(config), "-p", "2222", "-l", "backup",
        "--", HOST, f"test -d {DEST}",
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"]
    assert result == "remote result\n"


def test_omitted_authority_and_config_use_ssh_defaults(processes):
    connection(host="vault", user=None, port=None).run("true")
    assert len(processes) == 1, "control command must call SSH using operator defaults"
    assert processes[0][0] == ["ssh", *SECURE, "--", "vault", "true"]


def test_scp_writes_literal_remote_name_with_rate_limit(tmp_path, processes):
    config = tmp_path / "ssh files/config"
    source = tmp_path / "a%2Fbackup.tar.gz"
    connection(ssh_config=config, bwlimit=13).transfer(source, TEMP)
    assert len(processes) == 1, "SCP transfer process must be launched"
    argv, kwargs = processes[0]
    assert argv[:2] == ["scp", "-S"]
    assert Path(argv[2]).name == "_ssh_status.sh"
    assert argv[3:] == [
        *SECURE, "-F", str(config), "-P", "2222", "-l", "104",
        "--", str(source), f"{HOST_SPEC}:{TEMP}",
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_rsync_passes_ssh_arguments_as_one_remote_shell_argument(tmp_path, processes):
    config = tmp_path / "operator's ssh files" / "config"
    source = tmp_path / "a%2Fbackup.tar.gz"
    connection("rsync", ssh_config=config, bwlimit=13).transfer(source, TEMP)
    assert len(processes) == 1, "rsync transfer process must be launched"
    argv, _ = processes[0]
    assert argv[:4] == ["rsync", "--timeout=30", "--bwlimit=13", "-e"]
    escaped_config = str(config).replace("'", "''")
    last = f" {' '.join(SECURE)} -F '{escaped_config}' -p 2222"
    assert argv[4].endswith(last)
    assert Path(shlex.split(argv[4].removesuffix(last))[0]).name == "_ssh_status.sh"
    assert argv[5:] == ["--", str(source), f"{HOST_SPEC}:{TEMP}"]


@pytest.mark.parametrize("method", ["scp", "rsync"])
def test_transfer_keeps_unspecified_options_out_of_arguments(method, tmp_path, processes):
    source = tmp_path / "archive.tar.gz"
    connection(method=method, host="vault", user=None, port=None).transfer(source, TEMP)
    assert len(processes) == 1, "transfer must use the selected tool and the SSH defaults"
    argv, _ = processes[0]
    assert argv[-1] == f"vault:{TEMP}"
    assert "-P" not in argv
    assert "-l" not in argv
    assert not any(option.startswith("--bwlimit") for option in argv)
    if method == "rsync":
        shell = shlex.split(argv[argv.index("-e") + 1])
        assert shell[1:] == SECURE
    else:
        assert argv[3:-3] == SECURE


@pytest.mark.parametrize("path", ["/remote/a;echo", "/remote/../unsafe", "relative"])
def test_bad_remote_operand_never_starts_a_process(path, tmp_path, processes):
    with pytest.raises(ValueError, match="remote path"):
        connection().transfer(tmp_path / "archive", path)
    assert processes == []


@pytest.mark.parametrize("message", [
    "Host key verification failed.",
    "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!",
    "Permission denied (publickey).",
    "ssh: connect to host vault port 22: Operation timed out",
])
def test_ssh_connection_failures_have_a_fixed_class_and_last_diagnostic(monkeypatch, message):
    def execute(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, stdout="", stderr=f"earlier\n{message}\n\n")

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(CopyCommandError) as failure:
        connection().run("true")
    assert failure.value.failure_class == "connect"
    assert str(failure.value) == "connect"
    assert failure.value.diagnostic == message


@pytest.mark.parametrize("status,expected", [(21, "destination_not_ready"), (23, "other")])
def test_ssh_returns_remote_failure_classes_by_status(monkeypatch, status, expected):
    def execute(argv, **kwargs):
        return subprocess.CompletedProcess(argv, status, stdout="", stderr="remote detail")

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(CopyCommandError) as failure:
        connection().run("test -d /remote", failures={21: "destination_not_ready"})
    assert failure.value.failure_class == expected


@pytest.mark.parametrize("program", ["ssh", "scp", "rsync"])
def test_missing_local_tool(monkeypatch, tmp_path, program):
    def execute(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", execute)
    ssh = connection(method=program if program != "ssh" else "scp")
    with pytest.raises(CopyCommandError) as failure:
        if program == "ssh":
            ssh.run("true")
        else:
            ssh.transfer(tmp_path / "archive", TEMP)
    assert failure.value.failure_class == "tool_missing"
    assert program in failure.value.diagnostic


@pytest.mark.parametrize("method,tool_status", [("scp", 1), ("rsync", 12)])
@pytest.mark.parametrize("ssh_status,expected", [
    ("255", "connect"),
    ("missing", "tool_missing"),
    ("1", "transfer"),
    (None, "transfer"),
])
def test_transfer_uses_ssh_exit_status_separately_from_tool_status(
    monkeypatch, tmp_path, method, tool_status, ssh_status, expected,
):
    def execute(argv, **kwargs):
        if ssh_status is not None:
            Path(kwargs["env"]["BDBACKUP_SSH_STATUS"]).write_text(ssh_status + "\n")
        return subprocess.CompletedProcess(argv, tool_status, stdout="", stderr="detail\nlast line")

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(CopyCommandError) as failure:
        connection(method).transfer(tmp_path / "archive", TEMP)
    assert failure.value.failure_class == expected
    assert failure.value.diagnostic == "last line"
