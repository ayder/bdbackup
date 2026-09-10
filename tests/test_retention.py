"""Behaviour tests for bdbackup.retention -- the GFS retention policy.

Behavioural contract is unchanged from the reference backup_retention.py
tool; tests exercise the in-process planner (`Policy`/`scan`) and the
`bdbackup retention` CLI command.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup import retention as br
from bdbackup.cli import main


def mkfile(directory, name, when, size=64):
    path = Path(directory) / name
    path.write_bytes(b"\0" * size)
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


NOW = datetime(2026, 7, 29, 23, 59, 59)


@pytest.fixture
def tree(tmp_path):
    full = tmp_path / "full"
    incr = tmp_path / "incr"
    log = tmp_path / "log"
    for d in (full, incr, log):
        d.mkdir()
    return type("Tree", (), {"root": tmp_path, "full": full, "incr": incr, "log": log})()


def plan(tree, *, daily=7, weekly=4, monthly=6, incr_days=7, pick="first", min_keep=2):
    policy = br.Policy(daily, weekly, monthly, incr_days, 30, pick, min_keep)
    fulls, _ = br.scan(tree.full, "full_*.mbi", "full", NOW)
    incrs, _ = br.scan(tree.incr, "incr_*.mbi", "incr", NOW)
    today = NOW.date()
    policy.apply_to_fulls(fulls, today)
    policy.apply_to_incrementals(fulls, incrs, today)
    return fulls, incrs


def add_full(tree, when):
    return mkfile(tree.full, f"full_{when:%Y%m%d_%H%M%S}.mbi", when)


def add_incr(tree, when):
    return mkfile(tree.incr, f"incr_{when:%Y%m%d_%H%M%S}.mbi", when)


def kept(files):
    return sorted(f.name for f in files if f.keep)


class TestTiers:
    def test_daily_keeps_every_backup_in_window(self, tree):
        for i in range(10):
            add_full(tree, NOW - timedelta(days=i, hours=22))
            add_full(tree, NOW - timedelta(days=i, hours=10))
        fulls, _ = plan(tree)
        daily = [f for f in fulls if f.reason.startswith("daily")]
        # 7 calendar days x 2 backups a day
        assert len(daily) == 14, [f.name for f in daily]

    def test_weekly_keeps_one_per_iso_week(self, tree):
        for i in range(84):
            add_full(tree, NOW - timedelta(days=i, hours=22))
        fulls, _ = plan(tree)
        weekly = [f for f in fulls if f.reason.startswith("weekly")]
        assert len(weekly) == 4
        assert [f.ts.strftime("%a") for f in weekly] == ["Mon"] * 4

    def test_monthly_keeps_first_of_month(self, tree):
        for i in range(300):
            add_full(tree, NOW - timedelta(days=i, hours=22))
        fulls, _ = plan(tree)
        monthly = [f for f in fulls if f.reason.startswith("monthly")]
        assert len(monthly) == 6
        assert [f.ts.day for f in monthly] == [1] * 6
        assert [f.ts.strftime("%Y-%m") for f in monthly] == [
            "2026-01",
            "2026-02",
            "2026-03",
            "2026-04",
            "2026-05",
            "2026-06",
        ]

    def test_pick_last_takes_newest_in_bucket(self, tree):
        for i in range(300):
            add_full(tree, NOW - timedelta(days=i, hours=22))
        fulls, _ = plan(tree, pick="last")
        monthly = [f for f in fulls if f.reason.startswith("monthly")]
        # Jun truncated by the weekly floor (2026-06-29)
        assert [f.ts.day for f in monthly] == [31, 28, 31, 30, 31, 28]

    def test_tiers_do_not_overlap(self, tree):
        for i in range(300):
            add_full(tree, NOW - timedelta(days=i, hours=22))
        fulls, _ = plan(tree)
        kept_files = [f for f in fulls if f.keep]
        # 7 daily + 4 weekly + 6 monthly, no double counting
        assert len(kept_files) == 17, [(f.name, f.reason) for f in kept_files]

    def test_gaps_do_not_shift_buckets(self, tree):
        for month in range(1, 8):
            add_full(tree, datetime(2026, month, 15, 2, 0))
        fulls, _ = plan(tree)
        assert kept(fulls) == sorted(
            [
                "full_20260115_020000.mbi",
                "full_20260215_020000.mbi",
                "full_20260315_020000.mbi",
                "full_20260415_020000.mbi",
                "full_20260515_020000.mbi",
                "full_20260615_020000.mbi",
                "full_20260715_020000.mbi",
            ]
        )


class TestChainSafety:
    def test_weekly_tier_cannot_strand_an_incremental(self, tree):
        """The classic data-loss bug: weekly picks the Monday full, the Sunday
        full expires, and the incrementals still inside the daily window that
        descend from it become unrestorable."""
        add_full(tree, datetime(2026, 7, 13, 2, 0))  # Mon -> weekly winner
        add_full(tree, datetime(2026, 7, 19, 2, 0))  # Sun -> chain anchor
        for day in (23, 24, 25):
            add_incr(tree, datetime(2026, 7, day, 2, 0))

        fulls, incrs = plan(tree, min_keep=0)
        anchor = [f for f in fulls if "20260719" in f.name][0]

        assert anchor.keep, "full_20260719 was expired while incrementals still depend on it"
        assert "chain anchor" in anchor.reason
        assert len(kept(incrs)) == 3

    def test_incremental_without_parent_is_dropped(self, tree):
        add_full(tree, datetime(2026, 7, 26, 2, 0))
        add_incr(tree, datetime(2026, 7, 24, 2, 0))  # predates every full
        _fulls, incrs = plan(tree)
        assert kept(incrs) == []
        assert "orphan" in incrs[0].reason

    def test_old_incrementals_expire_with_their_chain(self, tree):
        add_full(tree, datetime(2026, 3, 1, 2, 0))
        for day in range(2, 10):
            add_incr(tree, datetime(2026, 3, day, 2, 0))
        fulls, incrs = plan(tree)
        assert fulls[0].keep  # monthly -4
        assert kept(incrs) == []  # but no incrementals
        assert "chain anchor" not in fulls[0].reason


class TestSafety:
    def test_min_keep_fulls_floor(self, tree):
        add_full(tree, datetime(2025, 1, 5, 2, 0))
        add_full(tree, datetime(2025, 1, 6, 2, 0))
        add_full(tree, datetime(2025, 1, 7, 2, 0))
        fulls, _ = plan(tree, min_keep=2)
        assert kept(fulls) == [
            "full_20250106_020000.mbi",
            "full_20250107_020000.mbi",
        ]

    def test_temp_and_symlink_are_skipped(self, tree):
        add_full(tree, datetime(2026, 7, 26, 2, 0))
        mkfile(tree.full, "full_20250101_020000.tmp.mbi", datetime(2025, 1, 1, 2, 0))
        target = add_full(tree, datetime(2025, 2, 1, 2, 0))
        os.symlink(target, Path(tree.full) / "full_link.mbi")
        _fulls, skipped = br.scan(tree.full, "full_*.mbi", "full", NOW)
        reasons = sorted(r for _, r in skipped)
        assert reasons == ["symlink", "temporary suffix"]

    def test_grace_window_protects_running_backup(self, tree):
        add_full(tree, NOW - timedelta(minutes=5))
        fulls, skipped = br.scan(tree.full, "full_*.mbi", "full", NOW)
        assert fulls == []
        assert "grace" in skipped[0][1]

    def test_future_timestamp_is_skipped(self, tree):
        add_full(tree, NOW + timedelta(days=30))
        fulls, skipped = br.scan(tree.full, "full_*.mbi", "full", NOW)
        assert fulls == []
        assert "future" in skipped[0][1]


class TestCLI:
    """CLI behaviour, matching the reference tool's subprocess tests."""

    def _args(self, tree, *extra, apply=False):
        args = [
            "retention",
            "--full-dir",
            str(tree.full),
            "--incr-dir",
            str(tree.incr),
            "--log-dir",
            str(tree.log),
            "--now",
            "2026-07-29",
            "-q",
        ]
        if apply:
            args.append("--apply")
        return args + list(extra)

    def test_empty_full_dir_aborts_the_run(self, tree):
        result = CliRunner().invoke(main, self._args(tree))
        assert result.exit_code == 3, result.output

    def test_dry_run_deletes_nothing(self, tree):
        for i in range(400):
            add_full(tree, NOW - timedelta(days=i, hours=22))
        before = len(list(Path(tree.full).iterdir()))
        result = CliRunner().invoke(main, self._args(tree))
        assert result.exit_code == 0, result.output
        assert len(list(Path(tree.full).iterdir())) == before

    def test_apply_deletes_exactly_the_plan(self, tree):
        for i in range(400):
            add_full(tree, NOW - timedelta(days=i, hours=22))
        result = CliRunner().invoke(main, self._args(tree, apply=True))
        assert result.exit_code == 0, result.output
        assert len(list(Path(tree.full).iterdir())) == 17

    def test_second_run_is_idempotent(self, tree):
        for i in range(400):
            add_full(tree, NOW - timedelta(days=i, hours=22))
        first = CliRunner().invoke(main, self._args(tree, apply=True))
        assert first.exit_code == 0, first.output
        after_first = sorted(p.name for p in Path(tree.full).iterdir())
        second = CliRunner().invoke(main, self._args(tree, apply=True))
        assert second.exit_code == 0, second.output
        assert sorted(p.name for p in Path(tree.full).iterdir()) == after_first


class TestTimestampParsing:
    def test_formats(self):
        cases = {
            "full_20260729.mbi": datetime(2026, 7, 29),
            "full_20260729_143000.mbi": datetime(2026, 7, 29, 14, 30, 0),
            "full_20260729143000.mbi": datetime(2026, 7, 29, 14, 30, 0),
            "full_2026-07-29.mbi": datetime(2026, 7, 29),
            "full_2026-07-29T14-30-00.mbi": datetime(2026, 7, 29, 14, 30, 0),
            "full_2026_07_29.mbi": datetime(2026, 7, 29),
            "db01_full_20260729_020000.mbi": datetime(2026, 7, 29, 2, 0, 0),
        }
        for name, expected in cases.items():
            assert br.parse_timestamp_from_name(name) == expected, name

    def test_rejects_non_dates(self):
        for name in ("full_v2.mbi", "full_backup.mbi", "full_20261345.mbi"):
            assert br.parse_timestamp_from_name(name) is None, name

    def test_falls_back_to_mtime(self, tmp_path):
        when = datetime(2026, 7, 20, 3, 0)
        mkfile(tmp_path, "full_nodate.mbi", when)
        files, _ = br.scan(tmp_path, "full_*.mbi", "full", datetime(2026, 7, 29, 23, 59))
        assert files[0].ts_source == "mtime"
        assert files[0].ts == when


class TestMonthMath:
    def test_shift_month_across_year(self):
        d = datetime(2026, 2, 15).date()
        assert br.shift_month(d, 1).isoformat() == "2026-01-01"
        assert br.shift_month(d, 2).isoformat() == "2025-12-01"
        assert br.shift_month(d, 14).isoformat() == "2024-12-01"

    def test_month_end_leap_year(self):
        assert br.month_end(br.date(2024, 2, 1)).day == 29
        assert br.month_end(br.date(2026, 2, 1)).day == 28


class TestConfigJobs:
    """type = "retention" jobs run via `bdbackup run` like any other job."""

    def _config(self, tree, apply=False):
        cfg = Path(tree.root) / "config.toml"
        cfg.write_text(
            "[cleanup]\n"
            'type = "retention"\n'
            f'full_dir = "{tree.full}"\n'
            f'incr_dir = "{tree.incr}"\n'
            f'log_dir = "{tree.log}"\n'
            f"apply = {'true' if apply else 'false'}\n"
            "daily = 7\nweekly = 4\nmonthly = 6\n"
            "quiet = true\n"
        )
        return cfg

    def test_config_job_dry_run_then_apply(self, tree, monkeypatch):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        monkeypatch.setattr(br, "datetime", FrozenDatetime)
        for i in range(400):
            add_full(tree, NOW - timedelta(days=i, hours=22))

        cfg = self._config(tree, apply=False)
        before = len(list(Path(tree.full).iterdir()))
        result = CliRunner().invoke(main, ["run", "-c", str(cfg), "cleanup"])
        assert result.exit_code == 0, result.output
        assert len(list(Path(tree.full).iterdir())) == before  # dry-run

        cfg = self._config(tree, apply=True)
        result = CliRunner().invoke(main, ["run", "-c", str(cfg), "cleanup"])
        assert result.exit_code == 0, result.output
        assert len(list(Path(tree.full).iterdir())) == 17

    def test_config_job_empty_full_dir_aborts_run(self, tree):
        cfg = self._config(tree, apply=True)
        result = CliRunner().invoke(main, ["run", "-c", str(cfg), "cleanup"])
        # Empty full dir -> run_job rc 3 -> job failure -> EXIT_BACKUP_FAILED (1)
        assert result.exit_code == 1
