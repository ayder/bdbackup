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

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The child reads a JSON list of requests on stdin and prints one flushed JSON line per request,
# in order. A request names a stage path, the marker that says the share is mounted, the name
# of the ledger snapshot, the recorded locations to look at, and every path the ledger knows
# below the stage path. ``check`` is the whole of it; the tests run it in-process.
CHILD_SOURCE = r'''
import json, os, stat, sys

LOCK = ".bdbackup.lock"
TEMP = ".gfs-tmp-"


def device_of(path):
    return os.lstat(path).st_dev


def own_temporary(name):
    # bdbackup's own temporary names: a GFS copy, and a pgbackrest export's tar before its rename.
    return name.startswith(TEMP) or (name.startswith(".") and ".tar.tmp-" in name)


def walk(root, known, ancestors, skip_at_root):
    """The entries below ``root`` that are neither known nor a directory above a known path.

    It never follows a symlink and never enters another device: a directory whose device is
    not the root's counts as one entry. An unknown entry is one entry, whatever it holds.
    """
    unknown = 0
    device = device_of(root)
    pending = [(root, True)]
    while pending:
        directory, top = pending.pop()
        with os.scandir(directory) as scan:
            for entry in scan:
                name = entry.name
                path = os.path.join(directory, name)
                if name == LOCK or own_temporary(name) or (top and name in skip_at_root):
                    continue
                if path in known:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if device_of(path) != device:
                        unknown += 1
                    elif path in ancestors:
                        pending.append((path, False))
                    else:
                        unknown += 1
                else:
                    unknown += 1
    return unknown


def check(request):
    """``{"checked": True, "missing": n, "unknown": m}``, or why the path was not checked."""
    root = request["path"]
    # A path that is walked must carry its marker (an unmounted share has none). One that is
    # only looked at for missing locations, an unfed first stage, must exist.
    walked = request["walk"]
    problem = "no marker" if walked else "no path"
    wanted = stat.S_ISREG if walked else stat.S_ISDIR
    try:
        found = os.stat(os.path.join(root, request["marker"]) if walked else root)
    except (FileNotFoundError, NotADirectoryError):
        return {"checked": False, "problem": problem}
    except OSError as exc:
        return {"checked": False, "problem": "error: " + (exc.strerror or str(exc))}
    if not wanted(found.st_mode):
        return {"checked": False, "problem": problem}
    try:
        missing = 0
        for location in request["locations"]:
            try:
                os.lstat(location)
            except (FileNotFoundError, NotADirectoryError):
                missing += 1
        known = set(request["known"])
        ancestors = set()
        for path in known:
            parent = os.path.dirname(path)
            while parent.startswith(root + os.sep) and parent not in ancestors:
                ancestors.add(parent)
                parent = os.path.dirname(parent)
        unknown = None
        if walked:
            unknown = walk(root, known, ancestors, {request["marker"], request["snapshot"]})
    except OSError as exc:
        return {"checked": False, "problem": "error: " + (exc.strerror or str(exc))}
    return {"checked": True, "missing": missing, "unknown": unknown}


def main():
    for i, request in enumerate(json.load(sys.stdin)):
        line = dict(check(request), i=i)
        sys.stdout.write(json.dumps(line) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
'''


@dataclass(frozen=True)
class DriftRequest:
    """One stage path to check."""

    path: Path
    marker: str  # the file whose presence says the share is mounted (when the path is walked)
    snapshot: str  # the ledger snapshot's name, which is not an unknown entry
    locations: tuple[str, ...]  # the recorded locations in this stage path: each must exist
    known: tuple[str, ...]  # every path the ledger knows below it: none of them is unknown
    walk: bool = True  # False: only the locations are looked at, and the path must exist


@dataclass(frozen=True)
class DriftResult:
    """What the check found at one stage path; ``problem`` says why it was not checked."""

    path: Path
    checked: bool
    missing: int = 0
    unknown: int | None = 0  # None: the path was not walked
    problem: str = ""  # "no marker", "no path", "timeout" or "error: <what the system said>"


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
        "walk": request.walk,
    }


def _child_argv() -> list[str]:
    """The command of the child; tests replace it with one that hangs."""
    return [sys.executable, "-I", "-c", CHILD_SOURCE]


def _result(request: DriftRequest, line: dict) -> DriftResult:
    if line["checked"]:
        unknown = None if line["unknown"] is None else int(line["unknown"])
        return DriftResult(request.path, True, int(line["missing"]), unknown)
    return DriftResult(request.path, False, problem=str(line["problem"]))


def run_child(requests: list[DriftRequest], timeout_seconds: float) -> list[DriftResult]:
    """Check each request in one child under one deadline; never raises.

    A request the child did not answer before the deadline is ``timeout``: the child is killed
    and not waited for, as a hard mount may not release it until its I/O ends. A child that
    cannot start, or that stops early, leaves its unanswered requests with an ``error``.
    """
    if not requests:
        return []
    payload = json.dumps([_wire(request) for request in requests]).encode()
    timed_out = False
    try:
        child = subprocess.Popen(  # noqa: S603
            _child_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
    except (OSError, ValueError) as exc:
        return [DriftResult(r.path, False, problem=f"error: {exc}") for r in requests]
    try:
        output, _ = child.communicate(payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        output, timed_out = exc.stdout, True
        child.kill()
    except OSError:
        output = None
        child.kill()
    for pipe in (child.stdin, child.stdout):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    answered: dict[int, DriftResult] = {}
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else (output or "")
    for raw in text.split("\n")[:-1]:  # what follows the last newline is incomplete or empty
        try:
            line = json.loads(raw)
            answered[line["i"]] = _result(requests[line["i"]], line)
        except (ValueError, KeyError, TypeError, IndexError):
            break
    why = "timeout" if timed_out else "error: the drift check ended early"
    return [answered.get(i) or DriftResult(r.path, False, problem=why)
            for i, r in enumerate(requests)]
