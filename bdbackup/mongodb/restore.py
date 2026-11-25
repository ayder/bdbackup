"""Recognize the two extracted PBM entries and describe the operator's restore steps."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bdbackup.mongodb.pbm import BACKUP_NAME

_SUFFIX = ".pbm.json"


@dataclass(frozen=True)
class UnitFacts:
    name: str
    backup_type: str


def describe_unit(root: Path) -> UnitFacts | None:
    """Read the name and type of an extracted PBM unit, if the two entries agree."""
    try:
        entries = tuple(root.iterdir())
        if len(entries) != 2:
            return None
        metadata = next((entry for entry in entries if entry.name.endswith(_SUFFIX)), None)
        if metadata is None or metadata.is_symlink() or not metadata.is_file():
            return None
        name = metadata.name.removesuffix(_SUFFIX)
        if not BACKUP_NAME.fullmatch(name):
            return None
        tree = root / name
        if tree.is_symlink() or not tree.is_dir():
            return None
        facts = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # An ordinary or damaged tar may contain a PBM-like filename.
        return None
    if not isinstance(facts, dict) or facts.get("name") != name:
        return None
    kind = facts.get("type")
    if kind not in ("logical", "physical"):
        return None
    return UnitFacts(name, kind)


def restore_lines(facts: UnitFacts) -> list[str]:
    """Explain how the operator restores the extracted backup with PBM."""
    name = facts.name
    lines = [
        "",
        f"To restore PBM {facts.backup_type} backup {name}, follow these steps.",
        f"1. Put {name}.pbm.json and {name}/ in filesystem storage mounted at the same path "
        "on every node of the target replica set, with PBM configured to use it.",
        "   The restore fails until every node can read both entries.",
        "2. Resync PBM's backup list with that storage.",
        "   pbm config --force-resync --wait",
        f"3. List the backups and confirm the list shows {name}.",
        "   pbm list",
        f"4. Restore the {facts.backup_type} backup and follow Percona's "
        "post-restore instructions.",
        f"   pbm restore {name}",
        '   If pbm restore answers "operation staled" just after resync, '
        "wait a few seconds and run it again.",
    ]
    if facts.backup_type == "physical":
        lines += [
            "   Restart mongod and the PBM agents on every node after the restore, then resync.",
            "   pbm config --force-resync",
            f"   Wait until pbm list shows {name} again.",
        ]
    return lines
