"""Running a pgbackrest job from ``bdbackup run``: export, no new full, max age, dry run, locks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupError
from bdbackup.cli import main
from bdbackup.engines import _reset_for_tests
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History, HistorySettings
from bdbackup.postgres import PgBackRest
from bdbackup.utils import process_lock
from tests.pgbackrest_fake import fake_pgbackrest, info_json, make_repo

NEWEST = "20261109-010000F"
STOP = datetime(2026, 11, 9, 1, 1, tzinfo=UTC)  # the fake stops a backup a minute after its label
SOON = STOP + timedelta(hours=2, minutes=30)
RUN = "bdbackup.postgres.pgbackrest.subprocess.run"

CONFIG = """[history]
database = "state/history.sqlite3"

[gfs-main]
type = "gfs"
[[gfs-main.stage]]
paths = ["STAGE1"]
period = "daily"
keep = "5d"

[pg]
type = "pgbackrest"
stanza = "main"
repo_path = "repo"
out_dir = "STAGE1/pg"
"""


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    """Give each test a fresh registry isolated from the user's config dir."""
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


@dataclass
class Site:
    """A config, a fake repository and the way to run job ``pg`` against them."""

    root: Path
    config: Path
    repo: Path
    fake: object = field(default=None)

    @property
    def out_dir(self) -> Path:
        return self.root.resolve() / "STAGE1" / "pg"

    @property
    def lock_file(self) -> Path:
        return self.root.resolve() / "state" / "history.sqlite3.pgbackrest-pg.lock"

    @property
    def unit(self) -> Path:
        return self.out_dir / f"main-{NEWEST}.tar"

    @property
    def history(self) -> History:
        return History(
            HistorySettings(
                database=self.root.resolve() / "state" / "history.sqlite3",
                restore_root=self.root.resolve() / "restores",
            )
        )

    def rows(self):
        """Every history row of the job, newest first; none before the first row is written."""
        if not (self.root / "state" / "history.sqlite3").exists():
            return []
        return self.history.records(job="pg")

    def run(self, *args: str, now: datetime = SOON):
        """Invoke ``run -j pg`` with pgBackRest faked and the clock at ``now``."""
        self.fake = fake_pgbackrest(self.repo)
        with patch(RUN, self.fake), patch("bdbackup.cli._now", return_value=now):
            return CliRunner().invoke(
                main, ["run", "-c", str(self.config), "-j", "pg", *args]
            )


def make_site(tmp_path, *, extra="", marker=True, **repo_args) -> Site:
    repo = make_repo(tmp_path, **repo_args)
    stage = tmp_path / "STAGE1"
    stage.mkdir()
    if marker:
        (stage / MARKER).write_text("")
    config = tmp_path / "config.toml"
    config.write_text(CONFIG + extra)
    return Site(tmp_path, config, repo)


def lock_is_held(lock_file: Path) -> bool:
    try:
        with process_lock(lock_file):
            return False
    except BlockingIOError:
        return True


# ---- a new full ------------------------------------------------------------------------------


def test_run_exports_and_records(tmp_path):
    site = make_site(tmp_path)

    with patch.object(PgBackRest, "verify", autospec=True, side_effect=PgBackRest.verify) as spy:
        result = site.run()

    assert result.exit_code == 0, result.output
    assert f"Job pg: {site.unit}" in result.output
    assert spy.call_count == 1
    (row,) = site.rows()
    assert row.status == "success"
    assert row.backup_type == "pgbackrest-full"
    assert row.path == str(site.unit)
    assert json.loads(row.restore_info) == {
        "kind": "pgbackrest",
        "stanza": "main",
        "label": NEWEST,
        "pgbackrest_version": "2.58.0",
        "pg_version": "16",
        "page_errors": False,
    }
    data = site.unit.read_bytes()
    assert row.checksum == hashlib.sha256(data).hexdigest()
    assert row.size_bytes == len(data)


def test_second_run_no_new_full(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    before = site.unit.read_bytes()

    second = site.run()

    assert second.exit_code == 0, second.output
    assert f"no new full: newest {NEWEST}, 2 hours old" in second.output
    assert "Job pg:" not in second.output
    assert len(site.rows()) == 1
    assert site.unit.read_bytes() == before


def test_a_failed_attempt_does_not_make_the_full_old(tmp_path):
    site = make_site(tmp_path, marker=False)
    assert site.run().exit_code == 1
    (site.root / "STAGE1" / MARKER).write_text("")

    retry = site.run()

    assert retry.exit_code == 0, retry.output
    assert f"Job pg: {site.unit}" in retry.output
    assert [row.status for row in site.rows()] == ["success", "failed"]


# ---- max_full_age_hours (spec 4.6) -----------------------------------------------------------


def test_max_age_turns_no_new_full_into_failure(tmp_path):
    site = make_site(tmp_path, extra="max_full_age_hours = 26\n")
    assert site.run().exit_code == 0

    late = site.run(now=STOP + timedelta(hours=30))

    assert late.exit_code == 1
    assert (
        f"no new full: newest {NEWEST}, 30 hours old (older than max_full_age_hours = 26)"
        in late.output
    )
    assert len(site.rows()) == 1


@pytest.mark.parametrize(
    ("age", "code"),
    [
        pytest.param(timedelta(hours=26), 0, id="exactly-the-limit"),
        pytest.param(timedelta(hours=26, minutes=59), 0, id="hours-round-down"),
        pytest.param(timedelta(hours=27), 1, id="one-hour-over"),
    ],
)
def test_max_age_is_a_strict_limit_on_whole_hours(tmp_path, age, code):
    site = make_site(tmp_path, extra="max_full_age_hours = 26\n")
    assert site.run().exit_code == 0

    again = site.run(now=STOP + age)

    assert again.exit_code == code, again.output
    assert ("older than max_full_age_hours" in again.output) == bool(code)
    assert len(site.rows()) == 1


def test_no_max_age_means_no_limit(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0

    old = site.run(now=STOP + timedelta(days=400))

    assert old.exit_code == 0
    assert f"no new full: newest {NEWEST}, 9600 hours old" in old.output


def test_max_age_not_applied_to_new_full(tmp_path):
    site = make_site(tmp_path, extra="max_full_age_hours = 26\n")

    result = site.run(now=STOP + timedelta(hours=30))

    assert result.exit_code == 0, result.output
    assert f"Job pg: {site.unit}" in result.output
    assert "no new full" not in result.output
    assert [row.status for row in site.rows()] == ["success"]


# ---- page checksum errors --------------------------------------------------------------------

PAGE_WARNING = (
    f"WARNING: full {NEWEST} has page checksum errors; it is exported, "
    "and restoring it needs checksum-page-error=n"
)


def test_page_errors_warn_and_record(tmp_path):
    site = make_site(tmp_path, fulls=((NEWEST, True, True),))

    first = site.run()

    assert first.exit_code == 0, first.output
    assert PAGE_WARNING in first.output
    assert first.output.index("WARNING") < first.output.index("Job pg:")
    (row,) = site.rows()
    assert json.loads(row.restore_info)["page_errors"] is True

    again = site.run()

    assert again.exit_code == 0
    assert PAGE_WARNING in again.output
    assert again.output.index("WARNING") < again.output.index("no new full")


def test_page_errors_warn_before_the_dry_run_report(tmp_path):
    site = make_site(tmp_path, fulls=((NEWEST, True, True),))

    result = site.run("--dry-run")

    assert result.exit_code == 0, result.output
    assert result.output.index(PAGE_WARNING) < result.output.index("Dry run;")


# ---- refusals --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_args",
    [
        pytest.param({"fulls": ((NEWEST, False, False),)}, id="no-archive-copy"),
        pytest.param({"cipher": True}, id="encrypted"),
        pytest.param({"fulls": ((f"{NEWEST}_20261110-010000D", True, False),)}, id="no-full"),
    ],
)
@pytest.mark.parametrize("args", [(), ("--dry-run",)], ids=["run", "dry-run"])
def test_refusals_write_no_row(tmp_path, caplog, repo_args, args):
    site = make_site(tmp_path, **repo_args)
    caplog.set_level(logging.INFO)

    result = site.run(*args)

    assert result.exit_code == 1, result.output
    assert "Backup/verify failed" in caplog.text
    assert site.rows() == []
    assert not site.out_dir.exists()


def test_root_is_refused_before_the_lock_is_taken(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    with process_lock(site.lock_file):
        result = site.run()

    assert result.exit_code == 1, result.output
    assert site.fake.calls == []
    assert site.rows() == []


def test_root_is_refused_on_a_dry_run(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    result = site.run("--dry-run")

    assert result.exit_code == 1, result.output
    assert site.fake.calls == []


# ---- the lock (spec 4.7) ---------------------------------------------------------------------


def test_locked_exits_3(tmp_path):
    site = make_site(tmp_path)

    with process_lock(site.lock_file):
        result = site.run()

    assert result.exit_code == 3, result.output
    assert site.fake.calls == []  # nothing ran, not even the choice of a full
    assert site.rows() == []
    assert not site.out_dir.exists()


def test_the_lock_is_held_from_choosing_to_verifying_and_then_released(tmp_path):
    site = make_site(tmp_path)
    seen = []

    def watch(name):
        original = getattr(PgBackRest, name)

        def wrapper(engine, *args):
            seen.append((name, lock_is_held(site.lock_file)))
            return original(engine, *args)

        return patch.object(PgBackRest, name, autospec=True, side_effect=wrapper)

    with watch("choose"), watch("export"), watch("verify"):
        result = site.run()

    assert result.exit_code == 0, result.output
    assert seen == [("choose", True), ("export", True), ("verify", True)]
    assert not lock_is_held(site.lock_file)


# ---- dry run (spec 6.2) ----------------------------------------------------------------------


def test_dry_run_reports_and_writes_nothing(tmp_path):
    site = make_site(tmp_path)
    repo_bytes = info_json(site.repo, "main")[0]["backup"][0]["info"]["repository"]["size"]

    result = site.run("--dry-run")

    assert result.exit_code == 0, result.output
    assert (
        f"Dry run; would export {NEWEST} ({repo_bytes} bytes) of stanza main to {site.unit}"
        in result.output
    )
    assert not site.out_dir.exists()
    assert not (site.root / "state").exists()  # no history database, no lock file
    assert all("info" in call for call in site.fake.calls)
    assert not any("version" in call for call in site.fake.calls)


@pytest.mark.parametrize(
    ("age", "code", "limit"),
    [
        pytest.param(timedelta(hours=2), 0, "", id="recent"),
        pytest.param(timedelta(hours=30), 1, " (older than max_full_age_hours = 26)", id="old"),
    ],
)
def test_dry_run_with_no_new_full_reports_it_with_the_same_exit_code(tmp_path, age, code, limit):
    site = make_site(tmp_path, extra="max_full_age_hours = 26\n")
    assert site.run().exit_code == 0

    result = site.run("--dry-run", now=STOP + age)

    assert result.exit_code == code, result.output
    assert f"no new full: newest {NEWEST}, {age // timedelta(hours=1)} hours old{limit}" in (
        result.output
    )
    assert "Dry run;" not in result.output
    assert len(site.rows()) == 1


def test_dry_run_marker_missing_fails(tmp_path, caplog):
    site = make_site(tmp_path, marker=False)
    caplog.set_level(logging.INFO)

    result = site.run("--dry-run")

    assert result.exit_code == 1, result.output
    assert MARKER in caplog.text
    assert "Dry run;" not in result.output
    assert not site.out_dir.exists()
    assert site.rows() == []


# ---- --no-verify -----------------------------------------------------------------------------


def test_no_verify_skips_readback(tmp_path):
    site = make_site(tmp_path)

    with patch.object(PgBackRest, "verify", autospec=True) as verify:
        result = site.run("--no-verify")

    assert result.exit_code == 0, result.output
    verify.assert_not_called()
    (row,) = site.rows()
    assert row.status == "success"
    assert row.checksum == hashlib.sha256(site.unit.read_bytes()).hexdigest()


def test_a_failed_verification_leaves_a_failed_row_and_exit_1(tmp_path):
    site = make_site(tmp_path)

    with patch.object(
        PgBackRest, "verify", autospec=True, side_effect=BackupError("verification failed")
    ) as verify:
        result = site.run()

    assert result.exit_code == 1
    verify.assert_called_once()
    assert site.unit.is_file()  # exported before the read-back found fault
    assert [row.status for row in site.rows()] == ["failed"]
