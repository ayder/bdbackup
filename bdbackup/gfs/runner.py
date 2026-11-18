"""One GFS run: lock, load, decide, act, report (spec 2 §4).

Copies never hold an engine lock; only the commit of an xtrabackup unit takes the root lock of
the directory it leaves and the one it enters, and a busy lock defers the unit (§4.5).
"""

from __future__ import annotations

import errno
import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bdbackup.config import Config, Job
from bdbackup.gfs import store
from bdbackup.gfs.actions import (
    LocalTransport,
    Refusal,
    Transport,
    copy_verified,
    final_name_exists,
    verify,
)
from bdbackup.gfs.drift import Drift, DriftRequest, DriftResult
from bdbackup.gfs.drift import run_child as run_drift
from bdbackup.gfs.policy import Decision, Unit, decide
from bdbackup.gfs.stages import MARKER, Stage, fed_first_stages
from bdbackup.history import History, artifact_identity
from bdbackup.metrics.settings import DEFAULT_TIMEOUT_SECONDS
from bdbackup.utils import process_lock


@dataclass
class RunOutcome:
    """What ``run_job`` reports besides its exit code.

    ``locked`` is set when exit code 3 means another run of the job holds its lock, as opposed
    to a run that finished with deferred units.
    """

    locked: bool = False
    # The facts of a run that finished and was not a dry run (spec 16 section 6), for
    # ``last_runs.facts``: ``computed_at``, the reason ``reasons``, the ``moves`` by method and
    # the ``drift``. ``None`` for a dry run, a busy job lock and a run that raised.
    facts: dict | None = None


def job_lock_path(config: Config, name: str) -> Path:
    """One run per gfs job: a lock file beside the live history database."""
    return Path(f"{config.history.database}.gfs-{name}.lock")


def snapshot_name(job: Job) -> str:
    """The ledger snapshot written to every later stage path (spec 2 §4.3, D18)."""
    return f"{job.name}.ledger.sqlite3"


@dataclass
class _Tally:
    moved: int = 0
    moved_bytes: int = 0
    deleted: int = 0
    deleted_bytes: int = 0
    held: int = 0
    deferred: int = 0
    refused: int = 0
    failed: int = 0
    renamed: int = 0
    copied: int = 0
    # (stage from 1, 0 when not configured; outcome; reason class) -> units
    reasons: Counter = field(default_factory=Counter)
    lines: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)  # the report's Drift lines
    snapshots: list[str] = field(default_factory=list)


class _Deferred(Exception):
    pass


def _stage_index(stages: tuple[Stage, ...], location: store.Location) -> int | None:
    for index, stage in enumerate(stages):
        if location.stage_path in stage.paths:
            return index
    return None


def _engine_locks(stack: ExitStack, unit: store.ManagedUnit, paths: list[Path]) -> None:
    if unit.kind != "xtrabackup":
        return
    for parent in sorted({p.parent for p in paths}):
        try:
            stack.enter_context(process_lock(parent / ".bdbackup.lock"))
        except BlockingIOError:
            raise _Deferred() from None


def _identities(unit: store.ManagedUnit, at: Path) -> dict[int, str]:
    """Each member's artifact identity at a new location, so ``history`` detects replacement."""
    identities = {}
    for member in unit.members:
        relative = Path(member.path).relative_to(unit.unit)
        identities[member.id] = artifact_identity(at / relative if relative != Path(".") else at)
    return identities


class _Run:
    def __init__(self, config: Config, job: Job, today, out, transport: Transport,
                 dry_run: bool, outcome: RunOutcome | None = None,
                 drift: Drift = run_drift):
        self.job = job
        self.outcome = outcome
        self.drift = drift
        # The drift check has the deadline of a metrics run: [metrics] timeout_seconds, or 60.
        self.drift_timeout = (config.metrics.timeout_seconds if config.metrics is not None
                              else DEFAULT_TIMEOUT_SECONDS)
        self.drift_facts: list[dict] = []
        self.stages: tuple[Stage, ...] = job.params["stage"]
        # A pgbackrest job writes into the first stage: it has a marker and a ledger snapshot.
        self.fed = self.stages[0].paths[0] in fed_first_stages(config.jobs)
        self.apply = not dry_run
        self.history = History(config.history)
        self.today = today
        self.out = out
        self.transport = transport
        self.tally = _Tally()
        # Unit path -> (stage index or None once deleted, size) when the run ends (§4.6).
        self.where: dict[str, tuple[int | None, int]] = {}
        self.run_id: int | None = None

    def count(self, outcome: str, reason_class: str, index: int | None) -> None:
        """Count one unit that was ``outcome`` for ``reason_class``, in the stage ``index``."""
        self.tally.reasons[(0 if index is None else index + 1, outcome, reason_class)] += 1

    def label(self, index: int | None) -> str:
        return str(self.stages[index].paths[0]) if index is not None else "-"

    def line(self, action: str, unit: store.ManagedUnit, source: int | None,
             target: int | None, reason: str) -> None:
        prefix = "would " if not self.apply and action in ("move", "delete") else ""
        self.tally.lines.append(
            f"{prefix}{action} {unit.series} {unit.relative.as_posix()} {unit.time.date()} "
            f"{self.label(source)} -> {self.label(target)} ({reason})"
        )

    def step(self, unit: store.ManagedUnit, action: str, source, destination, outcome, reason):
        if self.apply:
            store.record_step(self.history, self.job.name, unit.id, action,
                              source and str(source), destination and str(destination),
                              outcome, reason, self.run_id)

    def ensure_row(self, unit: store.ManagedUnit) -> store.ManagedUnit:
        """Record the unit the first time a run acts on it."""
        if not self.apply or unit.id is not None:
            return unit
        return replace(unit, id=store.record_unit(self.history, unit))

    def defer_running(self, unit, indexes, backup: tuple[int, str]) -> None:
        """Its job is still backing up: no action of any kind, whatever its age (r8 R2)."""
        current = max(indexes) if indexes and None not in indexes else None
        if current is not None:
            self.where[str(unit.unit)] = (current, unit.size)
        record_id, started = backup
        self.tally.deferred += 1
        self.count("deferred", "backup_running", current)
        self.line("defer", unit, current, None,
                  f"deferred: backup running (record {record_id}, started {started})")
        self.step(unit, "defer", None, None, "deferred", "backup running")

    def refuse(self, unit, current, reason, reason_class: str = "other") -> None:
        self.tally.refused += 1
        self.count("refused", reason_class, current)
        self.line("refused", unit, current, None, f"refused: {reason}")
        self.step(unit, "refuse", None, None, "refused", reason)

    def verify(self, unit, location: store.Location) -> None:
        """Verify one location; its stage's ``verify = false`` skips the checksum re-read."""
        index = _stage_index(self.stages, location)
        verify(unit, location.path, checksums=index is None or self.stages[index].verify)

    def remove_locations(self, unit, locations, *, verified: bool) -> None:
        for location in locations:
            if not verified:
                self.verify(unit, location)
            self.transport.remove(location.path)

    def complete_interrupted(self, unit, current: int, leftovers) -> store.ManagedUnit:
        """Locations in an earlier stage are left over from a move recorded but not finished."""
        if not self.apply:
            return unit
        for location in unit.locations:  # never remove the earlier copy unless the new ones hold
            if location not in leftovers:
                self.verify(unit, location)
        with ExitStack() as stack:
            _engine_locks(stack, unit, [loc.path for loc in leftovers])
            self.remove_locations(unit, leftovers, verified=False)
            store.record_move(self.history, unit.id, [], leftovers)
        self.line("move", unit, _stage_index(self.stages, leftovers[0]), current,
                  "completes an interrupted move")
        self.step(unit, "move", leftovers[0].path, None, "ok", "completes an interrupted move")
        return replace(unit, locations=tuple(loc for loc in unit.locations
                                             if loc not in leftovers))

    def renamable(self, unit, target: int) -> bool:
        """A move is a rename when one path takes a one-location unit on the same filesystem.

        Decided before anything is read, from the two stage paths (D6).
        """
        paths = self.stages[target].paths
        return (len(paths) == 1 and len(unit.locations) == 1
                and self.transport.same_filesystem(unit.locations[0].stage_path, paths[0]))

    def rename(self, unit, stage_path: Path, final: Path) -> tuple[bool, store.ManagedUnit]:
        """Move a unit by renaming it; ``False`` when the paths turn out to be on two mounts.

        The source is verified under its own stage's flag, then under the engine locks the row
        is recorded, the rename is done and the new location written (D6).
        """
        location = unit.locations[0]
        self.verify(unit, location)
        renamed = False
        try:
            with ExitStack() as stack:
                _engine_locks(stack, unit, [location.path, final])
                unit = self.ensure_row(unit)
                self.transport.relocate(location.path, final)
                renamed = True
                store.record_move(
                    self.history, unit.id, [store.Location(stage_path, final)],
                    unit.locations, {final: _identities(unit, final)})
        except OSError as exc:
            if renamed or exc.errno != errno.EXDEV:
                raise
        return renamed, unit

    def move(self, unit, current: int, decision: Decision) -> None:
        target = decision.target
        finals = [path / unit.relative for path in self.stages[target].paths]
        for path in self.stages[target].paths:
            if not (path / MARKER).is_file():
                raise Refusal(f"destination not ready: {path}", "destination_not_ready")
        for final in finals:
            if self.transport.exists(final):
                raise final_name_exists(final)
        if not self.apply:
            self.line("move", unit, current, target, decision.reason)
            return
        source = unit.locations[0].path
        if self.renamable(unit, target):
            renamed, unit = self.rename(unit, self.stages[target].paths[0], finals[0])
            if renamed:
                self.tally.renamed += 1
                self.tally.moved += 1
                self.tally.moved_bytes += unit.size
                self.line("move", unit, current, target, f"{decision.reason}, renamed")
                self.step(unit, "move", source, finals[0], "ok", decision.reason)
                return
        for location in unit.locations[1:]:  # the source is hashed while it is copied
            self.verify(unit, location)
        temps: list[Path] = []
        try:
            for final in finals:
                temps.append(copy_verified(unit, source, final, self.transport))
            with ExitStack() as stack:
                _engine_locks(stack, unit, [*(loc.path for loc in unit.locations), *finals])
                for temp, final in zip(temps, finals, strict=True):
                    self.transport.rename(temp, final)
                temps = []
                unit = self.ensure_row(unit)
                new = [store.Location(path, final)
                       for path, final in zip(self.stages[target].paths, finals, strict=True)]
                store.record_move(self.history, unit.id, new, unit.locations,
                                  {final: _identities(unit, final) for final in finals})
                self.remove_locations(unit, unit.locations, verified=True)
        finally:
            for temp in temps:
                if self.transport.exists(temp):
                    self.transport.remove(temp)
        self.tally.copied += 1
        self.tally.moved += 1
        self.tally.moved_bytes += unit.size
        self.line("move", unit, current, target, decision.reason)
        for final in finals:
            self.step(unit, "move", source, final, "ok", decision.reason)

    def delete(self, unit, current: int, decision: Decision) -> None:
        if not self.apply:
            self.line("delete", unit, current, None, decision.reason)
            return
        for location in unit.locations:
            self.verify(unit, location)
        with ExitStack() as stack:
            _engine_locks(stack, unit, [loc.path for loc in unit.locations])
            unit = self.ensure_row(unit)
            self.remove_locations(unit, unit.locations, verified=True)
            store.record_delete(self.history, unit.id)
        self.tally.deleted += 1
        self.tally.deleted_bytes += unit.size
        self.line("delete", unit, current, None, decision.reason)
        for location in unit.locations:
            self.step(unit, "delete", location.path, None, "ok", decision.reason)

    def paths(self, unit, decision: Decision) -> list[tuple[Path | None, Path | None]]:
        """The (source, destination) of each step an action writes: one per path."""
        if decision.action == "move":
            return [(unit.locations[0].path, path / unit.relative)
                    for path in self.stages[decision.target].paths]
        if decision.action == "delete":
            return [(location.path, None) for location in unit.locations]
        return [(None, None)]

    def act(self, unit, current: int, decision: Decision) -> None:
        try:
            if decision.action == "move":
                self.move(unit, current, decision)
                self.where[str(unit.unit)] = (decision.target, unit.size)
            elif decision.action == "delete":
                self.delete(unit, current, decision)
                self.where[str(unit.unit)] = (None, unit.size)
            elif decision.action == "hold":
                self.tally.held += 1
                self.count("held", "period_incomplete", current)
                self.line("hold", unit, current, None, decision.reason)
                self.step(unit, "hold", None, None, "held", decision.reason)
            else:
                self.ensure_row(unit)
        except Refusal as refusal:
            self.refuse(unit, current, refusal.reason, refusal.reason_class)
        except _Deferred:
            self.tally.deferred += 1
            self.count("deferred", "locked", current)
            self.line("defer", unit, current, decision.target, "deferred: locked")
            self.step(unit, decision.action, None, None, "deferred", "locked")
        except Exception as exc:  # the next run retries; nothing was recorded as done
            self.tally.failed += 1
            self.count("failed", "error", current)
            self.line("failed", unit, current, decision.target, f"failed: {exc}")
            for source, destination in self.paths(unit, decision):
                self.step(unit, decision.action, source, destination, "failed",
                          str(exc) or type(exc).__name__)

    def run(self) -> int:
        """Run once; an applying run records its row in ``gfs_runs`` (spec 2 r8 R1)."""
        if self.apply:
            self.run_id = store.begin_run(self.history, self.job.name)
        try:
            return self._run()
        except BaseException:
            if self.run_id is not None:
                store.fail_run(self.history, self.run_id)
            raise

    def exit_code(self) -> int:
        if self.tally.failed or self.tally.refused:
            return 1
        return 3 if self.tally.deferred else 0

    def counts(self, unmanaged: int) -> dict[str, int]:
        t = self.tally
        return {"moved": t.moved, "deleted": t.deleted, "held": t.held,
                "deferred": t.deferred, "refused": t.refused, "failed": t.failed,
                "unmanaged": unmanaged}

    def finish(self, unmanaged: int) -> None:
        store.finish_run(self.history, self.run_id, self.exit_code(), self.counts(unmanaged))

    def _run(self) -> int:
        units, unmanaged, judgments = store.load(
            self.history, self.stages[0].paths[0],
            {path for stage in self.stages for path in stage.paths})
        if self.apply and judgments:  # S2 is judged once, whatever happens to the unit next
            store.record_members(self.history, judgments)
        running = store.running_backups(self.history)
        placed: dict[str, tuple[store.ManagedUnit, int]] = {}
        for unit in units:
            indexes = [_stage_index(self.stages, loc) for loc in unit.locations]
            if unit.series in running:
                self.defer_running(unit, indexes, running[unit.series])
                continue
            if None in indexes or not indexes:
                self.refuse(self.ensure_row(unit), None, "stage not configured",
                            "stage_not_configured")
                continue
            current = max(indexes)
            self.where[str(unit.unit)] = (current, unit.size)
            leftovers = [loc for loc, i in zip(unit.locations, indexes, strict=True)
                         if i < current]
            if leftovers:
                try:
                    unit = self.complete_interrupted(unit, current, leftovers)
                except Refusal as refusal:
                    self.refuse(unit, current, refusal.reason, refusal.reason_class)
                    continue
                except _Deferred:
                    self.tally.deferred += 1
                    self.count("deferred", "locked", current)
                    self.line("defer", unit, current, current, "deferred: locked")
                    continue
            placed[str(unit.unit)] = (unit, current)
        decisions = decide(
            [Unit(key, u.series, u.time.date(), current) for key, (u, current) in placed.items()],
            self.stages, self.today,
        )
        for decision in sorted(decisions, key=lambda d: (d.unit.day, str(d.unit.id))):
            unit, current = placed[decision.unit.id]
            self.act(unit, current, decision)
        self.check_drift()  # what the run leaves behind, before the snapshot is taken
        if self.apply:
            # The live row stays running through the snapshot step, so a run stuck there shows in
            # running_since. The local copy that goes to every stage gets the finished row
            # first, so each stage copy shows this run finished; a failed copy then shows only
            # in the live row's exit code and count.
            self.snapshot(unmanaged)
            self.finish(unmanaged)
            if self.outcome is not None:
                self.outcome.facts = self.facts()
        self.report(unmanaged)
        return self.exit_code()

    def facts(self) -> dict:
        """What this run leaves for ``last_runs.facts`` (spec 16 section 6)."""
        t = self.tally
        return {
            "computed_at": datetime.now(UTC).isoformat(),
            "reasons": [
                {"stage": stage, "outcome": outcome, "reason": reason, "units": units}
                for (stage, outcome, reason), units in sorted(t.reasons.items())
            ],
            "moves": {"rename": t.renamed, "copy": t.copied},
            "drift": self.drift_facts,
        }

    def check_drift(self) -> None:
        """Compare the destination stage paths with the ledger (spec 16 section 6.3).

        The later stage paths, and the first when a pgbackrest job writes into it, are checked
        in a child under one deadline. Nothing here raises, changes the exit code or blocks an
        action: a stage that could not be checked is reported and left out of the facts' counts.
        """
        destinations = [(index, path) for index, stage in enumerate(self.stages, 1)
                        if index > 1 or self.fed for path in stage.paths]
        try:
            units, _, _ = store.load(self.history, self.stages[0].paths[0],
                                     {path for stage in self.stages for path in stage.paths})
            recorded = [Path(r.path) for r in self.history.records(successful=True) if r.path]
            requests = []
            for _, path in destinations:
                locations = [str(loc.path) for unit in units for loc in unit.locations
                             if loc.stage_path == path]
                known = {*locations, *(str(r) for r in recorded if r.is_relative_to(path))}
                requests.append(DriftRequest(path, MARKER, snapshot_name(self.job),
                                             tuple(locations), tuple(sorted(known))))
            results = self.drift(requests, self.drift_timeout)
            if len(results) != len(requests):
                raise RuntimeError("the drift check answered for the wrong stage paths")
        except Exception as exc:
            results = [DriftResult(path, False, problem=f"failed: {exc}")
                       for _, path in destinations]
        timed_out = False
        for result in results:
            if result.checked:
                if result.missing or result.unknown:
                    self.tally.drift.append(f"Drift {result.path}: {result.missing} missing, "
                                            f"{result.unknown} unknown")
            elif result.problem == "timeout":
                if not timed_out:  # one line for every path the deadline cut off
                    self.tally.drift.append(
                        f"drift not checked: timeout after {self.drift_timeout} s")
                timed_out = True
            elif result.problem.startswith("error: "):
                self.tally.drift.append(
                    f"drift not checked: {result.path}: {result.problem[len('error: '):]}")
            elif result.problem.startswith("failed: "):
                if not any(line.startswith("drift not checked") for line in self.tally.drift):
                    why = result.problem[len("failed: "):]
                    self.tally.drift.append(f"drift not checked: {why}")
        self.drift_facts = []
        for index in sorted({index for index, _ in destinations}):
            here = [r for (i, _), r in zip(destinations, results, strict=True) if i == index]
            checked = all(r.checked for r in here)
            self.drift_facts.append({
                "stage": index, "checked": checked,
                "missing": sum(r.missing for r in here) if checked else 0,
                "unknown": sum(r.unknown for r in here) if checked else 0,
            })

    def snapshot_failed(self, path: Path, reason) -> None:
        self.tally.failed += 1
        self.count("failed", "snapshot", next(
            (i for i, stage in enumerate(self.stages) if path in stage.paths), None))
        self.tally.snapshots.append(f"failed snapshot {path}: {reason}")

    def snapshot(self, unmanaged: int) -> None:
        """Copy the whole ledger to every later stage path, whatever the actions' outcome.

        The first stage gets it too when a pgbackrest job feeds it, under the same marker rule.

        One consistent copy is made locally with SQLite's online backup and then copied to
        each path under a temporary name, fsynced and renamed, so SQLite never writes on a
        stage and every path receives the same bytes (spec 2 §4.3, D18). The copy carries this
        run's row as finished, with the counts it has now; the live row is finished after.
        """
        live = self.history.settings.database
        if not live.exists():
            self.tally.snapshots.append("no ledger to snapshot")
            return
        local = Path(f"{live}.gfs-{self.job.name}.snapshot")
        local.unlink(missing_ok=True)  # a leftover of an interrupted run; the job lock is held
        os.close(os.open(local, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        try:
            with (
                closing(sqlite3.connect(f"{live.as_uri()}?mode=ro", uri=True)) as source,
                closing(sqlite3.connect(local)) as target,
            ):
                source.backup(target)
                with target:  # the copy shows this run finished; the live row is still running
                    store.finish_run_in(
                        target, self.run_id, self.exit_code(), self.counts(unmanaged))
            fd = os.open(local, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            name = snapshot_name(self.job)
            for stage in self.stages if self.fed else self.stages[1:]:
                for path in stage.paths:
                    if not (path / MARKER).is_file():
                        self.snapshot_failed(path, "destination not ready")
                        continue
                    temp = path / f".gfs-tmp-{uuid4().hex}-{name}"
                    try:
                        self.transport.copy(local, temp, lambda _name, _digest: None)
                        self.transport.replace(temp, path / name)
                    except Exception as exc:  # reported; the next run writes it again
                        if self.transport.exists(temp):
                            self.transport.remove(temp)
                        self.snapshot_failed(path, exc)
                    else:
                        self.tally.snapshots.append(f"snapshot {path / name}")
        finally:
            local.unlink(missing_ok=True)

    def report(self, unmanaged: int) -> None:
        mode = "APPLY" if self.apply else "DRY RUN"
        self.out(f"GFS run {self.job.name} ({mode}) for {self.today}")
        for line in self.tally.lines:
            self.out(f"  {line}")
        t = self.tally
        self.out(
            f"Summary: moved {t.moved} ({t.moved_bytes} bytes), deleted {t.deleted} "
            f"({t.deleted_bytes} bytes), held {t.held}, deferred {t.deferred}, "
            f"refused {t.refused}, failed {t.failed}, unmanaged {unmanaged}"
        )
        if modes_refused := getattr(self.transport, "modes_refused", 0):
            self.out(f"permission bits not kept on {modes_refused} entries: "
                     "the destination refused chmod")
        for index, stage in enumerate(self.stages):
            sizes = [size for at, size in self.where.values() if at == index]
            units = "unit" if len(sizes) == 1 else "units"
            note = "" if stage.verify else ", checksums not re-read"
            self.out(f"Stage {stage.paths[0]}: {len(sizes)} {units} ({sum(sizes)} bytes){note}")
        for line in t.drift:
            self.out(line)
        for line in t.snapshots:
            self.out(line)


def run_job(config: Config, job: Job, *, now: datetime | None = None,
            out: Callable[[str], None] = print, transport: Transport | None = None,
            dry_run: bool = False, outcome: RunOutcome | None = None,
            drift: Drift | None = None) -> int:
    """Run one gfs job; return 0, 1 (a failure or refusal) or 3 (locked or deferred only).

    A dry run reports what would change and changes nothing. ``outcome``, when given, tells a
    busy job lock from deferred work, which both exit 3.
    """
    today = (now or datetime.now()).date()
    try:
        with process_lock(job_lock_path(config, job.name)):
            return _Run(
                config, job, today, out, transport or LocalTransport(), dry_run, outcome,
                drift or run_drift,
            ).run()
    except BlockingIOError:
        if outcome is not None:
            outcome.locked = True
        out(f"another run of {job.name} is in progress")
        return 3
