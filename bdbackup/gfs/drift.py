"""Drift: what the later stage paths hold against what the ledger records (spec 16 section 6.3).

A stage path can differ from the ledger without any action looking at it: a unit removed by
hand, a rename a crash cut short, a share remounted onto the wrong export. Drift counts, per
stage path, the recorded locations that are not there (``missing``) and the entries that are
not recorded (``unknown``). It reads the filesystem's metadata only, never a unit's data.

Every check that touches a share runs in a child process under one deadline, as the metrics
probes do: a hung hard mount must not hang the run. The child's source imports nothing from
bdbackup, so it starts fast and cannot import a share-bound module.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The child reads a JSON list of requests on stdin and prints one flushed JSON line per request,
# in order. A request names a stage path, the marker that says the share is mounted, the name
# of the ledger snapshot, the recorded locations to look at, and every path the ledger knows
# below the stage path. ``check`` is the whole of it; the tests run it in-process.
CHILD_SOURCE = r'''
import json, sys


def check(request):
    return {"checked": False, "problem": "stub: nothing is checked yet"}


def main():
    for i, request in enumerate(json.load(sys.stdin)):
        sys.stdout.write(json.dumps(dict(check(request), i=i)) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
'''


@dataclass(frozen=True)
class DriftRequest:
    """One stage path to check."""

    path: Path
    marker: str  # the file whose presence says the share is mounted
    snapshot: str  # the ledger snapshot's name, which is not an unknown entry
    locations: tuple[str, ...]  # the recorded locations in this stage path: each must exist
    known: tuple[str, ...]  # every path the ledger knows below it: none of them is unknown


@dataclass(frozen=True)
class DriftResult:
    """What the check found at one stage path; ``problem`` says why it was not checked."""

    path: Path
    checked: bool
    missing: int = 0
    unknown: int = 0
    problem: str = ""  # "no marker", "timeout" or "error: <what the system said>"


Drift = Callable[[list[DriftRequest], float], list[DriftResult]]


def load_child() -> dict:
    """The child's code as a namespace, for a test or a stand-in that runs it in-process."""
    namespace = {"__name__": "gfs_drift_child"}
    exec(compile(CHILD_SOURCE, "<gfs drift child>", "exec"), namespace)  # noqa: S102
    return namespace


def _wire(request: DriftRequest) -> dict:
    return {
        "path": str(request.path), "marker": request.marker, "snapshot": request.snapshot,
        "locations": list(request.locations), "known": list(request.known),
    }


def _child_argv() -> list[str]:
    """The command of the child; tests replace it with one that hangs."""
    return [sys.executable, "-I", "-c", CHILD_SOURCE]


def _result(request: DriftRequest, line: dict) -> DriftResult:
    if line["checked"]:
        return DriftResult(request.path, True, int(line["missing"]), int(line["unknown"]))
    return DriftResult(request.path, False, problem=str(line["problem"]))


def run_child(requests: list[DriftRequest], timeout_seconds: float) -> list[DriftResult]:
    """Stub: nothing is checked yet."""
    return [DriftResult(r.path, False, problem="stub: nothing is checked yet") for r in requests]
