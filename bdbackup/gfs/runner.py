"""One GFS run: lock, load, decide, act, report (spec 2 §4).

Copies never hold an engine lock; only the commit of an xtrabackup unit takes the root lock of
the directory it leaves and the one it enters, and a busy lock defers the unit (§4.5).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bdbackup.config import Config, Job
from bdbackup.gfs import store
from bdbackup.gfs.actions import LocalTransport, Refusal, Transport, copy_verified, verify
from bdbackup.gfs.policy import Decision, Unit, decide
from bdbackup.gfs.stages import MARKER, Stage
from bdbackup.history import History
from bdbackup.utils import process_lock


def job_lock_path(config: Config, name: str) -> Path:
    """One run per gfs job: a lock file beside the live history database."""
    return Path(f"{config.history.database}.gfs-{name}.lock")


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
    lines: list[str] = field(default_factory=list)


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


class _Run:
    def __init__(self, config: Config, job: Job, today, out, transport: Transport):
        self.job = job
        self.stages: tuple[Stage, ...] = job.params["stage"]
        self.apply: bool = job.params["apply"]
        self.history = History(config.history)
        self.today = today
        self.out = out
        self.transport = transport
        self.tally = _Tally()

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
                              outcome, reason)

    def ensure_row(self, unit: store.ManagedUnit) -> store.ManagedUnit:
        if unit.id is not None or not self.apply:
            return unit
        unit_id = store.record_unit(self.history, unit)
        return store.ManagedUnit(unit_id, unit.unit, unit.series, unit.relative, unit.kind,
                                 unit.time, unit.members, unit.locations, unit.size)

    def refuse(self, unit, current, reason) -> None:
        self.tally.refused += 1
        self.line("refused", unit, current, None, f"refused: {reason}")
        self.step(unit, "refuse", None, None, "refused", reason)

    def remove_locations(self, unit, locations, *, verified: bool) -> None:
        for location in locations:
            if not verified:
                verify(unit, location.path)
            self.transport.remove(location.path)

    def complete_interrupted(self, unit, current: int, leftovers) -> store.ManagedUnit:
        """Locations in an earlier stage are left over from a move recorded but not finished."""
        if not self.apply:
            return unit
        with ExitStack() as stack:
            _engine_locks(stack, unit, [loc.path for loc in leftovers])
            self.remove_locations(unit, leftovers, verified=False)
            store.record_move(self.history, unit.id, [], leftovers)
        self.line("move", unit, _stage_index(self.stages, leftovers[0]), current,
                  "completes an interrupted move")
        self.step(unit, "move", leftovers[0].path, None, "ok", "completes an interrupted move")
        kept = tuple(loc for loc in unit.locations if loc not in leftovers)
        return store.ManagedUnit(unit.id, unit.unit, unit.series, unit.relative, unit.kind,
                                 unit.time, unit.members, kept, unit.size)

    def move(self, unit, current: int, decision: Decision) -> None:
        target = decision.target
        finals = [path / unit.relative for path in self.stages[target].paths]
        for path in self.stages[target].paths:
            if not (path / MARKER).is_file():
                raise Refusal(f"destination not ready: {path}")
        for final in finals:
            if self.transport.exists(final):
                raise Refusal(f"final name exists: {final}")
        if not self.apply:
            self.line("move", unit, current, target, decision.reason)
            return
        source = unit.locations[0].path
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
                store.record_move(self.history, unit.id, new, unit.locations)
                self.remove_locations(unit, unit.locations, verified=True)
        finally:
            for temp in temps:
                if self.transport.exists(temp):
                    self.transport.remove(temp)
        self.tally.moved += 1
        self.tally.moved_bytes += unit.size
        self.line("move", unit, current, target, decision.reason)
        self.step(unit, "move", source, finals[0], "ok", decision.reason)

    def delete(self, unit, current: int, decision: Decision) -> None:
        if not self.apply:
            self.line("delete", unit, current, None, decision.reason)
            return
        for location in unit.locations:
            verify(unit, location.path)
        with ExitStack() as stack:
            _engine_locks(stack, unit, [loc.path for loc in unit.locations])
            unit = self.ensure_row(unit)
            self.remove_locations(unit, unit.locations, verified=True)
            store.record_delete(self.history, unit.id)
        self.tally.deleted += 1
        self.tally.deleted_bytes += unit.size
        self.line("delete", unit, current, None, decision.reason)
        self.step(unit, "delete", unit.locations[0].path, None, "ok", decision.reason)

    def act(self, unit, current: int, decision: Decision) -> None:
        try:
            if decision.action == "move":
                self.move(unit, current, decision)
            elif decision.action == "delete":
                self.delete(unit, current, decision)
            elif decision.action == "hold":
                self.tally.held += 1
                self.line("hold", unit, current, None, decision.reason)
                self.step(unit, "hold", None, None, "held", decision.reason)
            else:
                self.ensure_row(unit)
        except Refusal as refusal:
            self.refuse(unit, current, refusal.reason)
        except _Deferred:
            self.tally.deferred += 1
            self.line("defer", unit, current, decision.target, "deferred: locked")
            self.step(unit, decision.action, None, None, "deferred", "locked")
        except Exception as exc:  # the next run retries; nothing was recorded as done
            self.tally.failed += 1
            self.line("failed", unit, current, decision.target, f"failed: {exc}")
            self.step(unit, decision.action, None, None, "failed", type(exc).__name__)

    def run(self) -> int:
        units, unmanaged = store.load(self.history, self.stages[0].paths[0])
        placed: dict[str, tuple[store.ManagedUnit, int]] = {}
        for unit in units:
            indexes = [_stage_index(self.stages, loc) for loc in unit.locations]
            if None in indexes or not indexes:
                self.refuse(self.ensure_row(unit), None, "stage not configured")
                continue
            current = max(indexes)
            leftovers = [loc for loc, i in zip(unit.locations, indexes, strict=True)
                         if i < current]
            if leftovers:
                try:
                    unit = self.complete_interrupted(unit, current, leftovers)
                except Refusal as refusal:
                    self.refuse(unit, current, refusal.reason)
                    continue
                except _Deferred:
                    self.tally.deferred += 1
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
        self.report(unmanaged)
        if self.tally.failed or self.tally.refused:
            return 1
        return 3 if self.tally.deferred else 0

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


def run_job(config: Config, job: Job, *, now: datetime | None = None,
            out: Callable[[str], None] = print, transport: Transport | None = None) -> int:
    """Run one gfs job; return 0, 1 (a failure or refusal) or 3 (locked or deferred only)."""
    today = (now or datetime.now()).date()
    try:
        with process_lock(job_lock_path(config, job.name)):
            return _Run(config, job, today, out, transport or LocalTransport()).run()
    except BlockingIOError:
        out(f"another run of {job.name} is in progress")
        return 3
