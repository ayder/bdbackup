"""SSH copy settings and subprocess commands."""

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv6Address
from pathlib import Path
from stat import S_IMODE, S_ISREG
from uuid import uuid4

_NAME_CHARS = r"A-Za-z0-9._+=@,%~\-"
_SAFE_NAME = re.compile(rf"[{_NAME_CHARS}]+")
_SAFE_PATH = re.compile(rf"/[{_NAME_CHARS}/]*")
_AUTHORITY = re.compile(
    r"(?:(?P<user>[A-Za-z0-9_.+-]+)@)?"
    r"(?P<host>[A-Za-z0-9_.][A-Za-z0-9_.-]*|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>[0-9]+))?"
)
_KEYS = {"destination", "method", "ssh_config", "bwlimit"}
_SSH_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
)
_SSH_STATUS_HELPER = Path(__file__).with_name("_ssh_status.sh")
_REMOTE_FAILURES = {20: "destination_not_ready", 21: "tool_missing", 22: "name_taken"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODE = re.compile(r"[0-7]{1,4}")


@dataclass(frozen=True)
class CopySettings:
    destination: str
    method: str
    host: str
    path: str
    user: str | None = None
    port: int | None = None
    ssh_config: Path | None = None
    bwlimit: int | None = None


def safe_artifact_name(name: object) -> bool:
    return isinstance(name, str) and _SAFE_NAME.fullmatch(name) is not None


def _safe_remote_path(path: str) -> bool:
    return _SAFE_PATH.fullmatch(path) is not None and not {".", ".."}.intersection(path.split("/"))


def _parse_authority(authority: str) -> tuple[str | None, str, str | None, int | None]:
    match = _AUTHORITY.fullmatch(authority)
    if match is None:
        raise ValueError("copy.destination must have a valid SSH host and optional user and port")
    user, host, written_port = match.groups()
    if host.startswith("["):
        host = host[1:-1]
        try:
            IPv6Address(host)
        except ValueError:
            raise ValueError("copy.destination must have a valid IPv6 host") from None
    port = None
    if written_port is not None:
        digits = written_port.lstrip("0")
        if not digits or len(digits) > 5 or int(digits) > 65535:
            raise ValueError("copy.destination port must be in 1–65535")
        port = int(digits)
    return user, host, written_port, port


def parse_copy_settings(
    table: object, base: Path, resolve: Callable[[Path], Path],
) -> tuple[CopySettings, tuple[str, str | None, str]]:
    if not isinstance(table, dict):
        raise ValueError("copy must be a table")
    unknown = table.keys() - _KEYS
    if unknown:
        raise ValueError(f"unknown copy.{min(unknown)} key")
    destination = table.get("destination")
    if not isinstance(destination, str) or not destination.startswith("ssh://"):
        raise ValueError("copy.destination must be an ssh:// URL")
    authority, slash, rest = destination.removeprefix("ssh://").partition("/")
    path = slash + rest
    if not _safe_remote_path(path):
        raise ValueError("copy.destination needs an absolute, shell-safe path without . or ..")
    user, host, written_port, port = _parse_authority(authority)
    method = table.get("method")
    if method not in ("rsync", "scp"):
        raise ValueError("copy.method must be rsync or scp")
    ssh_config = None
    if "ssh_config" in table:
        name = table["ssh_config"]
        if not isinstance(name, str) or not name.strip() or "\0" in name:
            raise ValueError("copy.ssh_config must be a nonempty filesystem path")
        try:
            ssh_config = resolve(base / Path(name).expanduser())
        except (OSError, RuntimeError, ValueError):
            raise ValueError("copy.ssh_config could not be resolved") from None
    bwlimit = table.get("bwlimit")
    if "bwlimit" in table and (type(bwlimit) is not int or bwlimit <= 0):
        raise ValueError("copy.bwlimit must be a positive whole number")
    settings = CopySettings(destination, method, host, path, user, port, ssh_config, bwlimit)
    return settings, (host, written_port, path)


class CopyCommandError(Exception):
    """A fixed failure class and a separate operator diagnostic."""

    def __init__(self, failure_class: str, diagnostic: str | None):
        super().__init__(failure_class)
        self.failure_class = failure_class
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class CopyResult:
    bytes_sent: int
    started_at: datetime
    ended_at: datetime
    already_there: bool = False


def copy_artifact(
    settings: CopySettings, source: Path, artifact_name: str, checksum: str,
) -> CopyResult:
    """Send one regular file to a fresh temporary name and verify before linking it."""
    started = datetime.now(UTC)
    if not safe_artifact_name(artifact_name) or artifact_name in {".", ".."}:
        raise CopyCommandError("other", "invalid SSH copy artifact name")
    if not _safe_remote_path(settings.path):
        raise CopyCommandError("other", "invalid SSH copy destination path")
    source_info = _source_info(source)
    directory = settings.path.rstrip("/") or "/"
    prefix = directory.rstrip("/")
    final = f"{prefix}/{artifact_name}"
    connection = SSHConnection(settings)
    existing = _existing_checksum(connection, directory, final)
    if existing is not None:
        if existing != checksum:
            raise CopyCommandError("name_taken", None)
        return CopyResult(0, started, datetime.now(UTC), already_there=True)

    temp = f"{prefix}/.bdbackup-tmp-{uuid4().hex}-{artifact_name}"
    published = False
    try:
        connection.transfer(source, temp)
        mode = _source_mode_after_transfer(source, source_info)
        _verify_staged(connection, temp, checksum, mode)
        _publish(connection, temp, final)
        published = True
    finally:
        try:
            connection.run(f"rm -f -- {temp}")
        except CopyCommandError as exc:
            if published:
                logging.getLogger(__name__).warning(
                    "Could not remove temporary SSH copy %s: %s",
                    temp, exc.diagnostic or exc.failure_class,
                )
    return CopyResult(source_info.st_size, started, datetime.now(UTC))


def _execute(
    argv: list[str], *, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", check=False, env=env,
        )
    except FileNotFoundError:
        raise CopyCommandError("tool_missing", f"{argv[0]}: executable not found") from None
    except OSError as exc:
        raise CopyCommandError("other", f"{argv[0]}: {exc}") from None


def _last_diagnostic(result: subprocess.CompletedProcess[str]) -> str | None:
    lines = (result.stderr or "").strip().splitlines()
    return lines[-1] if lines else None


def _rsync_word(word: str) -> str:
    # rsync parses -e itself. In its quoting syntax a doubled quote is a literal quote.
    if any(char in word for char in " '\"\t\r\n"):
        return "'" + word.replace("'", "''") + "'"
    return word


class SSHConnection:
    """Use one endpoint and the same SSH options for commands and transfers."""

    def __init__(self, settings: CopySettings):
        self.settings = settings

    def _options(self, port_option: str) -> list[str]:
        options = list(_SSH_OPTIONS)
        if self.settings.ssh_config is not None:
            options.extend(("-F", str(self.settings.ssh_config)))
        if self.settings.port is not None:
            options.extend((port_option, str(self.settings.port)))
        return options

    def run(self, command: str, *, failures: Mapping[int, str] | None = None) -> str:
        """Run a fixed remote command. Reserve exit 255 for the SSH client."""
        argv = ["ssh", *self._options("-p")]
        if self.settings.user is not None:
            argv.extend(("-l", self.settings.user))
        argv.extend(("--", self.settings.host, command))
        result = _execute(argv)
        if result.returncode == 0:
            return result.stdout
        failure_class = (failures or {}).get(result.returncode, "other")
        if result.returncode == 255:
            failure_class = "connect"
        raise CopyCommandError(failure_class, _last_diagnostic(result))

    def transfer(self, source: Path, remote_path: str) -> None:
        if not _safe_remote_path(remote_path):
            raise ValueError("invalid SSH transfer remote path")
        host = self.settings.host
        if ":" in host:
            host = f"[{host}]"
        if self.settings.user is not None:
            host = f"{self.settings.user}@{host}"
        target = f"{host}:{remote_path}"
        argv = self._transfer_args()
        argv.extend(("--", str(source.absolute()), target))

        # Both transfer tools can hide SSH's exit status. Keep it out of the data streams
        # and separate from diagnostics so a refused key is classed without parsing prose.
        with tempfile.TemporaryDirectory(prefix="bdbackup-ssh-") as directory:
            status_path = Path(directory) / "status"
            result = _execute(argv, env={**os.environ, "BDBACKUP_SSH_STATUS": str(status_path)})
            if result.returncode == 0:
                return
            try:
                status = status_path.read_text(encoding="ascii").strip()
            except FileNotFoundError:
                status = ""
        failure_class = {"255": "connect", "missing": "tool_missing"}.get(status, "transfer")
        raise CopyCommandError(failure_class, _last_diagnostic(result))

    def _transfer_args(self) -> list[str]:
        helper = str(_SSH_STATUS_HELPER)
        bwlimit = self.settings.bwlimit
        if self.settings.method == "scp":
            argv = ["scp", "-S", helper, *self._options("-P")]
            if bwlimit is not None:
                argv.extend(("-l", str(bwlimit * 8)))
            return argv
        argv = ["rsync", "--timeout=30"]
        if bwlimit is not None:
            argv.append(f"--bwlimit={bwlimit}")
        remote_shell = " ".join(_rsync_word(arg) for arg in (helper, *self._options("-p")))
        argv.extend(("-e", remote_shell, "--inplace"))
        return argv


def _source_info(source: Path) -> os.stat_result:
    try:
        info = source.stat()
    except (FileNotFoundError, NotADirectoryError):
        raise CopyCommandError("source_unavailable", None) from None
    except OSError as exc:
        raise CopyCommandError("other", str(exc)) from None
    if not S_ISREG(info.st_mode):
        raise CopyCommandError("source_unavailable", None)
    return info


def _source_mode_after_transfer(source: Path, before: os.stat_result) -> int:
    try:
        after = source.stat()
    except OSError as exc:
        raise CopyCommandError("transfer", str(exc)) from None
    if (
        not os.path.samestat(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise CopyCommandError("transfer", "local artifact changed during SSH transfer")
    return S_IMODE(before.st_mode) & S_IMODE(after.st_mode)


def _existing_checksum(connection: SSHConnection, directory: str, final: str) -> str | None:
    commands = [
        f"test -d {directory} && test -w {directory} || exit 20",
        "command -v sha256sum >/dev/null || exit 21",
    ]
    if connection.settings.method == "rsync":
        commands.append("command -v rsync >/dev/null || exit 21")
    commands.append(
        f"if test -e {final} || test -L {final}; then "
        f"test -f {final} || exit 22; sha256sum -- {final} || exit 23; fi"
    )
    output = connection.run("; ".join(commands), failures=_REMOTE_FAILURES)
    return _read_checksum(output) if output.strip() else None


def _read_checksum(output: str) -> str:
    digest, *_ = output.split(maxsplit=1) or ("",)
    if _SHA256.fullmatch(digest) is None:
        raise CopyCommandError("other", "invalid remote SHA-256 output")
    return digest


def _read_mode(output: str) -> int:
    mode = output.strip()
    if _MODE.fullmatch(mode) is None:
        raise CopyCommandError("other", "invalid remote file mode")
    return int(mode, 8)


def _verify_staged(connection: SSHConnection, temp: str, checksum: str, allowed_mode: int) -> None:
    output = connection.run(
        f"test -f {temp} && test ! -L {temp} || exit 23; "
        f"sha256sum -- {temp} || exit 23; stat -c %a -- {temp} || exit 23"
    )
    digest, separator, mode = output.partition("\n")
    if _read_checksum(digest) != checksum:
        raise CopyCommandError("checksum_mismatch", None)
    if not separator:
        raise CopyCommandError("other", "missing remote file mode")
    remote_mode = _read_mode(mode)
    if remote_mode & ~allowed_mode:
        cap = remote_mode & allowed_mode
        actual = connection.run(f"chmod {cap:04o} {temp} && stat -c %a -- {temp}")
        if _read_mode(actual) & ~allowed_mode:
            raise CopyCommandError("other", "remote file mode exceeds local permissions")


def _publish(connection: SSHConnection, temp: str, final: str) -> None:
    connection.run(
        f"ln -T -- {temp} {final} || {{ "
        f"if test -e {final} || test -L {final}; then exit 22; fi; exit 23; }}",
        failures=_REMOTE_FAILURES,
    )
