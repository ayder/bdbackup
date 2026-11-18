"""The probe children: every check that touches a share runs in a child process (D8, D10).

A hard-mounted NFS share that hangs blocks any ``stat`` made against it, so the metrics process
never makes one. It sends its requests to one child per probe (``destination`` and
``repository``), which answers one flushed line per request, and gives them all one deadline.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
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


def _converse(group: list[Request], child: subprocess.Popen, deadline: float) -> tuple[dict, str]:
    """Send ``child`` its requests and read its lines until ``deadline``; its answers and status."""
    payload = json.dumps([_wire(request) for request in group]).encode()
    timed_out = False
    try:
        output, _ = child.communicate(payload, timeout=max(deadline - time.monotonic(), 0.0))
    except subprocess.TimeoutExpired as exc:
        output, timed_out = exc.stdout, True
        child.kill()  # and do not wait: on a hard mount it may not die until the I/O does
    except OSError:
        output = None
        child.kill()
    answers, well_formed = _parse(group, output)
    complete = well_formed and len(answers) == len(group)
    if timed_out:
        status = "ok" if complete else "timeout"
    else:
        status = "ok" if complete and child.returncode == 0 else "failed"
    _close(child)
    return answers, status


def run_probes(requests: list[Request], timeout_seconds: float) -> Results:
    """Run the requests in one child per probe, under one deadline; never raises.

    The children start first, and each is then talked to in a thread of its own, so a probe
    that hangs cannot take the time another one needs (D10). ``Results.status`` says for each
    probe that ran whether it finished (``ok``), hit the deadline (``timeout``; the lines it
    printed before are kept, the child is killed and not waited for) or failed (it could not
    start, exited nonzero, printed something that is not a line, or stopped before answering
    every request).
    """
    deadline = time.monotonic() + timeout_seconds
    groups: dict[str, list[Request]] = {}
    for request in requests:
        groups.setdefault(request.probe, []).append(request)
    status: dict[str, str] = {}
    children: dict[str, subprocess.Popen] = {}
    for name in groups:
        try:
            children[name] = subprocess.Popen(  # noqa: S603
                _child_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
        except (OSError, ValueError):
            status[name] = "failed"
    outcomes: dict[str, tuple[dict, str]] = {}

    def talk(name: str) -> None:
        outcomes[name] = _converse(groups[name], children[name], deadline)

    threads = [threading.Thread(target=talk, args=(name,), daemon=True) for name in children]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    answers: dict = {}
    for name in groups:
        if name in outcomes:
            got, status[name] = outcomes[name]
            answers.update(got)
        else:
            status.setdefault(name, "failed")  # a thread that died is not an answer
    return Results(answers, status)
