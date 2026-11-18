"""Drift: the stage paths against the ledger, checked in a child under a deadline (spec 16 6.3).

These tests run the real child (``drift.run_child``); every other GFS test runs the same code
in-process (``tests/conftest.py``).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from bdbackup.gfs import drift, runner
from bdbackup.gfs.stages import MARKER
from tests.test_gfs_run import Env

REAL = drift.run_child
HANG = [sys.executable, "-c", "import time; time.sleep(30)"]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def run(env, now=None, **kwargs):
    """Run the job, with the real drift child; ``(exit code, report, facts)``."""
    outcome = runner.RunOutcome()
    kwargs.setdefault("drift", REAL)
    code, output = env.run_api(now or datetime.now(), outcome=outcome, **kwargs)
    return code, output, outcome.facts


def stages(facts):
    """``stage -> (checked, missing, unknown)`` of the drift in the facts."""
    return {d["stage"]: (d["checked"], d["missing"], d["unknown"]) for d in facts["drift"]}


def lines(output, prefix="Drift "):
    return [line for line in output.splitlines() if line.startswith(prefix)]


def placed(env):
    """Unit a in stage 2 (NFS/daily), unit b in stage 1; one run done."""
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert run(env)[0] == 0
    return env.root / "NFS/daily/files/a.tar.gz"


# The first stage, when no pgbackrest job feeds it, is checked for missing locations only: it
# has no marker and is not walked, so its unknown count is None.
FIRST = (True, 0, None)
CLEAN = {1: FIRST, 2: (True, 0, 0), 3: (True, 0, 0)}


def test_the_suite_runs_gfs_with_an_in_process_drift_check_by_default():
    assert runner.run_drift is not drift.run_child


def test_a_share_that_matches_the_ledger_has_no_drift(env):
    placed(env)

    code, output, facts = run(env)

    assert code == 0, output
    assert stages(facts) == CLEAN
    assert lines(output) == []


# ---- missing -----------------------------------------------------------------------------------


def test_a_location_removed_by_hand_is_missing(env):
    moved = placed(env)
    moved.unlink()  # the unit only stays in its stage: no action looks at it

    code, output, facts = run(env)

    assert code == 0, output
    assert stages(facts) == {**CLEAN, 2: (True, 1, 0)}
    assert lines(output) == [f"Drift {env.abs('NFS/daily')}: 1 missing, 0 unknown"]


# ---- unknown -----------------------------------------------------------------------------------


def test_stray_entries_are_unknown_one_each_and_are_not_entered(env):
    placed(env)
    daily = env.root / "NFS/daily"
    (daily / "stray.txt").write_text("x")
    (daily / "strays").mkdir()
    (daily / "strays" / "one").write_text("1")  # inside a stray directory: not counted
    (daily / "strays" / "two").write_text("2")
    (daily / "files" / "inside.txt").write_text("x")  # below a directory above a unit

    code, output, facts = run(env)

    assert code == 0, output
    assert stages(facts) == {**CLEAN, 2: (True, 0, 3)}
    assert lines(output) == [f"Drift {env.abs('NFS/daily')}: 0 missing, 3 unknown"]


def test_a_symlink_is_an_entry_and_is_not_followed(env):
    placed(env)
    elsewhere = env.root / "elsewhere"
    elsewhere.mkdir()
    for name in ("a", "b", "c"):
        (elsewhere / name).write_text(name)
    (env.root / "NFS/daily" / "link").symlink_to(elsewhere, target_is_directory=True)
    (env.root / "NFS/daily/files" / "dangling").symlink_to(env.root / "nowhere")

    _, _, facts = run(env)

    assert stages(facts)[2] == (True, 0, 2)  # the two links, not what they point at


def test_a_symlink_above_a_recorded_location_is_one_entry_and_is_not_followed(env):
    placed(env)
    files = env.root / "NFS/daily/files"
    files.rename(env.root / "moved-files")
    files.symlink_to(env.root / "moved-files", target_is_directory=True)

    _, _, facts = run(env)

    # The location is there through the link, so it is not missing; the link is not a directory
    # above a unit, since the walk does not follow it: it is one unknown entry.
    assert stages(facts)[2] == (True, 0, 1)


def request_for(env, **overrides):
    """A check of NFS/daily after one run, for the child's code run in-process."""
    daily = env.abs("NFS/daily")
    fields = {
        "path": daily, "marker": MARKER, "snapshot": "gfs-main.ledger.sqlite3",
        "locations": (str(daily / "files/a.tar.gz"),), "known": (str(daily / "files/a.tar.gz"),),
    }
    return drift._wire(drift.DriftRequest(**{**fields, **overrides}))


def test_a_directory_on_another_device_is_one_unknown_entry_and_is_not_entered(env):
    placed(env)
    child = drift.load_child()
    request = request_for(env)
    assert child["check"](request) == {"checked": True, "missing": 0, "unknown": 0}
    files = str(env.abs("NFS/daily/files"))
    real = child["device_of"]

    # "files" is above a recorded unit, so the walk would enter it; here it is another mount
    child["device_of"] = lambda path: real(path) + 1 if str(path) == files else real(path)

    assert child["check"](request) == {"checked": True, "missing": 0, "unknown": 1}


def test_what_bdbackup_writes_and_the_history_knows_is_not_unknown(env):
    placed(env)
    daily = env.root / "NFS/daily"
    assert (daily / MARKER).is_file() and (daily / "gfs-main.ledger.sqlite3").is_file()
    (daily / "files" / ".bdbackup.lock").write_text("")
    (daily / "files" / f".gfs-tmp-{'ab' * 16}-a.tar.gz").write_text("half a copy")
    (daily / f".main-20261118-010000F.tar.tmp-{'0' * 32}").write_text("half an export")
    loose = daily / "loose.tar"  # a successful record of another job, not a unit of this one
    loose.write_text("a record")
    env._record(loose, {"kind": "file"}, "loose", datetime.now().date(), "file")

    code, output, facts = run(env)

    assert code == 0, output
    assert stages(facts) == CLEAN
    assert lines(output) == []


# ---- unchecked stages --------------------------------------------------------------------------


def test_a_stage_without_its_marker_is_not_checked(env):
    placed(env)
    (env.root / "NFS/weekly" / MARKER).unlink()  # an unmounted share
    (env.root / "NFS/weekly" / "stray").write_text("x")

    code, output, facts = run(env)

    assert stages(facts) == {**CLEAN, 3: (False, 0, 0)}
    assert lines(output) == []  # not a drift: the snapshot step reports the missing marker


def test_a_first_stage_that_is_not_fed_is_never_walked(env):
    placed(env)
    (env.root / "BACKUP" / "stray.txt").write_text("x")

    _, output, facts = run(env)

    assert stages(facts) == CLEAN  # a stray file there is not counted: its unknown is None
    assert lines(output) == []


def test_a_first_stage_that_is_not_fed_is_checked_for_missing_locations(env):
    placed(env)
    (env.root / "BACKUP" / "files" / "b.tar.gz").unlink()  # unit b only stays in stage 1
    (env.root / "BACKUP" / "stray.txt").write_text("x")

    code, output, facts = run(env)

    assert code == 0, output
    assert stages(facts) == {**CLEAN, 1: (True, 1, None)}
    assert lines(output) == [f"Drift {env.abs('BACKUP')}: 1 missing"]  # no unknown count: no walk


def test_a_first_stage_that_does_not_exist_is_not_checked(env):
    placed(env)
    shutil.rmtree(env.root / "BACKUP")

    _, output, facts = run(env)

    assert stages(facts) == {**CLEAN, 1: (False, 0, 0)}
    assert lines(output) == []  # a missing first stage is not a drift of units


def test_a_first_stage_a_pgbackrest_job_writes_into_is_walked(env):
    env.feed_first_stage()
    (env.root / "BACKUP" / MARKER).touch()
    placed(env)
    (env.root / "BACKUP" / "stray.txt").write_text("x")

    _, output, facts = run(env)

    assert stages(facts)[1] == (True, 0, 1)  # the stray file; unit b is recorded, so is not
    assert lines(output) == [f"Drift {env.abs('BACKUP')}: 0 missing, 1 unknown"]


# ---- the deadline and the guards ---------------------------------------------------------------


def with_metrics(env, seconds):
    section = f'[metrics]\npath = "metrics/x.prom"\ntimeout_seconds = {seconds}\n'
    env.config_path.write_text(env.config_path.read_text() + section)


def test_a_hanging_child_ends_the_run_at_the_deadline(env, monkeypatch):
    with_metrics(env, 1)
    placed(env)
    env.file_unit("c.tar.gz", days_ago=8)  # a move for this run to make
    monkeypatch.setattr(drift, "_child_argv", lambda: HANG)

    started = time.monotonic()
    code, output, facts = run(env)
    elapsed = time.monotonic() - started

    assert elapsed < 6
    assert code == 0, output
    assert lines(output, "drift not checked") == ["drift not checked: timeout after 1 s"]
    assert stages(facts) == {1: (False, 0, 0), 2: (False, 0, 0), 3: (False, 0, 0)}
    assert facts["moves"] == {"rename": 1, "copy": 0}  # the move was made before the check
    assert lines(output) == []


def test_the_deadline_is_the_metrics_timeout_or_60(env):
    seen = []

    def spy(requests, timeout):
        seen.append(timeout)
        return [drift.DriftResult(r.path, True) for r in requests]

    placed(env)
    run(env, drift=spy)
    with_metrics(env, 7)
    run(env, drift=spy)

    assert seen == [60, 7]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_walk_error_leaves_the_stage_unchecked_with_a_line_and_the_exit_code_alone(env):
    placed(env)
    files = env.root / "NFS/daily/files"  # above unit a, so the walk enters it
    files.chmod(0o300)  # it can be searched and written, not listed
    try:
        code, output, facts = run(env)
    finally:
        files.chmod(0o700)

    assert code == 0, output
    assert stages(facts) == {**CLEAN, 2: (False, 0, 0)}
    assert lines(output, "drift not checked") == [
        f"drift not checked: {env.abs('NFS/daily')}: Permission denied"
    ]


def test_a_drift_check_that_raises_blocks_nothing_and_changes_no_exit_code(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)

    def broken(requests, timeout):
        raise RuntimeError("boom")

    code, output, facts = run(env, drift=broken)

    assert code == 0, output
    assert (env.root / "NFS/daily/files/a.tar.gz").exists()
    assert lines(output, "drift not checked") == ["drift not checked: boom"]
    assert stages(facts) == {1: (False, 0, 0), 2: (False, 0, 0), 3: (False, 0, 0)}


def test_a_dry_run_before_any_history_exists_still_checks_the_stage_paths(env):
    assert not env.database.exists()
    (env.root / "NFS/daily" / "stray.txt").write_text("x")
    env.dry_run = True

    code, output, facts = run(env)

    assert code == 0, output
    assert facts is None
    assert lines(output, "drift not checked") == []  # no database is no records, not an error
    assert lines(output) == [f"Drift {env.abs('NFS/daily')}: 0 missing, 1 unknown"]


def test_a_dry_run_reports_drift_and_records_no_facts(env):
    moved = placed(env)
    moved.unlink()
    env.dry_run = True

    code, output, facts = run(env)

    assert code == 0, output
    assert facts is None
    assert lines(output) == [f"Drift {env.abs('NFS/daily')}: 1 missing, 0 unknown"]


def test_the_drift_lines_come_after_the_stage_lines_and_before_the_snapshots(env):
    placed(env).unlink()

    _, output, _ = run(env)

    kinds = [re.match(r"(Stage|Drift|snapshot)", line).group(1)
             for line in output.splitlines() if re.match(r"(Stage|Drift|snapshot) ", line)]
    assert kinds == ["Stage", "Stage", "Stage", "Drift", "snapshot", "snapshot"]


# ---- the crash window of a rename (decision D7) ------------------------------------------------


def test_a_crash_after_a_rename_shows_as_one_missing_and_one_unknown(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert run(env)[0] == 0  # a is in NFS/daily
    later = datetime.now() + timedelta(days=30)  # now a belongs in NFS/weekly
    with patch.object(runner.store, "record_move", side_effect=OSError("disk full")):
        code, output, _ = run(env, later)
    assert code == 1 and "failed: disk full" in output  # renamed, and the ledger does not know

    code, output, facts = run(env, later)

    assert code == 1, output
    assert "refused: final name exists" in output
    assert (2, "refused", "final_name_exists", 1) in [
        (r["stage"], r["outcome"], r["reason"], r["units"]) for r in facts["reasons"]
    ]
    # the ledger names a copy that is gone, and the share holds one the ledger does not name
    assert stages(facts) == {**CLEAN, 2: (True, 1, 0), 3: (True, 0, 1)}
    assert lines(output) == [f"Drift {env.abs('NFS/daily')}: 1 missing, 0 unknown",
                             f"Drift {env.abs('NFS/weekly')}: 0 missing, 1 unknown"]


# ---- the facts in the metrics ------------------------------------------------------------------


def test_the_child_checks_a_request_it_is_given_the_same_in_a_real_process(env):
    placed(env)
    (env.root / "NFS/daily" / "stray").write_text("x")
    request = drift.DriftRequest(
        env.abs("NFS/daily"), MARKER, "gfs-main.ledger.sqlite3",
        (str(env.abs("NFS/daily/files/a.tar.gz")),), (str(env.abs("NFS/daily/files/a.tar.gz")),),
    )

    [real] = drift.run_child([request], 20)
    [local] = [drift._result(request, drift.load_child()["check"](drift._wire(request)))]

    assert real == local == drift.DriftResult(env.abs("NFS/daily"), True, 0, 1)


def test_a_path_that_does_not_exist_is_not_checked(tmp_path):
    request = drift.DriftRequest(Path(tmp_path / "gone"), MARKER, "x.sqlite3", (), ())

    [result] = drift.run_child([request], 20)

    assert result == drift.DriftResult(tmp_path / "gone", False, problem="no marker")
