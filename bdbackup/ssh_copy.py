"""Settings for copying a published backup to an SSH server."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path

_NAME_CHARS = r"A-Za-z0-9._+=@,%~\-"
_SAFE_NAME = re.compile(rf"[{_NAME_CHARS}]+")
_SAFE_PATH = re.compile(rf"/[{_NAME_CHARS}/]*")
_AUTHORITY = re.compile(
    r"(?:(?P<user>[A-Za-z0-9_.+-]+)@)?"
    r"(?P<host>[A-Za-z0-9_.][A-Za-z0-9_.-]*|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>[0-9]+))?"
)
_KEYS = {"destination", "method", "ssh_config", "bwlimit"}


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
    if _SAFE_PATH.fullmatch(path) is None or {".", ".."}.intersection(path.split("/")):
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
