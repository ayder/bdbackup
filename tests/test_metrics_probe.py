"""The probe children: stat and listing answers, one deadline, and hangs that stay in the child."""

from __future__ import annotations

import ast
import errno
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import bdbackup
from bdbackup.config import Config
from bdbackup.history import History
from bdbackup.metrics import probe
from bdbackup.metrics.collector import (
    DESTINATION,
    REPOSITORY,
    Failure,
    ListAnswer,
    ListPrefixRequest,
    MetricError,
    StatAnswer,
    StatRequest,
)
from bdbackup.metrics.probe import CHILD_SOURCE, run_probes
from bdbackup.metrics.run import collect_all
from tests.test_gfs_run import Env
from tests.test_metrics_collect import (
    NOW,
    destination,
    in_process_probe,
    units,
    values,
)

PACKAGE = Path(bdbackup.__file__).parent


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def names(listing) -> list[str] | None:
    """The names in a listing answer, or ``None`` when the probe gave no listing."""
    return None if listing is None else [name for name, _ in listing.entries]


def stat_answer(path) -> StatAnswer:
    st = os.stat(path)
    return StatAnswer(st.st_mode, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


# A child that answers its first request, then hangs the way a dead NFS server does.
HANG = (
    "import json, sys, time\n"
    "requests = json.load(sys.stdin)\n"
    "print(json.dumps({'i': 0, 'ok': False, 'errno': 2}), flush=True)\n"
    "time.sleep(30)\n"
)
# A child that hangs before it answers any stat request but answers listings like the real one.
HANG_ON_STAT = (
    "import io, json, sys, time\n"
    "text = sys.stdin.read()\n"
    "if any(r['kind'] == 'stat' for r in json.loads(text)):\n"
    "    time.sleep(30)\n"
    "sys.stdin = io.StringIO(text)\n"
    f"exec({CHILD_SOURCE!r})\n"
)
SLEEP = "import time\ntime.sleep(30)\n"
# A child that answers its first request with a failure and then exits.
ANSWER_ONE = (
    "import json, sys\nsys.stdin.read()\n"
    "print(json.dumps({'i': 0, 'ok': False, 'errno': 2}))\n"
)


def use_child(monkeypatch, code: str) -> list:
    """Run ``code`` as the probe child, and return the list of the children it starts."""
    children = []
    real_popen = subprocess.Popen

    def spying(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(probe, "_child_argv", lambda: [sys.executable, "-c", code])
    monkeypatch.setattr(subprocess, "Popen", spying)
    return children


# ---- the real child ---------------------------------------------------------------------------


def test_stat_facts_of_a_file_a_directory_a_link_and_a_missing_path(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"abc")
    directory = tmp_path / "directory"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(file)
    missing = tmp_path / "missing"
    requests = [StatRequest(path) for path in (file, directory, link, missing)]

    results = run_probes(requests, 10)

    assert results.stat(file) == stat_answer(file)
    assert results.stat(directory) == stat_answer(directory)
    assert results.stat(link) == stat_answer(file)  # a link is followed, as artifact_identity does
    assert results.stat(missing) is None
    assert results.answers.get(StatRequest(missing)) == Failure(errno.ENOENT)
    assert results.probe_status(DESTINATION) == "ok"
    assert results.probe_status(REPOSITORY) is None  # that probe did not run


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any directory")
def test_a_path_behind_an_unsearchable_directory_is_a_failure(tmp_path):
    closed = tmp_path / "closed"
    closed.mkdir()
    (closed / "marker").write_text("")
    closed.chmod(0)
    try:
        results = run_probes([StatRequest(closed / "marker")], 10)
    finally:
        closed.chmod(0o700)

    assert results.answers.get(StatRequest(closed / "marker")) == Failure(errno.EACCES)
    assert results.probe_status(DESTINATION) == "ok"


def test_a_listing_reports_the_entries_that_start_with_the_prefix(tmp_path):
    directory = tmp_path / "wal"
    directory.mkdir()
    for name in ("0001-aa.gz", "0001-bb.gz", "0002-aa.gz"):
        (directory / name).write_bytes(b"x")
    (directory / "0001-dangling").symlink_to(tmp_path / "gone")  # matches, cannot be stat'ed
    (directory / "zz-dangling").symlink_to(tmp_path / "gone")  # does not match: never stat'ed
    wanted = ListPrefixRequest(directory, "0001-")
    none = ListPrefixRequest(directory, "9999-")
    absent = ListPrefixRequest(tmp_path / "absent", "0001-")

    results = run_probes([wanted, none, absent], 10)

    listing = results.listing(directory, "0001-")
    assert names(listing) == ["0001-aa.gz", "0001-bb.gz"]
    assert [mtime for _, mtime in listing.entries] == [
        os.stat(directory / name).st_mtime for name in ("0001-aa.gz", "0001-bb.gz")
    ]
    assert results.answers.get(none) == ListAnswer(())
    assert results.listing(directory, "9999-") == ListAnswer(())
    assert results.answers.get(absent) == Failure(errno.ENOENT)
    assert results.probe_status(REPOSITORY) == "ok"
    assert results.probe_status(DESTINATION) is None


def test_requests_of_both_probes_are_answered_by_their_own_child(tmp_path):
    file = tmp_path / "file"
    file.write_text("x")
    listing = ListPrefixRequest(tmp_path, "fi")

    results = run_probes([StatRequest(file), listing], 10)

    assert results.stat(file) == stat_answer(file)
    assert names(results.listing(tmp_path, "fi")) == ["file"]
    assert dict(results.status) == {DESTINATION: "ok", REPOSITORY: "ok"}


def test_no_requests_run_no_child_and_each_probe_runs_one(tmp_path, monkeypatch):
    children = use_child(monkeypatch, ANSWER_ONE)

    empty = run_probes([], 5)

    assert children == []
    assert dict(empty.status) == {} and dict(empty.answers) == {}
    both = run_probes([StatRequest(tmp_path), ListPrefixRequest(tmp_path, "x")], 5)
    assert len(children) == 2  # one child for each probe, whatever the number of requests
    assert dict(both.status).keys() == {DESTINATION, REPOSITORY}


def test_the_child_uses_only_the_standard_library():
    assert "import json" in CHILD_SOURCE and "os.stat" in CHILD_SOURCE
    assert "bdbackup" not in CHILD_SOURCE
    assert "subprocess" not in CHILD_SOURCE


# ---- a hang -----------------------------------------------------------------------------------


def test_a_child_that_hangs_gives_its_lines_so_far_and_returns_at_the_deadline(
    tmp_path, monkeypatch
):
    children = use_child(monkeypatch, HANG)
    first, second = StatRequest(tmp_path / "a"), StatRequest(tmp_path / "b")

    started = time.monotonic()
    results = run_probes([first, second], 1)
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert results.answers == {first: Failure(errno.ENOENT)}  # the one line it printed
    assert results.probe_status(DESTINATION) == "timeout"
    # The child was killed, and the parent did not wait for it to be reaped.
    assert children[0].wait(timeout=10) == -signal.SIGKILL


def test_one_probe_that_hangs_leaves_the_other_probes_answers_intact(tmp_path, monkeypatch):
    use_child(monkeypatch, HANG_ON_STAT)
    (tmp_path / "wal-1").write_bytes(b"x")
    listing = ListPrefixRequest(tmp_path, "wal-")
    stat = StatRequest(tmp_path / "wal-1")

    started = time.monotonic()
    results = run_probes([stat, listing], 1)
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert results.probe_status(DESTINATION) == "timeout"
    assert results.probe_status(REPOSITORY) == "ok"
    assert names(results.listing(tmp_path, "wal-")) == ["wal-1"]
    assert results.stat(tmp_path / "wal-1") is None


def test_a_child_that_cannot_start_is_a_failed_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_child_argv", lambda: ["/no/such/python"])

    results = run_probes([StatRequest(tmp_path)], 5)

    assert results.probe_status(DESTINATION) == "failed"
    assert dict(results.answers) == {}


@pytest.mark.parametrize(
    ("code", "answered"),
    [
        ("import sys\nsys.exit(3)\n", 0),
        ("print('not json')\n", 0),
        ("import sys, json\nprint(json.dumps({'i': 0, 'ok': True}), flush=True)\n", 0),
        ("import json, sys\nprint(json.dumps({'i': 0, 'ok': False, 'errno': 2}))\nsys.exit(1)", 1),
        # exits 0 without answering the second request
        ("import json\nprint(json.dumps({'i': 0, 'ok': False, 'errno': 2}))\n", 1),
    ],
    ids=["nonzero", "garbage", "missing-facts", "answer-then-nonzero", "incomplete"],
)
def test_a_child_that_exits_nonzero_prints_garbage_or_stops_early_is_a_failed_probe(
    tmp_path, monkeypatch, code, answered
):
    use_child(monkeypatch, code)

    results = run_probes([StatRequest(tmp_path / "a"), StatRequest(tmp_path / "b")], 5)

    assert results.probe_status(DESTINATION) == "failed"
    assert len(results.answers) == answered


def test_the_child_does_not_inherit_the_parents_descriptors(tmp_path, monkeypatch):
    held = tmp_path / "held.lock"
    fd = os.open(held, os.O_RDWR | os.O_CREAT, 0o644)
    os.set_inheritable(fd, True)  # as if the parent had been started with it open
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    children = use_child(monkeypatch, SLEEP)
    thread = threading.Thread(target=run_probes, args=([StatRequest(tmp_path)], 30))
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not children and time.monotonic() < deadline:
            time.sleep(0.01)
        assert children and children[0].poll() is None
        os.close(fd)  # without LOCK_UN: only a process that still has the descriptor keeps it
        check = "import fcntl, sys; fcntl.flock(open(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)"
        free = subprocess.run(  # noqa: S603
            [sys.executable, "-c", check, str(held)], capture_output=True, text=True
        )
        assert children[0].poll() is None  # the child is still there
    finally:
        for child in children:
            child.kill()
        thread.join(timeout=10)
    assert free.returncode == 0, free.stderr


# ---- wired into the collectors ----------------------------------------------------------------


def run_for(env, **kwargs):
    config = Config(env.config_path)
    return collect_all(config.jobs, History(config.history), now=NOW, timeout_seconds=10, **kwargs)


def comparable(collected):
    return sorted(
        (s.family, tuple(sorted(s.labels.items())), s.value) for s in collected.samples
    )


def test_the_collectors_use_the_real_probe_by_default(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    (env.root / "NFS/weekly/.bdbackup-destination").unlink()

    collected = run_for(env)  # no probe given

    assert destination(collected) == {
        ("gfs-main", "2", str(env.abs("NFS/daily"))): 1,
        ("gfs-main", "3", str(env.abs("NFS/weekly"))): 0,
    }
    assert units(collected) == {("files", "available"): 2, ("files", "unavailable"): 0,
                                ("files", "deleted"): 0}
    assert collected.errors == []
    config = Config(env.config_path)
    in_process = collect_all(
        config.jobs, History(config.history), now=NOW, timeout_seconds=10, probe=in_process_probe
    )
    assert comparable(collected) == comparable(in_process)


def test_a_hung_destination_is_one_error_and_the_rest_is_still_written(env, monkeypatch):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    children = use_child(monkeypatch, HANG)
    config = Config(env.config_path)

    started = time.monotonic()
    collected = collect_all(
        config.jobs, History(config.history), now=NOW, timeout_seconds=1
    )
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert collected.errors == [MetricError("destination", "", "timeout")]
    assert destination(collected) == {  # a path with no line reads 0
        ("gfs-main", "2", str(env.abs("NFS/daily"))): 0,
        ("gfs-main", "3", str(env.abs("NFS/weekly"))): 0,
    }
    assert units(collected) == {}  # the history states need the probe
    assert len(values(collected, "bdbackup_gfs_stage_units", "job_name", "stage")) == 3
    assert children[0].wait(timeout=10) == -signal.SIGKILL


# ---- no subprocess.run, and no import of config or cli, in the metrics code -------------------


def runtime_imports(path: Path) -> set[str]:
    """Modules a file imports outside ``if TYPE_CHECKING`` blocks."""
    tree = ast.parse(path.read_text())
    found = set()

    def visit(nodes):
        for node in nodes:
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                continue
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
            for child in ast.iter_child_nodes(node):
                visit([child])

    visit(tree.body)
    return found


def test_no_metrics_module_imports_config_or_cli_at_runtime():
    for path in (PACKAGE / "metrics").glob("*.py"):
        imported = runtime_imports(path)
        assert not {"bdbackup.config", "bdbackup.cli"} & imported, path.name
    engines = runtime_imports(PACKAGE / "engines.py")
    assert not any(name.startswith("bdbackup.metrics") for name in engines)


def test_no_metrics_module_calls_subprocess_run():
    for path in (PACKAGE / "metrics").glob("*.py"):
        assert "subprocess.run" not in path.read_text(), path.name


def test_subprocess_run_stays_in_pgbackrest_run_for_the_postgres_engine():
    """The integration harness replaces ``subprocess.run`` of pgbackrest's module; that must stay
    the one place the engine runs a command."""
    calls = []
    for path in (PACKAGE / "postgres").glob("*.py"):
        tree = ast.parse(path.read_text())
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)] or [tree]:
            for function in [n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)]:
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.run":
                        calls.append((path.name, function.name))
    assert set(calls) == {("pgbackrest.py", "_run")}


def test_the_probe_is_started_with_popen_and_closed_descriptors():
    source = (PACKAGE / "metrics" / "probe.py").read_text()
    assert "subprocess.Popen(" in source and "close_fds=True" in source
