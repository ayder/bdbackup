"""Restoring a pgbackrest unit and printing the pgbackrest restore command (spec 6.3)."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.engines import _reset_for_tests
from bdbackup.filebackup import FileBackup
from bdbackup.gfs import store as gfs_store
from bdbackup.postgres.restore import NO_CONFIG, UnitFacts, describe_unit, restore_lines
from tests.pgbackrest_fake import make_repo
from tests.test_pgbackrest_cli import NEWEST, SOON, make_site
from tests.test_pgbackrest_export import OLDER, full, make_engine

NEWEST_FACTS = UnitFacts(stanza="main", label=NEWEST, pg_version="16", page_errors=False)


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    """Give each test a fresh registry isolated from the user's config dir."""
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


def command(repo_dir, label=NEWEST, stanza="main", page_errors=False) -> str:
    """The command line spec 6.3 prints, written out in full, independent of the code under test."""
    page = " --checksum-page-error=n" if page_errors else ""
    return (
        f"pgbackrest --no-config --stanza={stanza} --repo1-path={repo_dir} "
        f"--pg1-path=<PGDATA> --set={label} --type=none "
        f"--log-level-file=off{page} restore"
    )


# The steps after the command, written out in full (spec 6.3, as amended by the Task 10 ruling).
PRE_START = (
    "printf '\\n%s\\n%s\\n' \"archive_mode = 'off'\" \"archive_command = ''\" "
    ">> <PGDATA>/postgresql.auto.conf"
)
CHECK = "postgres -D <PGDATA> -C archive_mode"
START = 'pg_ctl -D <PGDATA> -o "-c listen_addresses=localhost -c port=<free port>" start'


def extracted(tmp_path, *, stanza="main", fulls=((NEWEST, True, False),)) -> Path:
    """Export the newest of ``fulls`` with the engine and extract the tar like ``restore`` does."""
    repo = make_repo(tmp_path, stanza=stanza, fulls=fulls)
    label = fulls[-1][0]
    unit = make_engine(tmp_path, repo, stanza=stanza).export(full(label)).path
    return FileBackup.restore_archive(unit, tmp_path / "unit")


def rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


# ---- describe_unit ---------------------------------------------------------------------------


def test_describe_unit_from_extracted_export(tmp_path):
    root = extracted(tmp_path, fulls=((OLDER, True, False), (NEWEST, True, False)))

    assert describe_unit(root) == NEWEST_FACTS


def test_describe_unit_hyphenated_stanza(tmp_path):
    root = extracted(tmp_path, stanza="main-db")

    facts = describe_unit(root)

    assert facts == UnitFacts(stanza="main-db", label=NEWEST, pg_version="16", page_errors=False)
    assert command(root, stanza="main-db") in restore_lines(facts, root)


def test_describe_unit_reads_page_errors_from_the_label_entry(tmp_path):
    root = extracted(tmp_path, fulls=((NEWEST, True, True),))

    assert describe_unit(root) == UnitFacts("main", NEWEST, "16", page_errors=True)


def test_describe_unit_missing_backup_error_key_is_false(tmp_path):
    root = extracted(tmp_path, fulls=((NEWEST, True, True),))
    rewrite(root / "backup" / "main" / "backup.info", '"backup-error":true,', "")

    assert describe_unit(root) == NEWEST_FACTS


def test_describe_unit_ignores_a_diff_and_files_beside_the_label(tmp_path):
    root = extracted(tmp_path)
    (root / "backup" / "main" / f"{NEWEST}_20261110-010000D").mkdir()
    (root / "backup" / "main" / "backup.history").mkdir()

    assert describe_unit(root) == NEWEST_FACTS


def test_describe_unit_rejects_plain_tar_tree(tmp_path):
    root = tmp_path / "plain"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "hello").write_text("recover me")

    assert describe_unit(root) is None
    assert describe_unit(tmp_path / "does-not-exist") is None
    assert describe_unit(root / "docs" / "hello") is None


def test_describe_unit_rejects_two_labels(tmp_path):
    root = extracted(tmp_path)
    (root / "backup" / "main" / OLDER).mkdir()

    assert describe_unit(root) is None


def test_describe_unit_rejects_no_label(tmp_path):
    root = extracted(tmp_path)
    shutil.rmtree(root / "backup" / "main" / NEWEST)

    assert describe_unit(root) is None


def test_describe_unit_rejects_two_stanzas(tmp_path):
    root = extracted(tmp_path)
    other = root / "backup" / "other"
    other.mkdir()
    (other / "backup.info").write_text((root / "backup" / "main" / "backup.info").read_text())

    assert describe_unit(root) is None


def test_describe_unit_rejects_a_missing_archive_info(tmp_path):
    root = extracted(tmp_path)
    (root / "archive" / "main" / "archive.info").unlink()

    assert describe_unit(root) is None


def test_describe_unit_rejects_a_missing_backup_info(tmp_path):
    root = extracted(tmp_path)
    (root / "backup" / "main" / "backup.info").unlink()

    assert describe_unit(root) is None


def _line(name: str, replacement: str):
    """A damage that replaces the whole ``name=...`` line of an info file."""
    return lambda text: re.sub(rf"^{re.escape(name)}=.*$", replacement, text, flags=re.MULTILINE)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda text: "this is not an info file\n", id="no-sections"),
        pytest.param(_line("db-version", "db-version=16 not json"), id="value-not-json"),
        pytest.param(_line("db-version", ""), id="no-db-version"),
        pytest.param(lambda text: text.replace('"16"', "[" * 200000), id="json-nested-too-deep"),
        pytest.param(_line("db-version", "db-version=16"), id="db-version-not-a-string"),
        pytest.param(lambda text: text.replace("[db]", "[db-gone]"), id="no-db-section"),
        pytest.param(lambda text: text.replace(NEWEST, "20200101-000000F"), id="label-not-listed"),
        pytest.param(lambda text: text.replace("[backup:current]\n", ""), id="no-current"),
        pytest.param(_line(NEWEST, f"{NEWEST}=[1]"), id="entry-not-an-object"),
        pytest.param(
            lambda text: text.replace('"backup-error":false', '"backup-error":"no"'),
            id="backup-error-not-a-bool",
        ),
    ],
)
def test_describe_unit_rejects_an_info_file_it_cannot_read(tmp_path, damage):
    root = extracted(tmp_path)
    info = root / "backup" / "main" / "backup.info"
    original = info.read_text()
    info.write_text(damage(original))
    assert info.read_text() != original

    assert describe_unit(root) is None


def test_describe_unit_rejects_a_backup_info_that_is_not_text(tmp_path):
    root = extracted(tmp_path)
    (root / "backup" / "main" / "backup.info").write_bytes(b"\xff\xfe\x00[db]\n")

    assert describe_unit(root) is None


def test_describe_unit_rejects_a_backup_info_that_is_a_directory(tmp_path):
    root = extracted(tmp_path)
    info = root / "backup" / "main" / "backup.info"
    info.unlink()
    info.mkdir()

    assert describe_unit(root) is None


# ---- restore_lines ---------------------------------------------------------------------------


def test_restore_lines_command_exact(tmp_path):
    lines = restore_lines(NEWEST_FACTS, tmp_path / "unit")

    assert [line for line in lines if line.startswith("pgbackrest ")] == [
        command(tmp_path / "unit")
    ]
    assert NO_CONFIG == ("--no-config",)


def test_restore_lines_page_errors(tmp_path):
    facts = UnitFacts("main", NEWEST, "16", page_errors=True)

    lines = restore_lines(facts, tmp_path / "unit")

    assert [line for line in lines if line.startswith("pgbackrest ")] == [
        command(tmp_path / "unit", page_errors=True)
    ]
    # The option is explained, and only when it is there.
    explained = [
        line for line in lines
        if "checksum-page-error" in line and not line.startswith("pgbackrest ")
    ]
    assert explained
    plain = restore_lines(NEWEST_FACTS, tmp_path / "unit")
    assert not any("checksum-page-error" in line for line in plain)


def test_restore_lines_say_how_to_run_the_command(tmp_path):
    text = "\n".join(restore_lines(NEWEST_FACTS, tmp_path / "unit"))

    assert "<PGDATA>" in text and "empty directory" in text
    assert "never root" in text
    assert "PGBACKREST_" in text and "unset" in text.lower()
    assert "0700" in text and "copy" in text and "--repo1-path" in text
    assert "PostgreSQL 16" in text
    assert "archive_mode" in text and "off" in text
    assert "production timeline" in text


def test_restore_lines_print_the_pre_start_steps_in_order(tmp_path):
    lines = restore_lines(NEWEST_FACTS, tmp_path / "unit")

    (command_at,) = [i for i, line in enumerate(lines) if line.startswith("pgbackrest ")]
    steps = [lines.index(PRE_START), lines.index(CHECK), lines.index(START)]
    assert command_at < steps[0] < steps[1] < steps[2]
    # Each step is introduced by the line before it.
    assert "before the first start" in lines[steps[0] - 1].lower()
    assert "cluster owner" in lines[steps[0] - 1]
    assert "PostgreSQL 16" in lines[steps[1] - 1] and "off" in lines[steps[1] - 1]
    assert "pg_ctl" in lines[steps[2] - 1] and "cluster owner" in lines[steps[2] - 1]
    assert "service unit" in lines[steps[2] - 1]


def test_restore_lines_check_names_the_backups_postgresql_version(tmp_path):
    facts = UnitFacts("main", NEWEST, "18", page_errors=False)
    lines = restore_lines(facts, tmp_path / "unit")

    assert "PostgreSQL 18" in lines[lines.index(CHECK) - 1]


def test_restore_lines_no_archive_mode_option_and_no_signal_file(tmp_path):
    text = "\n".join(restore_lines(NEWEST_FACTS, tmp_path / "unit"))

    # Under --type=none pgBackRest writes no recovery settings: the option has no effect.
    assert "--archive-mode" not in text
    assert "The command passes" not in text
    assert "signal" not in text


def test_restore_lines_name_the_postgresql_version(tmp_path):
    facts = UnitFacts("main", NEWEST, "18", page_errors=False)

    assert "PostgreSQL 18" in "\n".join(restore_lines(facts, tmp_path / "unit"))


def test_restore_lines_quote_what_the_shell_would_read(tmp_path):
    # A stanza is a directory name in a tar that may have come from anywhere.
    facts = UnitFacts("a b;$(touch x)", NEWEST, "16", page_errors=False)
    repo_dir = tmp_path / "my unit"

    (line,) = [x for x in restore_lines(facts, repo_dir) if x.startswith("pgbackrest ")]

    assert shlex.split(line) == [
        "pgbackrest",
        "--no-config",
        "--stanza=a b;$(touch x)",
        f"--repo1-path={repo_dir}",
        "--pg1-path=<PGDATA>",
        f"--set={NEWEST}",
        "--type=none",
        "--log-level-file=off",
        "restore",
    ]


# ---- the restore command ---------------------------------------------------------------------


def manage(site) -> None:
    """Let the GFS job take the unit over: from then on restore verifies it against the ledger."""
    with patch("bdbackup.cli._now", return_value=SOON):
        result = CliRunner().invoke(main, ["run", "-c", str(site.config), "-j", "gfs-main"])
    assert result.exit_code == 0, result.output
    assert gfs_store.places(site.history)


def restore_history(site, destination):
    return CliRunner().invoke(
        main,
        ["restore", "-c", str(site.config), "--backup-id", "1", "--yes", "--dst", str(destination)],
    )


@pytest.mark.parametrize("managed", [False, True], ids=["unit-only", "managed-by-gfs"])
def test_history_restore_prints_command(tmp_path, managed):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    if managed:
        manage(site)
    destination = tmp_path / "recovered"

    result = restore_history(site, destination)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert f"Restored to: {destination}" in lines
    assert lines.count(command(destination)) == 1
    assert lines.index(f"Restored to: {destination}") < lines.index(command(destination))
    assert lines.count(PRE_START) == lines.count(CHECK) == lines.count(START) == 1
    # What is printed is what was restored: the tree `describe_unit` read is the pgBackRest
    # repository the command points at.
    assert (destination / "backup" / "main" / "backup.info").is_file()
    assert (destination / "archive" / "main" / "archive.info").is_file()
    assert (destination / "backup" / "main" / NEWEST / "backup.manifest").is_file()


def test_history_restore_verifies_the_unit_before_extracting(tmp_path, caplog):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    manage(site)
    # Same size, same mtime: only the ledger checksum can tell.
    before = site.unit.stat()
    data = bytearray(site.unit.read_bytes())
    data[-1] ^= 0xFF
    site.unit.write_bytes(bytes(data))
    os.utime(site.unit, ns=(before.st_atime_ns, before.st_mtime_ns))
    destination = tmp_path / "recovered"

    result = restore_history(site, destination)

    assert result.exit_code == 1
    assert "checksum mismatch" in caplog.text
    assert not destination.exists()
    assert "pgbackrest " not in result.output


def test_archive_restore_prints_command(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    destination = tmp_path / "from-archive"

    result = CliRunner().invoke(
        main, ["restore", "--archive", str(site.unit), "--dst", str(destination)]
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"Restored to: {destination}"
    assert lines.count(command(destination)) == 1
    assert lines.count(PRE_START) == lines.count(CHECK) == lines.count(START) == 1


def test_archive_and_history_restore_print_the_same_text(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    from_history, from_archive = tmp_path / "one", tmp_path / "two"

    first = restore_history(site, from_history)
    second = CliRunner().invoke(
        main, ["restore", "--archive", str(site.unit), "--dst", str(from_archive)]
    )

    def after_restored(result, destination):
        lines = result.output.replace(str(destination), "DESTINATION").splitlines()
        return lines[lines.index("Restored to: DESTINATION") + 1:]

    assert first.exit_code == 0 and second.exit_code == 0
    assert after_restored(first, from_history) == after_restored(second, from_archive)
    assert len(after_restored(second, from_archive)) > 2


def test_archive_restore_names_the_destination_in_full(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        main, ["restore", "--archive", str(site.unit), "--dst", "relative-unit"]
    )

    assert result.exit_code == 0, result.output
    assert command(tmp_path / "relative-unit") in result.output.splitlines()


def test_archive_restore_page_errors_flag(tmp_path):
    site = make_site(tmp_path, fulls=((NEWEST, True, True),))
    assert site.run().exit_code == 0
    destination = tmp_path / "damaged"

    result = CliRunner().invoke(
        main, ["restore", "--archive", str(site.unit), "--dst", str(destination)]
    )

    assert result.exit_code == 0, result.output
    assert command(destination, page_errors=True) in result.output.splitlines()


def test_archive_restore_plain_tar_prints_nothing_new(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "hello").write_text("recover me")
    (tmp_path / "paths").write_text("hello\n")
    archive = FileBackup(
        tmp_path / "made", tmp_path / "paths", chdir=tmp_path / "source", format="tar.gz"
    ).backup().path
    destination = tmp_path / "out"

    result = CliRunner().invoke(
        main, ["restore", "--archive", str(archive), "--dst", str(destination)]
    )

    assert result.exit_code == 0, result.output
    assert result.output == f"Restored to: {destination}\n"
    assert (destination / "hello").read_text() == "recover me"
