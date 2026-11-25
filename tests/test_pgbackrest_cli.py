"""Running a pgbackrest job from ``bdbackup run``: export, no new full, max age, dry run, locks."""

from __future__ import annotations

import errno
import gc
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.engines import _reset_for_tests
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History, HistoryError, HistorySettings
from bdbackup.postgres import PgBackRest, pgbackrest
from bdbackup.utils import process_lock
from tests.pgbackrest_fake import fake_pgbackrest, info_json, make_repo
from tests.ssh_copy_fake import Remote

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
COPY = '\n[pg.copy]\ndestination="ssh://vault/remote/archive"\nmethod="rsync"\n'


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

    def last_run(self):
        """The ``last_runs`` row of job ``pg``; ``None`` before a run records one."""
        return self.history.last_runs().get("pg")

    def run(self, *args: str, now: datetime = SOON, ssh=None):
        """Invoke ``run -j pg`` with pgBackRest faked and the clock at ``now``."""
        self.fake = fake_pgbackrest(self.repo)

        def execute(argv, **kwargs):
            if ssh is not None and argv[0] in {"ssh", "scp", "rsync"}:
                return ssh(argv, **kwargs)
            return self.fake(argv, **kwargs)

        with patch(RUN, execute), patch("bdbackup.cli._now", return_value=now):
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

    with patch.object(
        PgBackRest, "_read_back", autospec=True, side_effect=PgBackRest._read_back
    ) as spy:
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


def test_the_lock_is_held_from_choosing_to_the_read_back_and_then_released(tmp_path):
    site = make_site(tmp_path)
    seen = []

    def watch(name):
        original = getattr(PgBackRest, name)

        def wrapper(*args, **kwargs):
            seen.append((name, lock_is_held(site.lock_file)))
            return original(*args, **kwargs)

        return patch.object(PgBackRest, name, autospec=True, side_effect=wrapper)

    with watch("choose"), watch("export"), watch("_read_back"):
        result = site.run()

    assert result.exit_code == 0, result.output
    assert seen == [("choose", True), ("export", True), ("_read_back", True)]
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

    with patch.object(PgBackRest, "_read_back", autospec=True) as verify:
        result = site.run("--no-verify")

    assert result.exit_code == 0, result.output
    verify.assert_not_called()
    (row,) = site.rows()
    assert row.status == "success"
    assert row.checksum == hashlib.sha256(site.unit.read_bytes()).hexdigest()


def test_a_failed_read_back_publishes_nothing_and_the_next_run_exports_the_full(
    tmp_path, monkeypatch
):
    site = make_site(tmp_path)
    original = pgbackrest._HashingReader.read

    def eio(self, *args):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(pgbackrest._HashingReader, "read", eio)
    failed = site.run()

    assert failed.exit_code == 1, failed.output
    assert list(site.out_dir.iterdir()) == []  # nothing under the final name, no temp file
    assert [row.status for row in site.rows()] == ["failed"]  # no successful row

    monkeypatch.setattr(pgbackrest._HashingReader, "read", original)
    retry = site.run()

    assert retry.exit_code == 0, retry.output
    assert f"Job pg: {site.unit}" in retry.output
    assert [row.status for row in site.rows()] == ["success", "failed"]


# ---- a binary that is missing or cannot be run -----------------------------------------------

MISSING = "bdbackup-no-such-pgbackrest"
NOT_FOUND = f"pgbackrest binary not found: {MISSING} (set the job's binary key to its full path)"


def binary_site(tmp_path, kind):
    """A site whose job names a binary that does not exist, or a file that is not executable.

    Returns the site and the message a run must report; pgBackRest is not faked.
    """
    if kind == "missing":
        return make_site(tmp_path, extra=f'binary = "{MISSING}"\n'), NOT_FOUND
    binary = tmp_path / "pgbackrest"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o644)
    message = f"pgbackrest binary is not executable: {binary} (check its permissions)"
    return make_site(tmp_path, extra=f'binary = "{binary}"\n'), message


def run_unfaked(site, *args):
    with patch("bdbackup.cli._now", return_value=SOON):
        return CliRunner().invoke(main, ["run", "-c", str(site.config), "-j", "pg", *args])


@pytest.mark.parametrize("args", [(), ("--dry-run",)], ids=["run", "dry-run"])
@pytest.mark.parametrize("kind", ["missing", "not-executable"])
def test_a_binary_that_cannot_run_is_reported_not_raised(tmp_path, caplog, kind, args):
    site, message = binary_site(tmp_path, kind)
    caplog.set_level(logging.INFO)

    result = run_unfaked(site, *args)

    assert result.exit_code == 1, result.output
    assert message in caplog.text
    assert "Traceback" not in caplog.text
    assert "Unexpected error" not in caplog.text
    assert site.rows() == []  # as for every refusal before the export starts (D7)
    assert not site.out_dir.exists()


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("missing", "No such file or directory"), ("not-executable", "Permission denied")],
)
def test_validate_still_reports_the_os_error_for_a_binary_that_cannot_run(
    tmp_path, kind, reason
):
    site, _ = binary_site(tmp_path, kind)

    result = run_unfaked(site, "--validate")

    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    assert f"[FAIL] pgbackrest version failed: {reason}" in lines
    assert f"[FAIL] pgbackrest info failed: {reason}" in lines
    assert "Traceback" not in result.output


# ---- the outcome of each run, kept for the metrics command ------------------------------------


def last_run(site):
    row = site.last_run()
    assert row is not None, "the run recorded no last_runs row"
    return row


def exported_facts(site, phases):
    return {
        "phases": phases,
        "label": NEWEST,
        "bytes": site.unit.stat().st_size,
        "full_age_seconds": 9000,  # the fake stops a full a minute after its label; SOON is later
    }


def test_last_run_of_an_export_has_its_outcome_and_facts(tmp_path):
    site = make_site(tmp_path)
    ticks = iter([10.0, 12.5, 20.0, 23.0])  # write 2.5 seconds, read-back 3.0

    with patch.object(pgbackrest, "_clock", lambda: next(ticks)):
        result = site.run()

    assert result.exit_code == 0, result.output
    row = last_run(site)
    assert (row.job_type, row.result, row.exit_code) == ("pgbackrest", "exported", 0)
    assert row.facts == exported_facts(site, {"write": 2.5, "verify": 3.0})
    assert datetime.fromisoformat(row.finished_at).utcoffset() == timedelta(0)


def test_last_run_of_an_export_without_verify_has_no_verify_phase(tmp_path):
    site = make_site(tmp_path)
    ticks = iter([10.0, 12.5])

    with patch.object(pgbackrest, "_clock", lambda: next(ticks)):
        result = site.run("--no-verify")

    assert result.exit_code == 0, result.output
    assert last_run(site).facts == exported_facts(site, {"write": 2.5})


def test_last_run_of_no_new_full_keeps_the_last_exports_facts(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    exported = last_run(site)

    second = site.run()

    assert second.exit_code == 0, second.output
    row = last_run(site)
    assert (row.result, row.exit_code) == ("no_new_full", 0)
    assert row.facts == exported.facts
    assert row.facts is not None


def test_no_new_full_can_enable_copy_on_schema_six_without_attempting_ssh(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    backup, = site.rows()
    facts = last_run(site).facts
    with closing(sqlite3.connect(site.history.settings.database)) as db, db:
        db.execute("DROP TABLE deliveries")
        db.execute("PRAGMA user_version = 6")
    site.config.write_text(site.config.read_text() + COPY)
    remote = Remote()

    result = site.run(ssh=remote)

    assert result.exit_code == 0, "a no-new-full run may see a history without a copy queue"
    assert "no new full:" in result.stdout and "Copy pg:" not in result.stdout
    assert not remote.calls
    assert site.rows() == [backup]
    row = last_run(site)
    assert (row.result, row.exit_code, row.facts) == ("no_new_full", 0, facts)
    assert site.history.deliveries() == []


@pytest.mark.parametrize(
    "existing,age,code,message",
    [(False, 2, 0, "would export"), (True, 2, 0, "no new full:"), (True, 30, 1, "older than")],
    ids=["new-full", "no-new-full", "too-old"],
)
def test_pgbackrest_dry_run_appends_copy_and_preserves_its_exit_code(
    tmp_path, existing, age, code, message,
):
    site = make_site(tmp_path, extra="max_full_age_hours = 26\n")
    database = site.history.settings.database
    if existing:
        assert site.run().exit_code == 0
        before = database.read_bytes()
    site.config.write_text(site.config.read_text() + COPY)
    paths = set(tmp_path.rglob("*"))
    remote = Remote()

    result = site.run("--dry-run", now=STOP + timedelta(hours=age), ssh=remote)

    assert result.exit_code == code, result.output
    assert message in result.stdout
    assert result.stdout.splitlines()[-1] == (
        "Dry run; configured copy: ssh://vault/remote/archive (rsync)"
    )
    assert not remote.calls
    assert set(tmp_path.rglob("*")) == paths
    if existing:
        assert database.read_bytes() == before


@pytest.mark.parametrize("method,verify", [("rsync", True), ("scp", True), ("scp", False)])
def test_export_copies_after_releasing_its_backup_lock(tmp_path, method, verify):
    site = make_site(tmp_path, extra=COPY.replace("rsync", method))
    remote = Remote()

    def check_lock(argv):
        assert not lock_is_held(site.lock_file)
        assert site.rows()[0].status == "success"

    remote.before = check_lock
    result = site.run(*([] if verify else ["--no-verify"]), ssh=remote)

    assert result.exit_code == 0, result.output
    assert f"Copy pg: {site.unit.name} -> ssh://vault/remote/archive ({method}," in result.stdout
    assert [(tool, path) for tool, path, _ in remote.transfers] == [(method, site.unit)]
    backup, = site.rows()
    assert remote.digest(f"/remote/archive/{site.unit.name}") == backup.checksum
    delivery, = site.history.deliveries()
    assert (delivery.status, delivery.method) == ("copied", method)
    row = last_run(site)
    assert (row.result, row.exit_code) == ("exported", 0)
    assert ("verify" in row.facts["phases"]) is verify
    assert row.facts == exported_facts(site, row.facts["phases"])


def copy_lock(site):
    return process_lock(f"{site.history.settings.database}.copy-pg.lock")


def disconnected(argv):
    return subprocess.CompletedProcess(argv, 255, "", "synthetic refused key")


@pytest.mark.parametrize("disposition", ["failure", "deferred"])
def test_export_copy_outcome_keeps_export_facts_and_no_new_full_retries(tmp_path, disposition):
    site = make_site(tmp_path, extra=COPY)
    remote = Remote()
    if disposition == "failure":
        remote.before = disconnected
        result = site.run(ssh=remote)
        expected = ("copy_failed", 1)
    else:
        with copy_lock(site):
            result = site.run(ssh=remote)
        expected = ("copy_deferred", 3)

    assert result.exit_code == expected[1], "copy failure or deferral must control the run exit"
    backup, = site.rows()
    assert backup.status == "success"
    before, = site.history.deliveries()
    assert before.status == ("failed" if disposition == "failure" else "pending")
    row = last_run(site)
    assert (row.result, row.exit_code) == expected
    assert row.facts == exported_facts(site, row.facts["phases"])
    assert "verify" in row.facts["phases"]

    remote.before = None
    second = site.run(ssh=remote)

    assert second.exit_code == 0, second.output
    assert "no new full:" in second.stdout
    assert f"Copy pg: {site.unit.name} ->" in second.stdout
    assert site.rows() == [backup]
    after, = site.history.deliveries()
    assert (after.status, after.attempt_count) == ("copied", before.attempt_count + 1)
    assert last_run(site).result == "no_new_full"
    assert last_run(site).facts == row.facts


@pytest.mark.parametrize("disposition", ["failure", "deferred"])
def test_no_new_full_copy_outcome_keeps_previous_export_facts(tmp_path, disposition):
    site = make_site(tmp_path, extra=COPY)
    remote = Remote()
    with copy_lock(site):
        first = site.run(ssh=remote)
    assert first.exit_code == 3, "an export must enqueue even when copying is deferred"
    original = last_run(site)
    units = site.rows()

    if disposition == "failure":
        remote.before = disconnected
        result = site.run(ssh=remote)
        expected = ("copy_failed", 1)
    else:
        with copy_lock(site):
            result = site.run(ssh=remote)
        expected = ("copy_deferred", 3)

    assert result.exit_code == expected[1], result.output
    assert "no new full:" in result.stdout
    outcome = last_run(site)
    assert (outcome.result, outcome.exit_code) == expected
    assert outcome.facts == original.facts
    assert site.rows() == units
    assert len(site.history.deliveries()) == 1


def test_copy_is_not_started_by_a_dry_run_failed_export_or_busy_export_lock(tmp_path):
    site = make_site(tmp_path, extra=COPY, marker=False)
    remote = Remote()

    with process_lock(site.lock_file):
        busy = site.run(ssh=remote)
    assert busy.exit_code == 3
    assert last_run(site).result == "locked"
    assert site.run(ssh=remote).exit_code == 1
    assert last_run(site).result == "destination_not_ready"

    (site.root / "STAGE1" / MARKER).touch()
    dry = site.run("--dry-run", ssh=remote)
    assert dry.exit_code == 0
    assert remote.calls == []
    assert site.history.deliveries() == []


def test_too_old_full_leaves_delivery_pending_without_ssh(tmp_path):
    site = make_site(tmp_path, extra="max_full_age_hours = 26\n" + COPY)
    remote = Remote()
    with copy_lock(site):
        first = site.run(ssh=remote)
    assert first.exit_code == 3, "the successful export must create a delivery to defer"
    before = site.history.deliveries()

    late = site.run(now=STOP + timedelta(hours=30), ssh=remote)

    assert late.exit_code == 1
    assert last_run(site).result == "full_too_old"
    assert remote.calls == []
    assert site.history.deliveries() == before


def test_last_run_of_a_full_too_old_has_its_result_and_exit_code(tmp_path):
    site = make_site(tmp_path, extra="max_full_age_hours = 26\n")
    assert site.run().exit_code == 0
    exported = last_run(site)

    late = site.run(now=STOP + timedelta(hours=30))

    assert late.exit_code == 1, late.output
    row = last_run(site)
    assert (row.result, row.exit_code) == ("full_too_old", 1)
    assert row.facts == exported.facts


def test_last_run_of_a_busy_lock_is_locked(tmp_path):
    site = make_site(tmp_path)

    with process_lock(site.lock_file):
        result = site.run()

    assert result.exit_code == 3, result.output
    row = last_run(site)
    assert (row.job_type, row.result, row.exit_code, row.facts) == ("pgbackrest", "locked", 3, None)


def test_last_run_of_a_missing_marker_is_destination_not_ready(tmp_path):
    site = make_site(tmp_path, marker=False)

    result = site.run()

    assert result.exit_code == 1, result.output
    row = last_run(site)
    assert (row.result, row.exit_code, row.facts) == ("destination_not_ready", 1, None)


def test_last_run_of_a_full_disk_is_export_failed(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    gc.collect()  # a stale failed stream of an earlier test flushes now, not through the patch
    original = pgbackrest._HashingWriter.write
    calls = []

    def fail_first_write(self, data):
        if self.discarding:  # the writer of an export that already failed
            return original(self, data)
        calls.append(len(data))
        if len(calls) == 1:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return original(self, data)

    monkeypatch.setattr(pgbackrest._HashingWriter, "write", fail_first_write)

    result = site.run()
    gc.collect()

    assert result.exit_code == 1, result.output
    row = last_run(site)
    assert (row.result, row.exit_code, row.facts) == ("export_failed", 1, None)


def test_last_run_of_a_failed_read_back_is_verify_failed(tmp_path, monkeypatch):
    site = make_site(tmp_path)

    def eio(self, *args):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(pgbackrest._HashingReader, "read", eio)

    result = site.run()

    assert result.exit_code == 1, result.output
    row = last_run(site)
    assert (row.result, row.exit_code, row.facts) == ("verify_failed", 1, None)


@pytest.mark.parametrize("failing", ["info", "version"])
def test_last_run_of_a_failing_pgbackrest_command_is_failed(tmp_path, failing):
    site = make_site(tmp_path)
    fake = fake_pgbackrest(site.repo)

    def run(cmd, **kwargs):
        if cmd[1:] and failing in cmd:
            raise subprocess.CalledProcessError(55, cmd, output="", stderr="ERROR: [055]: no\n")
        return fake(cmd, **kwargs)

    with patch(RUN, run):
        result = run_unfaked(site)

    assert result.exit_code == 1, result.output
    row = last_run(site)
    assert (row.result, row.exit_code, row.facts) == ("failed", 1, None)


def test_last_run_of_a_usage_error_is_failed_with_exit_code_2(tmp_path):
    site = make_site(tmp_path)

    with patch("bdbackup.cli.build_backend", side_effect=ValueError("bad setting")):
        result = run_unfaked(site)

    assert result.exit_code == 2, result.output
    row = last_run(site)
    assert (row.result, row.exit_code) == ("failed", 2)


def test_a_dry_run_leaves_the_last_run_alone(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    before = last_run(site)

    dry = site.run("--dry-run", now=STOP + timedelta(hours=30))

    assert dry.exit_code == 0, dry.output
    assert site.last_run() == before


@pytest.mark.parametrize(
    "error", [HistoryError("disk full"), OSError("read-only")], ids=["history", "os"]
)
@pytest.mark.parametrize(("marker", "code"), [(True, 0), (False, 1)], ids=["exported", "refused"])
def test_a_last_run_recording_failure_is_a_warning_and_changes_nothing_else(
    tmp_path, caplog, marker, code, error
):
    site = make_site(tmp_path, marker=marker)
    caplog.set_level(logging.WARNING)

    with patch.object(History, "record_last_run", side_effect=error):
        result = site.run()

    assert result.exit_code == code, result.output
    assert ("Job pg:" in result.output) == (code == 0)
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == "bdbackup.cli"
    ]
    assert len(warnings) == 1, caplog.text
    assert "last run" in warnings[0].getMessage()
    assert str(error) in warnings[0].getMessage()


# ---- last_runs, review fixes -------------------------------------------------------------------


def test_a_root_refusal_on_a_fresh_install_creates_no_state(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    result = site.run()

    assert result.exit_code == 1, result.output
    assert site.fake.calls == []
    # Nothing is left owned by root: no history database, no directory for it.
    assert not (site.root / "state").exists()
    assert site.last_run() is None


def test_a_root_refusal_leaves_an_existing_last_run_alone(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    before = last_run(site)
    database = (site.root / "state" / "history.sqlite3").read_bytes()
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    result = site.run()

    assert result.exit_code == 1, result.output
    assert site.last_run() == before
    assert (site.root / "state" / "history.sqlite3").read_bytes() == database


def test_a_failed_rename_of_the_export_records_export_failed(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    real_rename = os.rename

    def refuse(source, destination, *args, **kwargs):
        if ".tar.tmp-" in os.fspath(source):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", refuse)

    result = site.run()

    assert result.exit_code == 1, result.output
    assert not site.unit.exists()
    row = last_run(site)
    assert (row.result, row.exit_code, row.facts) == ("export_failed", 1, None)


def test_the_age_of_a_full_that_stopped_in_the_future_is_zero(tmp_path):
    site = make_site(tmp_path)

    result = site.run(now=STOP - timedelta(hours=1))  # the clock is behind the repository's

    assert result.exit_code == 0, result.output
    assert last_run(site).facts["full_age_seconds"] == 0
