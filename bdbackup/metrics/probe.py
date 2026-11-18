"""The probe children: every check that touches a share runs in a child process (D8, D10).

A hard-mounted NFS share that hangs blocks any ``stat`` made against it, so the metrics process
never makes one. It sends its requests to one child per probe (``destination`` and
``repository``), which answers one flushed line per request, and gives them all one deadline.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from bdbackup.metrics.collector import (
    Failure,
    ListAnswer,
    Request,
    Results,
    StatAnswer,
    StatRequest,
)

# The child reads the requests as JSON on stdin and prints one JSON line per request, in order,
# flushed at once. It uses only the standard library and never imports bdbackup. A listing
# reads one directory with one ``scandir`` and ``stat``s only the entries that match.
CHILD_SOURCE = """
import json, os, sys

def stat_line(i, path):
    st = os.stat(path)
    return {"i": i, "ok": True, "mode": st.st_mode, "dev": st.st_dev, "ino": st.st_ino,
            "size": st.st_size, "mtime_ns": st.st_mtime_ns}

def list_line(i, directory, prefix):
    entries = []
    with os.scandir(directory) as scan:
        for entry in scan:
            if entry.name.startswith(prefix):
                try:
                    entries.append({"name": entry.name, "mtime": entry.stat().st_mtime})
                except OSError:
                    pass
    return {"i": i, "ok": True, "entries": entries}

for i, request in enumerate(json.load(sys.stdin)):
    try:
        if request["kind"] == "stat":
            line = stat_line(i, request["path"])
        else:
            line = list_line(i, request["directory"], request["prefix"])
    except OSError as exc:
        line = {"i": i, "ok": False, "errno": exc.errno}
    sys.stdout.write(json.dumps(line) + "\\n")
    sys.stdout.flush()
"""

# Time a probe is still given to hand over what its child already wrote, when the deadline of
# the run has passed while an earlier probe was waited on.
_GRACE_SECONDS = 0.05


def _child_argv() -> list[str]:
    """The command of a probe child; tests replace it with a child that hangs."""
    return [sys.executable, "-I", "-c", CHILD_SOURCE]


def _wire(request: Request) -> dict:
    if isinstance(request, StatRequest):
        return {"kind": "stat", "path": str(request.path)}
    return {"kind": "list", "directory": str(request.directory), "prefix": request.prefix}


def _answer(request: Request, line: dict) -> StatAnswer | ListAnswer | Failure:
    """The answer in one line of the child's output; raises when the line is not one."""
    if not line["ok"]:
        errno = line.get("errno")
        return Failure(errno if isinstance(errno, int) else None)
    if isinstance(request, StatRequest):
        return StatAnswer(
            int(line["mode"]), int(line["dev"]), int(line["ino"]), int(line["size"]),
            int(line["mtime_ns"]),
        )
    entries = sorted((str(e["name"]), float(e["mtime"])) for e in line["entries"])
    return ListAnswer(tuple(entries))


def _parse(requests: list[Request], output: bytes | str | None) -> tuple[dict, bool]:
    """The answers in the child's output so far, and whether all of it was well formed.

    The output may stop in the middle of a line; that last, incomplete line is ignored.
    """
    if not output:
        return {}, True
    text = output if isinstance(output, str) else output.decode("utf-8", "replace")
    lines = text.split("\n")[:-1]  # what follows the last newline is incomplete or empty
    answers: dict = {}
    for raw in lines:
        try:
            line = json.loads(raw)
            index = line["i"]
            if type(index) is not int or not 0 <= index < len(requests):
                raise ValueError("no such request")
            answers[requests[index]] = _answer(requests[index], line)
        except (ValueError, KeyError, TypeError, AttributeError):
            return answers, False
    return answers, True


def _close(child: subprocess.Popen) -> None:
    """Close the parent's ends of a child's pipes; nothing is left buffered to flush."""
    for pipe in (child.stdin, child.stdout):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass


def run_probes(requests: list[Request], timeout_seconds: float) -> Results:
    """Run the requests in one child per probe, under one deadline; never raises.

    ``Results.status`` says for each probe that ran whether it finished (``ok``), hit the
    deadline (``timeout``; the lines it printed before are kept, the child is killed and not
    waited for) or failed (it could not start, exited nonzero, printed something that is not a
    line, or stopped before answering every request).
    """
    deadline = time.monotonic() + timeout_seconds
    groups: dict[str, list[Request]] = {}
    for request in requests:
        groups.setdefault(request.probe, []).append(request)
    answers: dict = {}
    status: dict[str, str] = {}
    children = []
    for name, group in groups.items():  # every child starts before any is waited on
        try:
            child = subprocess.Popen(  # noqa: S603
                _child_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
        except (OSError, ValueError):
            status[name] = "failed"
            continue
        children.append((name, group, child))
    for name, group, child in children:
        payload = json.dumps([_wire(request) for request in group]).encode()
        remaining = max(deadline - time.monotonic(), _GRACE_SECONDS)
        timed_out = False
        try:
            output, _ = child.communicate(payload, timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            output, timed_out = exc.stdout, True
            child.kill()  # and do not wait: on a hard mount it may not die until the I/O does
        except OSError:
            output = None
            child.kill()
        got, well_formed = _parse(group, output)
        answers.update(got)
        complete = well_formed and len(got) == len(group)
        if timed_out:
            status[name] = "ok" if complete else "timeout"
        else:
            status[name] = "ok" if complete and child.returncode == 0 else "failed"
        _close(child)
    return Results(answers, status)
