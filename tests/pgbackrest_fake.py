"""A fake pgBackRest repository and command runner, shared by the pgBackRest engine tests.

``make_repo`` writes a repository tree on disk; ``info_json`` reads it back as the
``pgbackrest info --output=json`` document; ``fake_pgbackrest`` answers the engine's
``version`` and ``info`` commands from it, in place of ``subprocess.run``.

The info files are parsed here by a few lines of this module's own, never by
``bdbackup.postgres.repository``, so a bug in the code under test cannot hide in its fake.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

CIPHER = "aes-256-cbc"
STOP_AFTER_START = 60  # seconds a fake backup takes

_BACKUP_TYPES = {"F": "full", "D": "diff", "I": "incr"}


def _label_start(label: str) -> int:
    """Start of a backup label (the last timestamp in it) as epoch seconds."""
    stamp = label.rsplit("_", 1)[-1][:15]
    return int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC).timestamp())


def _dump(sections: dict[str, dict[str, object]]) -> str:
    """Render the info file text format: ``[section]`` lines, then ``key=<JSON value>`` lines."""
    blocks = []
    for name, keys in sections.items():
        lines = [f"[{name}]"]
        lines += [
            f"{key}={json.dumps(value, separators=(',', ':'))}" for key, value in keys.items()
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _load(path: Path) -> dict[str, dict[str, object]]:
    sections: dict[str, dict[str, object]] = {}
    current: dict[str, object] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        if line.startswith("["):
            current = sections.setdefault(line[1:-1], {})
        else:
            key, value = line.split("=", 1)
            current[key] = json.loads(value)
    return sections


def _write_pair(directory: Path, name: str, text: str) -> None:
    """Write an info file and its ``.copy``, identical, as pgBackRest does."""
    (directory / name).write_text(text)
    (directory / f"{name}.copy").write_text(text)


def make_repo(
    root,
    stanza: str = "main",
    fulls=(("20261109-010000F", True, False),),
    cipher: bool = False,
) -> Path:
    """Write a repository under ``root / "repo"`` and return that path.

    Each entry of ``fulls`` is ``(label, archive_copy, page_error)``. The backup type comes from
    the label's last letter (``F`` full, ``D`` diff, ``I`` incr), so a diff is a label such as
    ``20261109-010000F_20261110-010000D``. Entries are listed in the order given, and a backup
    stops ``STOP_AFTER_START`` seconds after its label's time. Every label gets a
    ``backup.manifest`` and two data files, one of them in a nested directory.
    """
    repo = Path(root) / "repo"
    backup_dir = repo / "backup" / stanza
    archive_dir = repo / "archive" / stanza
    backup_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)

    current: dict[str, object] = {}
    for label, archive_copy, page_error in fulls:
        label_dir = backup_dir / label
        (label_dir / "pg_data" / "base" / "1").mkdir(parents=True)
        (label_dir / "backup.manifest").write_text(f'[backup]\nbackup-label="{label}"\n')
        (label_dir / "pg_data" / "PG_VERSION.gz").write_bytes(f"{label} PG_VERSION".encode())
        (label_dir / "pg_data" / "base" / "1" / "1259.gz").write_bytes(f"{label} 1259".encode())
        start = _label_start(label)
        current[label] = {
            "backup-error": page_error,
            "backup-info-repo-size": sum(
                f.stat().st_size for f in label_dir.rglob("*") if f.is_file()
            ),
            "backup-timestamp-start": start,
            "backup-timestamp-stop": start + STOP_AFTER_START,
            "backup-type": _BACKUP_TYPES[label[-1]],
            "db-id": 1,
            "option-archive-copy": archive_copy,
            "option-backup-standby": True,
            "option-checksum-page": True,
        }

    header: dict[str, object] = {
        "backrest-checksum": "0" * 40,
        "backrest-format": 5,
        "backrest-version": "2.58.0",
    }
    db: dict[str, object] = {"db-id": 1, "db-version": "16"}
    backup_sections: dict[str, dict[str, object]] = {
        "backrest": header,
        "backup:current": current,
    }
    if cipher:
        backup_sections["cipher"] = {"cipher-pass": "not-a-real-passphrase"}
    backup_sections["db"] = db
    _write_pair(backup_dir, "backup.info", _dump(backup_sections))
    _write_pair(archive_dir, "archive.info", _dump({"backrest": header, "db": db}))
    return repo


def info_json(repo, stanza: str) -> list[dict]:
    """The ``pgbackrest info --output=json`` document for ``stanza``, read from ``repo``."""
    sections = _load(Path(repo) / "backup" / stanza / "backup.info")
    backups = [
        {
            "label": label,
            "type": entry["backup-type"],
            "timestamp": {
                "start": entry["backup-timestamp-start"],
                "stop": entry["backup-timestamp-stop"],
            },
            "error": entry["backup-error"],
            "info": {"repository": {"size": entry["backup-info-repo-size"]}},
            "database": {"id": entry["db-id"]},
        }
        for label, entry in sections["backup:current"].items()
    ]
    db = sections["db"]
    return [
        {
            "name": stanza,
            "repo": [{"key": 1, "cipher": CIPHER if "cipher" in sections else "none"}],
            "db": [{"id": db["db-id"], "version": db["db-version"]}],
            "backup": backups,
        }
    ]


def fake_pgbackrest(repo, version: str = "2.58.0"):
    """A stand-in for ``subprocess.run`` that answers ``version`` and ``info --output=json``.

    Every command it receives is appended to ``run.calls``. Any other pgBackRest command fails
    the test with an ``AssertionError``.
    """

    def run(cmd, **kwargs):
        args = [str(arg) for arg in cmd]
        run.calls.append(args)
        command = next(arg for arg in args[1:] if not arg.startswith("-"))
        if command == "version":
            stdout = f"pgBackRest {version}\n"
        elif command == "info":
            stanza = next(a.split("=", 1)[1] for a in args if a.startswith("--stanza="))
            stdout = json.dumps(info_json(repo, stanza))
        else:
            raise AssertionError(f"unexpected pgbackrest command: {args}")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    run.calls = []
    return run
