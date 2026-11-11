"""Describe an extracted pgBackRest unit and print the command that restores it (spec 6.3).

Nothing here runs pgBackRest or writes anything. ``describe_unit`` reads the directory a
``restore`` extracted; ``restore_lines`` words the command and the points around it. Both the
history restore of a ``pgbackrest-full`` record and ``restore --archive`` use them, so the two
modes print the same text for the same unit.
"""

from __future__ import annotations

import re
import shlex
import textwrap
from dataclasses import dataclass
from pathlib import Path

from bdbackup.postgres.repository import LABEL, parse_info_file

# What keeps the restoring host's own pgbackrest.conf, and any stanza it defines, out of the
# restore. The integration test (spec 7) decides the final form; change only this constant.
NO_CONFIG = ("--no-config",)

PGDATA = "<PGDATA>"

# ``db-version`` is the PostgreSQL major version: "16", or "9.6" on a repository that old.
_PG_VERSION = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class UnitFacts:
    """What the printed command needs, read from the extracted tree."""

    stanza: str
    label: str
    pg_version: str
    page_errors: bool


def describe_unit(root: Path) -> UnitFacts | None:
    """The facts of the pgBackRest unit extracted at ``root``, or None if it is not one.

    A unit has exactly one ``backup/<stanza>/`` directory holding ``backup.info``, exactly one
    directory there named like a full backup label, and ``archive/<stanza>/archive.info``. The
    stanza and the label are the directory names; the PostgreSQL version and the page-error
    flag come from ``backup.info``. Anything unreadable or unexpected means "not a unit": this
    never raises. The tree can come out of any tar, so nothing in it may stop a restore that
    has already extracted.
    """
    try:
        return _describe(Path(root))
    except Exception:
        return None


def _subdirectories(parent: Path) -> list[Path]:
    return sorted(path for path in parent.iterdir() if path.is_dir())


def _describe(root: Path) -> UnitFacts | None:
    stanzas = [
        path for path in _subdirectories(root / "backup") if (path / "backup.info").is_file()
    ]
    if len(stanzas) != 1:
        return None
    backup_dir = stanzas[0]
    if not (root / "archive" / backup_dir.name / "archive.info").is_file():
        return None
    labels = [path.name for path in _subdirectories(backup_dir) if LABEL.fullmatch(path.name)]
    if len(labels) != 1:
        return None
    label = labels[0]

    sections = parse_info_file((backup_dir / "backup.info").read_text(encoding="utf-8"))
    version = sections.get("db", {}).get("db-version")
    entry = sections.get("backup:current", {}).get(label)
    if not isinstance(version, str) or not _PG_VERSION.fullmatch(version):
        return None
    if not isinstance(entry, dict):
        return None
    page_errors = entry.get("backup-error", False)
    if not isinstance(page_errors, bool):
        return None
    return UnitFacts(backup_dir.name, label, version, page_errors)


def restore_lines(facts: UnitFacts, repo_dir: Path) -> list[str]:
    """The restore command for the unit extracted at ``repo_dir``, and what to know around it.

    The command is the one line that starts with ``pgbackrest``; ``<PGDATA>`` in it is a
    placeholder for the person who runs it. Arguments that come from the extracted tree are
    shell-quoted, so a stanza name cannot add a command.
    """
    arguments = [
        "pgbackrest",
        *NO_CONFIG,
        shlex.quote(f"--stanza={facts.stanza}"),
        shlex.quote(f"--repo1-path={repo_dir}"),
        f"--pg1-path={PGDATA}",
        shlex.quote(f"--set={facts.label}"),
        "--type=none",
        "--log-level-file=off",
    ]
    if facts.page_errors:
        arguments.append("--checksum-page-error=n")
    arguments.append("restore")

    # Under --type=none pgBackRest writes no recovery settings, so --archive-mode=off has no
    # effect there (Task 10, pgBackRest 2.58.0 and 2.60.0); the copy turns archiving off itself.
    steps = [
        "Before the first start, as the cluster owner, turn archiving off in the copy:",
        f"printf '\\n%s\\n%s\\n' \"archive_mode = 'off'\" \"archive_command = ''\" "
        f">> {PGDATA}/postgresql.auto.conf",
        "",
        f"Check it with the PostgreSQL {facts.pg_version} server binary, without starting the "
        "server; it must print off:",
        f"postgres -D {PGDATA} -C archive_mode",
        "If it reports no postgresql.conf, the configuration was kept outside the data "
        "directory: see docs/README_PGBACKREST.md.",
        "",
        "Start the copy directly with pg_ctl as the cluster owner, never through a cluster "
        "manager or service unit that holds the production configuration:",
        f'pg_ctl -D {PGDATA} -o "-c listen_addresses=localhost -c port=<free port>" start',
    ]

    notes = [
        f"{PGDATA} is a new, empty directory on the host that restores.",
        "Run the command as the OS user that will own the cluster, never root. Unset every "
        "PGBACKREST_* environment variable first: pgBackRest reads them like options.",
        f"{repo_dir} belongs to the user who ran bdbackup (a history restore creates it with "
        "mode 0700). To restore as another user or on another host, copy it there first and "
        "change --repo1-path to the copy.",
        f"Start the restored cluster with PostgreSQL {facts.pg_version}.",
        "archive_mode must be off before the copy starts for the first time. With "
        "--type=none the copy stays on the production timeline, and one archived WAL segment "
        "would collide with production WAL. pgBackRest writes no recovery settings under "
        "--type=none, so the step after the command turns archiving off in "
        "postgresql.auto.conf, which is always read; the empty archive_command keeps the copy "
        "from archiving even if archive_mode is forced on at start.",
    ]
    if facts.page_errors:
        notes.append(
            "--checksum-page-error=n is there because this backup recorded invalid page "
            "checksums. pgBackRest 2.60.0 and later stop a restore on an invalid page checksum "
            "unless it is given; the restored cluster keeps those pages as they were."
        )

    lines = ["", "To restore this backup, run this pgBackRest command:", " ".join(arguments), ""]
    lines += [*steps, ""]
    for note in notes:
        lines += textwrap.wrap(
            note,
            width=96,
            initial_indent="  - ",
            subsequent_indent="    ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    return lines
