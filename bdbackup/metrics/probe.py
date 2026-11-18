"""The probe children: every check that touches a share runs in a child process (D8, D10).

A hard-mounted NFS share that hangs blocks any ``stat`` made against it, so the metrics process
never makes one. It sends its requests to one child per probe (``destination`` and
``repository``), which answers one flushed line per request, and gives them all one deadline.
"""

from __future__ import annotations

import subprocess
import sys

from bdbackup.metrics.collector import Request, Results

# The child reads the requests as JSON on stdin and prints one JSON line per request, in order,
# flushed at once. It uses only the standard library and never imports bdbackup.
CHILD_SOURCE = ""


def _child_argv() -> list[str]:
    """The command of a probe child; tests replace it with a child that hangs."""
    return [sys.executable, "-I", "-c", CHILD_SOURCE]


def run_probes(requests: list[Request], timeout_seconds: float) -> Results:
    """Not yet."""
    return Results()


__all__ = ["CHILD_SOURCE", "run_probes", "subprocess"]
