"""GFS (grandfather-father-son) retention policy for backup trees.

Tiered retention — every backup for the last N days, then one per ISO week
for N weeks, then one per calendar month for N months. Tiers are SEQUENTIAL,
not overlapping: the weekly tier only considers backups older than the daily
window, and the monthly tier only considers backups older than the weekly
range. Chain safety keeps incrementals restorable: an incremental is dropped
when its parent full is gone (orphan / broken chain), and a full that still
anchors a live incremental is pinned even past its tier ("chain anchor").

This module is the policy engine only. The ``bdbackup retention`` CLI command
and ``type = "retention"`` config jobs are thin wrappers over ``main()`` /
:func:`run_job` (see below); nothing here depends on backends or engines.
"""

from __future__ import annotations

import calendar
import fnmatch
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

__all__ = [
    "BackupFile",
    "Policy",
    "human",
    "looks_temporary",
    "main",
    "month_end",
    "month_start",
    "parse_timestamp_from_name",
    "run_job",
    "scan",
    "shift_month",
    "week_start",
]

# Exit-code contract (mirrors bdbackup.cli: 2 usage, 3 refuses-empty).
EXIT_OK = 0
EXIT_DELETE_FAILURES = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

# Conservative defaults, matching the reference policy script.
DEFAULTS = {
    "full_glob": "full_*.mbi",
    "incr_glob": "incr_*.mbi",
    "log_glob": "*.log",
    "daily": 7,
    "weekly": 4,
    "monthly": 6,
    "incr_days": 7,
    "log_days": 30,
    "pick": "first",
    "min_keep_fulls": 2,
}

# Markers for files still being written; matched as dot-separated components
# so both "full_x.mbi.tmp" and "full_x.tmp.mbi" are recognised.
IGNORE_TOKENS = frozenset(
    {"tmp", "temp", "part", "partial", "inprogress", "incomplete",
     "lock", "writing", "new"}
)
GRACE_MINUTES = 60
USE_FILENAME_TIMESTAMP = True

# Never operate on these, ever.
FORBIDDEN_DIRS = frozenset(
    {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib",
     "/proc", "/root", "/sbin", "/sys", "/usr", "/var"}
)

# Ordered most-specific first; each exposes named groups Y/m/d[/H/M/S].
_TS_PATTERNS = (
    # 2026-07-29T14-30-00 / 2026-07-29_14-30-00 / 2026-07-29 14:30:00
    re.compile(r"(?P<Y>\d{4})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})"
               r"[T_ -](?P<H>\d{2})[-:.]?(?P<M>\d{2})[-:.]?(?P<S>\d{2})"),
    # 20260729_143000 / 20260729-143000 / 20260729143000
    re.compile(r"(?P<Y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"
               r"[-_T]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})"),
    # 2026-07-29
    re.compile(r"(?P<Y>\d{4})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})"),
    # 20260729
    re.compile(r"(?P<Y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"),
)


# --------------------------------------------------------------------------
# Filename timestamp parsing
# --------------------------------------------------------------------------

def parse_timestamp_from_name(name: str) -> datetime | None:
    """Return a datetime parsed out of *name*, or None."""
    for pattern in _TS_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        parts = match.groupdict()
        try:
            stamp = datetime(
                int(parts["Y"]), int(parts["m"]), int(parts["d"]),
                int(parts.get("H") or 0),
                int(parts.get("M") or 0),
                int(parts.get("S") or 0),
            )
        except ValueError:
            continue  # e.g. matched 20261345 -- try the next pattern
        # Reject obvious nonsense so a version number never becomes a date.
        if 1990 <= stamp.year <= 2200:
            return stamp
    return None


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

def looks_temporary(entry: str) -> bool:
    """True if *entry* looks like a file the backup job is still writing."""
    lowered = entry.lower()
    if lowered.startswith(".") or lowered.endswith("~"):
        return True
    return any(token in lowered.split(".") for token in IGNORE_TOKENS)


@dataclass(slots=True)
class BackupFile:
    """One file on disk, with the timestamp we make decisions from."""

    path: str | Path
    kind: str           # "full" | "incr" | "log"
    ts: datetime
    size: int
    ts_source: str      # "name" | "mtime"
    keep: bool = False
    reason: str = "expired"
    parent: BackupFile | None = None  # for incrementals: the BackupFile full

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def day(self) -> date:
        return self.ts.date()

    def mark(self, reason: str) -> None:
        """Keeping wins over deleting; the first reason recorded is kept."""
        if not self.keep:
            self.keep = True
            self.reason = reason

    def __repr__(self) -> str:
        return f"<{self.kind} {self.name} {self.ts}>"


def scan(
    directory: str | Path,
    glob_pattern: str,
    kind: str,
    now: datetime,
) -> tuple[list[BackupFile], list[tuple[Path, str]]]:
    """Collect BackupFile objects for *glob_pattern* inside *directory*."""
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")

    real = os.path.realpath(directory).rstrip("/")
    if real in {p.rstrip("/") for p in FORBIDDEN_DIRS}:
        raise ValueError(f"refusing to operate on system directory: {real}")

    grace = timedelta(minutes=GRACE_MINUTES)
    files: list[BackupFile] = []
    skipped: list[tuple[Path, str]] = []

    for entry in sorted(os.listdir(directory)):
        path = directory / entry
        if not fnmatch.fnmatch(entry, glob_pattern):
            continue
        if looks_temporary(entry):
            skipped.append((path, "temporary suffix"))
            continue
        if path.is_symlink():
            skipped.append((path, "symlink"))
            continue
        if not path.is_file():
            continue

        try:
            stat = path.stat()
        except OSError as exc:
            skipped.append((path, f"stat failed: {exc}"))
            continue

        mtime = datetime.fromtimestamp(stat.st_mtime)
        ts = mtime
        source = "mtime"
        if USE_FILENAME_TIMESTAMP:
            parsed = parse_timestamp_from_name(entry)
            if parsed is not None:
                ts = parsed
                source = "name"

        # Order matters: a future mtime must be reported as a clock problem,
        # not silently absorbed by the grace window (now - mtime goes negative
        # and would otherwise compare as inside the grace window).
        if ts > now + timedelta(days=1) or mtime > now + grace:
            skipped.append((path, "timestamp in the future -- check the clock"))
            continue
        if timedelta(0) <= now - mtime < grace:
            skipped.append((path, f"inside {GRACE_MINUTES} min grace window"))
            continue

        files.append(BackupFile(path, kind, ts, stat.st_size, source))

    files.sort(key=lambda f: (f.ts, f.name))
    return files, skipped


# --------------------------------------------------------------------------
# Bucket helpers
# --------------------------------------------------------------------------

def week_start(day: date) -> date:
    """Monday of the ISO week containing *day*."""
    return day - timedelta(days=day.weekday())


def month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def shift_month(day: date, months_back: int) -> date:
    """First day of the month *months_back* months before *day*'s month."""
    total = (day.year * 12 + (day.month - 1)) - months_back
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def month_end(first_of_month: date) -> date:
    last = calendar.monthrange(first_of_month.year, first_of_month.month)[1]
    return date(first_of_month.year, first_of_month.month, last)


def pick(bucket_files: list[BackupFile], mode: str) -> BackupFile | None:
    """Choose the survivor inside a bucket (files already sorted by ts)."""
    if not bucket_files:
        return None
    return bucket_files[0] if mode == "first" else bucket_files[-1]


# --------------------------------------------------------------------------
# The policy
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Policy:
    """GFS tier settings + selection over a scanned set of files."""

    daily_days: int = DEFAULTS["daily"]
    weekly: int = DEFAULTS["weekly"]
    monthly: int = DEFAULTS["monthly"]
    incr_days: int = DEFAULTS["incr_days"]
    log_days: int = DEFAULTS["log_days"]
    bucket_pick: str = DEFAULTS["pick"]
    min_keep_fulls: int = DEFAULTS["min_keep_fulls"]

    # -- window boundaries -------------------------------------------------

    def daily_floor(self, today: date) -> date:
        """Oldest date still inside the 'keep everything' window."""
        return today - timedelta(days=self.daily_days - 1)

    def weekly_buckets(self, today: date) -> list[tuple[date, date]]:
        """[(start, end)] for the N ISO weeks preceding the current one."""
        current_monday = week_start(today)
        buckets = []
        for i in range(1, self.weekly + 1):
            start = current_monday - timedelta(weeks=i)
            buckets.append((start, start + timedelta(days=6)))
        return buckets

    def weekly_floor(self, today: date) -> date:
        buckets = self.weekly_buckets(today)
        return buckets[-1][0] if buckets else self.daily_floor(today)

    def monthly_buckets(self, today: date) -> list[tuple[date, date]]:
        """[(start, end)] for the N calendar months preceding this one."""
        buckets = []
        for i in range(1, self.monthly + 1):
            start = shift_month(today, i)
            buckets.append((start, month_end(start)))
        return buckets

    # -- selection ---------------------------------------------------------

    def apply_to_fulls(self, fulls: list[BackupFile], today: date) -> None:
        daily_floor = self.daily_floor(today)
        weekly_floor = self.weekly_floor(today)

        # Tier 1 -- everything in the last N days.
        for f in fulls:
            if f.day >= daily_floor:
                f.mark(f"daily (last {self.daily_days} days)")

        # Tier 2 -- one per ISO week, only for what the daily tier did not take.
        for index, (start, end) in enumerate(self.weekly_buckets(today), 1):
            bucket = [f for f in fulls
                      if start <= f.day <= end and f.day < daily_floor]
            chosen = pick(bucket, self.bucket_pick)
            if chosen is not None:
                chosen.mark(f"weekly -{index} (week of {start})")

        # Tier 3 -- one per calendar month, older than the weekly range.
        for index, (start, end) in enumerate(self.monthly_buckets(today), 1):
            bucket = [f for f in fulls
                      if start <= f.day <= end and f.day < weekly_floor]
            chosen = pick(bucket, self.bucket_pick)
            if chosen is not None:
                chosen.mark(f"monthly -{index} ({start.strftime('%Y-%m')})")

        # Safety floor -- always leave the newest N fulls in place.
        for f in list(reversed(fulls))[: self.min_keep_fulls]:
            f.mark(f"safety floor (newest {self.min_keep_fulls} fulls)")

    def apply_to_incrementals(
        self,
        fulls: list[BackupFile],
        incrs: list[BackupFile],
        today: date,
    ) -> None:
        incr_floor = today - timedelta(days=self.incr_days - 1)

        # Attach each incremental to the newest full at or before it.
        for incr in incrs:
            parent = None
            for full in fulls:
                if full.ts <= incr.ts:
                    parent = full
                else:
                    break
            incr.parent = parent

        # Age window first...
        for incr in incrs:
            if incr.parent is None:
                incr.reason = "orphan: no full precedes it"
                continue
            if incr.day >= incr_floor:
                incr.mark(f"incremental (last {self.incr_days} days)")

        # ...then pin the fulls those survivors depend on. A full that anchors
        # a live chain must outlive the GFS tiers or the chain is unrestorable.
        for incr in incrs:
            if incr.keep and incr.parent is not None:
                incr.parent.mark(f"chain anchor for {incr.name}")

        # Finally drop any incremental whose parent did not survive.
        for incr in incrs:
            if incr.keep and (incr.parent is None or not incr.parent.keep):
                incr.keep = False
                parent_name = incr.parent.name if incr.parent else "?"
                incr.reason = f"broken chain: parent {parent_name} expired"

    def apply_to_logs(self, logs: list[BackupFile], today: date) -> None:
        floor = today - timedelta(days=self.log_days - 1)
        for log in logs:
            if log.day >= floor:
                log.mark(f"log (last {self.log_days} days)")


# --------------------------------------------------------------------------
# Reporting / execution
# --------------------------------------------------------------------------

def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def report(
    groups: list[tuple[str, list[BackupFile]]],
    verbose: bool,
) -> tuple[str, int, int]:
    lines: list[str] = []
    total_keep = total_del = 0
    bytes_keep = bytes_del = 0

    for label, files in groups:
        if not files:
            continue
        lines.append("")
        lines.append(f"== {label} ({len(files)} files) ==")
        for f in files:
            flag = "KEEP  " if f.keep else "DELETE"
            if f.keep:
                total_keep += 1
                bytes_keep += f.size
            else:
                total_del += 1
                bytes_del += f.size
            if verbose or not f.keep:
                lines.append(
                    f"  {flag} {f.name:<42} {f.ts:%Y-%m-%d %H:%M}  "
                    f"{human(f.size):<10}  {f.reason}"
                )
        if not verbose:
            kept = sum(1 for f in files if f.keep)
            lines.append(f"  ({kept} kept, use -v to list them)")

    lines.append("")
    lines.append(
        f"Summary: keep {total_keep} ({human(bytes_keep)}), "
        f"delete {total_del} ({human(bytes_del)})"
    )
    return "\n".join(lines), total_del, bytes_del


def execute(files: list[BackupFile], apply_changes: bool) -> tuple[int, int, int]:
    deleted = failed = 0
    freed = 0
    for f in files:
        if f.keep:
            continue
        if not apply_changes:
            deleted += 1
            freed += f.size
            continue
        try:
            os.unlink(f.path)
        except OSError as exc:
            sys.stderr.write(f"ERROR: could not delete {f.path}: {exc}\n")
            failed += 1
            continue
        deleted += 1
        freed += f.size
    return deleted, failed, freed


# --------------------------------------------------------------------------
# Runner / CLI core
# --------------------------------------------------------------------------

def run_job(
    *,
    full_dir: str | Path,
    incr_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
    full_glob: str = DEFAULTS["full_glob"],
    incr_glob: str = DEFAULTS["incr_glob"],
    log_glob: str = DEFAULTS["log_glob"],
    daily: int = DEFAULTS["daily"],
    weekly: int = DEFAULTS["weekly"],
    monthly: int = DEFAULTS["monthly"],
    incr_days: int = DEFAULTS["incr_days"],
    log_days: int = DEFAULTS["log_days"],
    pick_mode: str = DEFAULTS["pick"],
    min_keep_fulls: int = DEFAULTS["min_keep_fulls"],
    now: datetime | None = None,
    apply_changes: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    out=sys.stdout,
) -> int:
    """Plan retention for a tree and optionally apply it. Returns an exit code."""
    if daily < 1:
        sys.stderr.write("ERROR: daily must be >= 1\n")
        return EXIT_USAGE
    if weekly < 0 or monthly < 0:
        sys.stderr.write("ERROR: weekly/monthly must be >= 0\n")
        return EXIT_USAGE

    now = now or datetime.now()
    today = now.date()

    policy = Policy(daily, weekly, monthly, incr_days, log_days,
                    pick_mode, min_keep_fulls)

    try:
        fulls, skipped_f = scan(full_dir, full_glob, "full", now)
        incrs, skipped_i = (
            scan(incr_dir, incr_glob, "incr", now) if incr_dir else ([], [])
        )
        logs, skipped_l = (
            scan(log_dir, log_glob, "log", now) if log_dir else ([], [])
        )
    except (NotADirectoryError, ValueError, OSError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return EXIT_USAGE

    if not fulls:
        sys.stderr.write(
            f"ERROR: no full backups matched {full_glob} in {full_dir} -- "
            "refusing to expire anything, because that is what a broken "
            "backup job looks like.\n"
        )
        return EXIT_REFUSED

    policy.apply_to_fulls(fulls, today)
    policy.apply_to_incrementals(fulls, incrs, today)
    policy.apply_to_logs(logs, today)

    groups = [(f"FULL   {full_dir}", fulls)]
    if incr_dir:
        groups.append((f"INCR   {incr_dir}", incrs))
    if log_dir:
        groups.append((f"LOG    {log_dir}", logs))

    text, _planned, _planned_bytes = report(groups, verbose)

    header = (
        f"Retention run {now:%Y-%m-%d %H:%M}  --  mode: "
        f"{'APPLY' if apply_changes else 'DRY RUN'}"
    )
    policy_line = (
        f"Policy: keep all of last {daily} days, then {weekly} weekly, then "
        f"{monthly} monthly (pick={pick_mode})"
    )

    if not quiet:
        out.write(header + "\n")
        out.write(policy_line + "\n")
        for path, why in skipped_f + skipped_i + skipped_l:
            out.write(f"  SKIP  {Path(path).name} ({why})\n")
        out.write(text + "\n")

    all_files = fulls + incrs + logs
    deleted, failed, freed = execute(all_files, apply_changes)

    verb = "deleted" if apply_changes else "would delete"
    out.write(
        f"{verb} {deleted} files, {human(freed)} reclaimed"
        f"{f', {failed} failures' if failed else ''}\n"
    )
    if not apply_changes and deleted:
        out.write("Dry run -- re-run with --apply to perform the deletions.\n")

    return EXIT_DELETE_FAILURES if failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """argparse-compatible entry point (script parity with the reference tool)."""
    import argparse

    p = argparse.ArgumentParser(
        description="GFS retention for backups (dry run unless --apply).")
    p.add_argument("--full-dir", required=True)
    p.add_argument("--incr-dir", default=None)
    p.add_argument("--log-dir", default=None)
    p.add_argument("--full-glob", default=DEFAULTS["full_glob"])
    p.add_argument("--incr-glob", default=DEFAULTS["incr_glob"])
    p.add_argument("--log-glob", default=DEFAULTS["log_glob"])
    p.add_argument("--daily", type=int, default=DEFAULTS["daily"])
    p.add_argument("--weekly", type=int, default=DEFAULTS["weekly"])
    p.add_argument("--monthly", type=int, default=DEFAULTS["monthly"])
    p.add_argument("--incr-days", type=int, default=DEFAULTS["incr_days"])
    p.add_argument("--log-days", type=int, default=DEFAULTS["log_days"])
    p.add_argument("--pick", choices=("first", "last"),
                   dest="pick_mode", default=DEFAULTS["pick"])
    p.add_argument("--min-keep-fulls", type=int, default=DEFAULTS["min_keep_fulls"])
    p.add_argument("--now", default=None, help="pretend it is this date (YYYY-MM-DD)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)

    now = None
    if args.now:
        try:
            now = datetime.strptime(args.now, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59
            )
        except ValueError:
            sys.stderr.write("ERROR: --now must be YYYY-MM-DD\n")
            return EXIT_USAGE

    return run_job(
        full_dir=args.full_dir,
        incr_dir=args.incr_dir,
        log_dir=args.log_dir,
        full_glob=args.full_glob,
        incr_glob=args.incr_glob,
        log_glob=args.log_glob,
        daily=args.daily,
        weekly=args.weekly,
        monthly=args.monthly,
        incr_days=args.incr_days,
        log_days=args.log_days,
        pick_mode=args.pick_mode,
        min_keep_fulls=args.min_keep_fulls,
        now=now,
        apply_changes=args.apply,
        verbose=args.verbose,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    sys.exit(main())
