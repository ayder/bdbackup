"""Reading a pgBackRest repository: info output, info files and a full backup directory."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bdbackup.backends import BackupError
from bdbackup.postgres.repository import (
    LABEL,
    FullBackup,
    RepositoryError,
    archive_copy,
    full_entries,
    newest_full,
    parse_info_file,
    read_info_pair,
)
from tests.pgbackrest_fake import fake_pgbackrest, info_json, make_repo, missing_stanza_info

OLDER = "20261108-010000F"
FULL = "20261109-010000F"
DIFF = "20261109-010000F_20261110-010000D"
GONE = "20250101-000000F"
BACKUP_INFO = "backup/main/backup.info"
ARCHIVE_INFO = "archive/main/archive.info"

# The text format from the Task 3 brief, verbatim.
INFO_TEXT = """[backrest]
backrest-checksum="abc123"
backrest-format=5
backrest-version="2.58.0"

[backup:current]
20261109-010000F={"backup-error":false,"backup-type":"full","option-archive-copy":true,"option-backup-standby":true,"option-checksum-page":true}

[db]
db-id=1
db-version="16"
"""


def test_repository_error_is_a_backup_error():
    assert issubclass(RepositoryError, BackupError)


def test_label_matches_full_labels_only():
    assert LABEL.fullmatch(FULL)
    assert not LABEL.fullmatch(DIFF)
    assert not LABEL.fullmatch("20261109-010000I")


# ---- newest_full ----------------------------------------------------------------------------


def test_newest_full_picks_latest_stop_and_ignores_diffs(tmp_path):
    # Listed oldest first with the newest backup overall (a diff) last, so neither the first nor
    # the last entry is the answer.
    repo = make_repo(
        tmp_path, fulls=((OLDER, True, False), (FULL, True, False), (DIFF, True, False))
    )

    full = newest_full(info_json(repo, "main"), "main")

    assert full is not None
    assert full.label == FULL


def test_newest_full_orders_by_stop_not_label(tmp_path):
    repo = make_repo(tmp_path, fulls=((OLDER, True, False), (FULL, True, False)))
    info = info_json(repo, "main")
    for backup in info[0]["backup"]:
        if backup["label"] == OLDER:
            backup["timestamp"]["stop"] += 7 * 86400

    assert newest_full(info, "main").label == OLDER


def test_newest_full_none_without_full(tmp_path):
    assert newest_full(info_json(make_repo(tmp_path, fulls=()), "main"), "main") is None


def test_newest_full_none_with_only_a_diff(tmp_path):
    repo = make_repo(tmp_path, fulls=((DIFF, True, False),))
    assert newest_full(info_json(repo, "main"), "main") is None


def test_newest_full_refuses_cipher(tmp_path):
    repo = make_repo(tmp_path, cipher=True)

    with pytest.raises(RepositoryError) as refused:
        newest_full(info_json(repo, "main"), "main")

    assert str(refused.value) == "encrypted repositories are not supported in 0.7.3"


def test_newest_full_unknown_stanza(tmp_path):
    repo = make_repo(tmp_path)

    with pytest.raises(RepositoryError, match="other"):
        newest_full(info_json(repo, "main"), "other")
    with pytest.raises(RepositoryError, match="other"):
        newest_full([], "other")


def test_newest_full_refuses_a_stanza_info_reports_missing():
    # Real pgBackRest lists an unknown stanza, with exit 0, as status 1.
    with pytest.raises(RepositoryError) as refused:
        newest_full(missing_stanza_info("nosuch"), "nosuch")

    assert str(refused.value) == (
        "stanza 'nosuch': pgbackrest info reports status 1: missing stanza path"
    )


@pytest.mark.parametrize("code", [3, 4, 5, 99])
def test_newest_full_refuses_any_other_problem_status(tmp_path, code):
    info = info_json(make_repo(tmp_path), "main")
    info[0]["status"] = {"code": code, "message": "something is wrong"}

    with pytest.raises(RepositoryError, match=f"status {code}: something is wrong"):
        newest_full(info, "main")


def test_newest_full_status_no_valid_backups_is_no_full(tmp_path):
    info = info_json(make_repo(tmp_path, fulls=()), "main")
    assert info[0]["status"]["code"] == 2

    assert newest_full(info, "main") is None


def test_newest_full_reads_page_errors_and_version(tmp_path):
    clean = make_repo(tmp_path / "clean")
    dirty = make_repo(tmp_path / "dirty", fulls=((FULL, True, True),))

    assert newest_full(info_json(clean, "main"), "main").page_errors is False
    full = newest_full(info_json(dirty, "main"), "main")
    assert full.page_errors is True
    assert full.pg_version == "16"


def test_newest_full_reports_stop_in_utc_and_repository_size(tmp_path):
    repo = make_repo(tmp_path)
    info = info_json(repo, "main")
    info[0]["backup"][0]["timestamp"]["stop"] = 1_700_000_000

    full = newest_full(info, "main")

    assert full == FullBackup(
        label=FULL,
        stop=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
        pg_version="16",
        page_errors=False,
        repo_bytes=sum(
            f.stat().st_size for f in (repo / "backup/main" / FULL).rglob("*") if f.is_file()
        ),
    )
    assert full.stop.utcoffset().total_seconds() == 0


def test_newest_full_takes_the_version_of_the_backups_database(tmp_path):
    info = info_json(make_repo(tmp_path), "main")
    info[0]["db"] = [
        {"id": 1, "version": "15"},
        {"id": 2, "version": "17"},
    ]
    info[0]["backup"][0]["database"]["id"] = 2

    assert newest_full(info, "main").pg_version == "17"


def test_newest_full_refuses_a_label_that_is_not_a_full_label(tmp_path):
    info = info_json(make_repo(tmp_path), "main")
    info[0]["backup"][0]["label"] = "../../etc"

    with pytest.raises(RepositoryError, match="label"):
        newest_full(info, "main")


# ---- parse_info_file and archive_copy -------------------------------------------------------


def test_parse_info_file_keeps_keys_and_decodes_json_values():
    parsed = parse_info_file(INFO_TEXT)

    assert list(parsed) == ["backrest", "backup:current", "db"]
    assert parsed["backrest"] == {
        "backrest-checksum": "abc123",
        "backrest-format": 5,
        "backrest-version": "2.58.0",
    }
    # The label key keeps its capital F, and its value is a decoded object.
    assert parsed["backup:current"][FULL]["option-archive-copy"] is True
    assert parsed["backup:current"][FULL]["backup-type"] == "full"
    assert parsed["db"] == {"db-id": 1, "db-version": "16"}


def test_parse_info_file_splits_on_the_first_equals_only():
    parsed = parse_info_file('[s]\nkey={"a":"b=c"}\n\n')

    assert parsed == {"s": {"key": {"a": "b=c"}}}


@pytest.mark.parametrize(
    "text",
    ['key="orphan"\n', "[s]\nkey=not json\n", "[s]\nno equals sign\n"],
    ids=["key-outside-section", "value-not-json", "line-without-equals"],
)
def test_parse_info_file_refuses_malformed_text(text):
    with pytest.raises(RepositoryError):
        parse_info_file(text)


def test_parse_info_file_refuses_json_nested_too_deep():
    # json.loads raises RecursionError, not ValueError, for this; an info file can come from a tar.
    with pytest.raises(RepositoryError) as caught:
        parse_info_file("[s]\nkey=" + "[" * 200000 + "\n")

    assert "[[" not in str(caught.value)


def test_parse_info_file_reads_what_the_fake_writes(tmp_path):
    repo = make_repo(tmp_path, fulls=((OLDER, True, False), (FULL, True, True)))

    parsed = parse_info_file((repo / BACKUP_INFO).read_text())

    assert list(parsed["backup:current"]) == [OLDER, FULL]
    assert parsed["backup:current"][FULL]["backup-error"] is True


def test_archive_copy_flag(tmp_path):
    repo = make_repo(tmp_path, fulls=((OLDER, False, False), (FULL, True, False)))
    backup_info = parse_info_file((repo / BACKUP_INFO).read_text())

    assert archive_copy(backup_info, FULL) is True
    assert archive_copy(backup_info, OLDER) is False


def test_archive_copy_is_false_when_the_flag_is_absent():
    assert archive_copy({"backup:current": {FULL: {}}}, FULL) is False


def test_archive_copy_refuses_a_label_that_is_not_listed(tmp_path):
    backup_info = parse_info_file((make_repo(tmp_path) / BACKUP_INFO).read_text())

    with pytest.raises(RepositoryError, match=GONE):
        archive_copy(backup_info, GONE)


# ---- read_info_pair -------------------------------------------------------------------------


def spoil_copy(repo: Path) -> tuple[Path, bytes]:
    """Make backup.info.copy differ from backup.info; return the copy and its good bytes."""
    copy = repo / f"{BACKUP_INFO}.copy"
    good = copy.read_bytes()
    copy.write_bytes(good + b"# half written\n")
    return copy, good


def test_read_info_pair_returns_both_files_and_their_stats(tmp_path):
    repo = make_repo(tmp_path)

    pair = read_info_pair(repo, BACKUP_INFO, label=FULL)

    assert pair.relative == BACKUP_INFO
    assert pair.main == pair.copy == (repo / BACKUP_INFO).read_bytes()
    assert pair.main_stat.st_size == len(pair.main)
    assert pair.copy_stat.st_size == len(pair.copy)
    assert pair.main_stat.st_ino == (repo / BACKUP_INFO).stat().st_ino
    assert pair.copy_stat.st_ino == (repo / f"{BACKUP_INFO}.copy").stat().st_ino


def test_read_info_pair_without_label_checks_only_consistency(tmp_path):
    repo = make_repo(tmp_path)

    pair = read_info_pair(repo, ARCHIVE_INFO)

    assert pair.relative == ARCHIVE_INFO
    assert pair.main == pair.copy == (repo / ARCHIVE_INFO).read_bytes()


def test_read_info_pair_retries_until_consistent(tmp_path):
    repo = make_repo(tmp_path)
    copy, good = spoil_copy(repo)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        copy.write_bytes(good)  # the writer finishes while we wait

    pair = read_info_pair(repo, BACKUP_INFO, label=FULL, wait=0.25, sleep=sleep)

    # One sleep means the first attempt failed and the second (the second attempt) passed.
    assert sleeps == [0.25]
    assert pair.main == pair.copy == good


def test_read_info_pair_gives_up_after_attempts(tmp_path):
    repo = make_repo(tmp_path)
    spoil_copy(repo)
    sleeps = []

    with pytest.raises(RepositoryError, match=BACKUP_INFO):
        read_info_pair(repo, BACKUP_INFO, label=FULL, attempts=3, wait=2.0, sleep=sleeps.append)

    # Three attempts wait twice: never after the last one.
    assert sleeps == [2.0, 2.0]


def test_read_info_pair_with_one_attempt_never_sleeps(tmp_path):
    repo = make_repo(tmp_path)
    spoil_copy(repo)
    sleeps = []

    with pytest.raises(RepositoryError):
        read_info_pair(repo, BACKUP_INFO, attempts=1, sleep=sleeps.append)

    assert sleeps == []


def test_read_info_pair_fails_when_label_gone(tmp_path):
    repo = make_repo(tmp_path)
    sleeps = []

    with pytest.raises(RepositoryError, match=GONE):
        read_info_pair(repo, BACKUP_INFO, label=GONE, sleep=sleeps.append)

    # A consistent pair that lacks the label will not get better by waiting.
    assert sleeps == []


@pytest.mark.parametrize("change", ["mtime", "inode"])
def test_read_info_pair_retries_when_a_file_changes_during_the_read(tmp_path, monkeypatch, change):
    repo = make_repo(tmp_path)
    main = repo / BACKUP_INFO
    read_bytes = Path.read_bytes
    changed = []

    def read_then_change(path):
        data = read_bytes(path)
        if path == main and not changed:
            changed.append(change)
            if change == "mtime":
                info = main.stat()
                os.utime(main, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
            else:
                replacement = main.with_name("replacement")
                replacement.write_bytes(data)
                os.replace(replacement, main)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_change)
    sleeps = []

    pair = read_info_pair(repo, BACKUP_INFO, label=FULL, wait=0.5, sleep=sleeps.append)

    # The bytes were identical all along; only the stat comparison can have noticed.
    assert changed == [change]
    assert sleeps == [0.5]
    assert pair.main == pair.copy


def test_read_info_pair_fails_when_a_file_is_missing(tmp_path):
    repo = make_repo(tmp_path)
    (repo / f"{BACKUP_INFO}.copy").unlink()

    with pytest.raises(RepositoryError, match=r"backup\.info\.copy"):
        read_info_pair(repo, BACKUP_INFO, label=FULL, sleep=lambda seconds: None)


# ---- full_entries ---------------------------------------------------------------------------


def test_full_entries_lists_directories_and_files_sorted(tmp_path):
    repo = make_repo(tmp_path)
    label_dir = repo / "backup/main" / FULL
    (label_dir / "pg_data" / "empty").mkdir()

    entries = full_entries(repo, "main", FULL)

    relative = [entry.relative_to(repo).as_posix() for entry in entries]
    assert relative == sorted(relative)
    assert all(entry.is_dir() or entry.is_file() for entry in entries)
    # The label directory itself, every file and every directory, empty ones included.
    expected = [label_dir, *label_dir.rglob("*")]
    assert sorted(relative) == sorted(p.relative_to(repo).as_posix() for p in expected)
    assert f"backup/main/{FULL}" in relative
    assert f"backup/main/{FULL}/backup.manifest" in relative
    assert f"backup/main/{FULL}/pg_data/empty" in relative


def test_full_entries_returns_paths_under_the_repository(tmp_path):
    repo = make_repo(tmp_path)

    entries = full_entries(repo, "main", FULL)

    assert entries[0] == repo / "backup/main" / FULL
    assert all(entry.is_relative_to(repo) for entry in entries)


@pytest.mark.parametrize("target", ["file", "directory"])
def test_full_entries_refuses_symlink(tmp_path, target):
    repo = make_repo(tmp_path)
    link = repo / "backup/main" / FULL / "pg_data" / "link"
    link.symlink_to(repo / BACKUP_INFO if target == "file" else repo / "archive")

    with pytest.raises(RepositoryError, match="link"):
        full_entries(repo, "main", FULL)


def test_full_entries_refuses_a_symlinked_label_directory(tmp_path):
    repo = make_repo(tmp_path)
    label_dir = repo / "backup/main" / FULL
    moved = repo / "elsewhere"
    label_dir.rename(moved)
    label_dir.symlink_to(moved)

    with pytest.raises(RepositoryError, match=FULL):
        full_entries(repo, "main", FULL)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_full_entries_refuses_other_entry_types(tmp_path):
    repo = make_repo(tmp_path)
    os.mkfifo(repo / "backup/main" / FULL / "pg_data" / "pipe")

    with pytest.raises(RepositoryError, match="pipe"):
        full_entries(repo, "main", FULL)


def test_full_entries_fails_when_the_full_is_gone(tmp_path):
    repo = make_repo(tmp_path)

    with pytest.raises(RepositoryError, match=GONE):
        full_entries(repo, "main", GONE)


# ---- read-only ------------------------------------------------------------------------------


def test_reading_works_on_a_read_only_repository(tmp_path):
    repo = make_repo(tmp_path)
    everything = sorted(repo.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    try:
        for path in everything:
            path.chmod(0o555 if path.is_dir() else 0o444)
        repo.chmod(0o555)

        read_info_pair(repo, BACKUP_INFO, label=FULL)
        read_info_pair(repo, ARCHIVE_INFO)
        assert full_entries(repo, "main", FULL)
    finally:
        repo.chmod(0o755)
        for path in reversed(everything):
            path.chmod(0o755 if path.is_dir() else 0o644)


# ---- the fake itself ------------------------------------------------------------------------


def test_fake_pgbackrest_answers_version_and_info(tmp_path):
    repo = make_repo(tmp_path)
    run = fake_pgbackrest(repo, version="2.58.0")

    version = run(["pgbackrest", "version"], capture_output=True, text=True)
    info = run(
        [
            "pgbackrest",
            "--repo=1",
            f"--repo1-path={repo}",
            "--stanza=main",
            "info",
            "--output=json",
        ],
        capture_output=True,
        text=True,
    )

    assert version.stdout == "pgBackRest 2.58.0\n"
    assert json.loads(info.stdout) == info_json(repo, "main")
    with pytest.raises(AssertionError, match="unexpected"):
        run(["pgbackrest", "backup"])
    assert [call[1] for call in run.calls] == ["version", "--repo=1", "backup"]
