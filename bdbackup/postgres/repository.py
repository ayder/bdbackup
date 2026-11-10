"""Read a pgBackRest repository: its info output, its info files and a full backup directory.

Pure reading. Nothing here writes to the repository or runs a subprocess, and every file under
the repository is opened read-only.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bdbackup.backends import BackupError

ENCRYPTED = "encrypted repositories are not supported in 0.7.3"

LABEL = re.compile(r"\d{8}-\d{6}F")


class RepositoryError(BackupError):
    """Raised when a pgBackRest repository cannot be read safely."""


@dataclass(frozen=True)
class FullBackup:
    label: str
    stop: datetime  # UTC-aware
    pg_version: str
    page_errors: bool
    repo_bytes: int


@dataclass(frozen=True)
class InfoPair:
    relative: str  # of the main file, for example backup/main/backup.info
    main: bytes
    copy: bytes
    main_stat: os.stat_result
    copy_stat: os.stat_result


def is_encrypted(entry: dict) -> bool:
    """Whether a stanza of the ``info --output=json`` document names a cipher on any repository."""
    return any(repo.get("cipher", "none") != "none" for repo in entry.get("repo", []))


def newest_full(info: list[dict], stanza: str) -> FullBackup | None:
    """The newest full backup of ``stanza`` in the parsed ``info --output=json`` document.

    "Newest" is by stop time; diffs and incrementals are ignored. Returns None when the stanza
    has no full. Raises RepositoryError when the stanza is not in the output or when the
    repository is encrypted.
    """
    entry = next((item for item in info if item.get("name") == stanza), None)
    if entry is None:
        raise RepositoryError(f"stanza {stanza!r} is not in the pgbackrest info output")
    if is_encrypted(entry):
        raise RepositoryError(ENCRYPTED)
    fulls = [backup for backup in entry.get("backup", []) if backup.get("type") == "full"]
    if not fulls:
        return None
    try:
        newest = max(fulls, key=lambda backup: (backup["timestamp"]["stop"], backup["label"]))
        label = newest["label"]
        if not LABEL.fullmatch(label):
            raise RepositoryError(f"pgbackrest info lists a full with the invalid label {label!r}")
        database_id = newest["database"]["id"]
        versions = [db["version"] for db in entry["db"] if db["id"] == database_id]
        if not versions:
            raise RepositoryError(f"pgbackrest info lists no database {database_id} for {label}")
        return FullBackup(
            label=label,
            stop=datetime.fromtimestamp(newest["timestamp"]["stop"], UTC),
            pg_version=versions[0],
            page_errors=bool(newest.get("error", False)),
            repo_bytes=int(newest["info"]["repository"]["size"]),
        )
    except (KeyError, TypeError) as exc:
        raise RepositoryError(f"unexpected pgbackrest info output: {exc!r}") from exc


def parse_info_file(text: str) -> dict[str, dict[str, object]]:
    """Parse pgBackRest's info file text into ``{section: {key: JSON-decoded value}}``.

    ``configparser`` is no use here: it lowercases keys (a label ends in ``F``) and mangles
    JSON values. The error messages never quote file content, which can hold secrets.
    """
    sections: dict[str, dict[str, object]] = {}
    current: dict[str, object] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, separator, value = line.partition("=")
        if current is None or not separator:
            raise RepositoryError(f"info file line {number} is not a key=value line in a section")
        try:
            current[key] = json.loads(value)
        except ValueError as exc:
            raise RepositoryError(f"info file line {number} has a value that is not JSON") from exc
    return sections


def archive_copy(backup_info: dict, label: str) -> bool:
    """Whether ``label`` was taken with archive-copy, per the parsed ``backup.info``."""
    entry = backup_info.get("backup:current", {}).get(label)
    if entry is None:
        raise RepositoryError(f"backup.info does not list {label}")
    return entry.get("option-archive-copy") is True


def _read_stable(path: Path) -> tuple[bytes, os.stat_result] | None:
    """The bytes and stat of ``path``, or None if it changed between a stat and the next."""
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise RepositoryError(f"cannot read {path}: {exc.strerror}") from exc
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        return None
    return data, before


def read_info_pair(
    repo: Path,
    relative: str,
    *,
    label: str | None = None,
    attempts: int = 5,
    wait: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> InfoPair:
    """Read an info file and its ``.copy`` as one consistent pair.

    A pair is accepted when the two files are byte-identical, neither changed size, mtime or
    inode across its read and, if ``label`` is given, the file still lists that backup. A pair
    that fails the first two is read again, up to ``attempts`` tries in all with ``wait``
    seconds between them (never after the last). A consistent pair that no longer lists
    ``label`` fails at once: waiting cannot bring the label back.
    """
    main_path = repo / relative
    copy_path = repo / f"{relative}.copy"
    problem = "no attempt was made"
    for attempt in range(attempts):
        if attempt:
            sleep(wait)
        main = _read_stable(main_path)
        copy = _read_stable(copy_path)
        if main is None or copy is None:
            problem = "a file changed while it was read"
        elif main[0] != copy[0]:
            problem = "the file and its .copy differ"
        else:
            if label is not None:
                try:
                    listed = parse_info_file(main[0].decode("utf-8")).get("backup:current", {})
                except UnicodeDecodeError as exc:
                    raise RepositoryError(f"{relative} is not UTF-8 text") from exc
                if label not in listed:
                    raise RepositoryError(f"{label} is no longer listed in {relative}")
            return InfoPair(relative, main[0], copy[0], main[1], copy[1])
    raise RepositoryError(
        f"{relative} and {relative}.copy were not consistent after {attempts} attempts: {problem}"
    )


def _walk(directory: Path, found: list[Path]) -> None:
    try:
        with os.scandir(directory) as scan:
            children = [
                (Path(item.path), item.stat(follow_symlinks=False).st_mode) for item in scan
            ]
    except OSError as exc:
        raise RepositoryError(f"cannot read {directory}: {exc.strerror}") from exc
    for path, mode in children:
        if stat.S_ISDIR(mode):
            found.append(path)
            _walk(path, found)
        elif stat.S_ISREG(mode):
            found.append(path)
        else:
            kind = "a symbolic link" if stat.S_ISLNK(mode) else "not a regular file or directory"
            raise RepositoryError(f"{path} is {kind}; only files and directories can be exported")


def full_entries(repo: Path, stanza: str, label: str) -> list[Path]:
    """The label directory ``backup/<stanza>/<label>/`` and everything under it, sorted.

    Directories are listed (so empty ones are kept) along with regular files, sorted by their
    POSIX path relative to ``repo``; the label directory itself comes first. Any other entry
    type, a symbolic link included, raises RepositoryError, as does a missing label directory.
    """
    root = repo / "backup" / stanza / label
    try:
        mode = root.lstat().st_mode
    except OSError as exc:
        raise RepositoryError(f"cannot read the full {label} at {root}: {exc.strerror}") from exc
    if not stat.S_ISDIR(mode):
        raise RepositoryError(f"the full {label} at {root} is not a directory")
    found = [root]
    _walk(root, found)
    return sorted(found, key=lambda path: path.relative_to(repo).as_posix())
