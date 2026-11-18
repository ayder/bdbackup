"""The collectors: job, history and GFS series from the history database, and their isolation."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from bdbackup.backends import BackupResult
from bdbackup.config import Config, Job
from bdbackup.engines import EngineInfo, _reset_for_tests, list_engines, register_engine
from bdbackup.gfs import runner
from bdbackup.gfs import store as gfs_store
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History, artifact_identity, stat_identity
from bdbackup.metrics.collector import (
    DESTINATION,
    Failure,
    MetricError,
    Results,
    StatAnswer,
    StatRequest,
)
from bdbackup.metrics.registry import Family
from bdbackup.metrics.render import Sample
from bdbackup.metrics.run import collect_all
from tests.test_gfs_run import Env

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
JOB_LABELS = ("job_name", "job_type")


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    """Give each test a fresh engine registry isolated from the user's config dir."""
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture(autouse=True)
def _no_real_pgbackrest(monkeypatch):
    """A pgbackrest job is collected with ``pgbackrest info``; never run a real binary here."""

    def refuse(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr("bdbackup.postgres.pgbackrest.subprocess.run", refuse)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def in_process_probe(requests, timeout_seconds):
    """What the probe child does, done here: answer each stat request from the filesystem."""
    answers = {}
    for request in requests:
        try:
            st = os.stat(request.path)
        except OSError as exc:
            answers[request] = Failure(exc.errno)
        else:
            answers[request] = StatAnswer(
                st.st_mode, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns
            )
    return Results(answers, {request.probe: "ok" for request in requests} or {DESTINATION: "ok"})


def collect(env, *, jobs=None, probe=in_process_probe):
    config = Config(env.config_path)
    return collect_all(
        jobs if jobs is not None else config.jobs,
        History(config.history),
        now=NOW,
        timeout_seconds=5,
        probe=probe,
    )


def values(collected, family, *labels):
    """``label values -> value`` of one family."""
    return {
        tuple(s.labels[name] for name in labels): s.value
        for s in collected.samples
        if s.family == family
    }


def epoch(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


def sql(env, statement, *args):
    with closing(sqlite3.connect(env.database)) as db, db:
        return db.execute(statement, args).fetchall()


def touch_database(env):
    """Create the schema-6 history database without a record of any configured job."""
    env.history.record_last_run("unrelated", "file", 0, "completed")


def add_pg(env, extra=""):
    env.feed_first_stage()
    if extra:
        env.config_path.write_text(env.config_path.read_text() + extra)


def success(env, job, label, size, *, kind="pgbackrest-full"):
    artifact = env.root / "BACKUP" / "pg" / f"{job}-{label}.tar"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"x" * size)
    env.history.run(job, kind, lambda: BackupResult(artifact, size, True), {"label": label})


# ---- the generic job families --------------------------------------------------------------


def test_last_run_series_exist_for_a_job_with_a_last_runs_row_only(env):
    add_pg(env)
    env.history.record_last_run("pg", "pgbackrest", 1, "export_failed")

    collected = collect(env)

    finished = env.history.last_runs()["pg"].finished_at
    pg = ("pg", "pgbackrest")
    assert values(collected, "bdbackup_job_last_run_timestamp_seconds", *JOB_LABELS) == {
        pg: epoch(finished)
    }
    assert values(collected, "bdbackup_job_last_run_exit_code", *JOB_LABELS) == {pg: 1}
    assert values(collected, "bdbackup_job_last_run_result", *JOB_LABELS, "result") == {
        ("pg", "pgbackrest", "export_failed"): 1
    }
    # gfs-main has no row, so it has no last_run series.
    assert all(
        s.labels["job_name"] != "gfs-main"
        for s in collected.samples
        if s.family.startswith("bdbackup_job_last_run_")
    )


def test_both_job_types_that_record_last_runs_appear(env):
    add_pg(env)
    env.history.record_last_run("pg", "pgbackrest", 0, "exported")
    env.history.record_last_run("gfs-main", "gfs", 3, "deferred")

    collected = collect(env)

    assert values(collected, "bdbackup_job_last_run_exit_code", *JOB_LABELS) == {
        ("pg", "pgbackrest"): 0,
        ("gfs-main", "gfs"): 3,
    }


def test_a_job_type_that_does_not_record_last_runs_is_left_out(env):
    env.config_path.write_text(
        env.config_path.read_text()
        + '[files]\ntype = "file"\ntemplate_filename = "paths"\nchdir = "source"\n'
        'backup_dst = "archives"\n'
    )
    env.history.record_last_run("files", "file", 0, "completed")
    add_pg(env)
    env.history.record_last_run("pg", "pgbackrest", 0, "exported")

    collected = collect(env)

    assert values(collected, "bdbackup_job_last_run_exit_code", *JOB_LABELS) == {
        ("pg", "pgbackrest"): 0  # and not the file job
    }


def test_an_inactive_job_is_left_out(env):
    add_pg(env, "active = false\n")
    env.history.record_last_run("pg", "pgbackrest", 0, "exported")
    env.history.record_last_run("gfs-main", "gfs", 0, "completed")

    collected = collect(env)

    assert values(collected, "bdbackup_job_last_run_exit_code", *JOB_LABELS) == {
        ("gfs-main", "gfs"): 0  # and not the inactive pg job
    }


def test_a_pgbackrest_job_running_since_comes_from_backup_runs(env):
    add_pg(env)
    touch_database(env)
    sql(
        env,
        "INSERT INTO backup_runs (job_name, backup_type, started_at, status, restore_info)"
        " VALUES ('pg', 'pgbackrest-full', '2026-11-18T10:00:00+00:00', 'running', '{}'),"
        " ('pg', 'pgbackrest-full', '2026-11-18T11:00:00+00:00', 'running', '{}')",
    )

    collected = collect(env)

    started = int(datetime(2026, 11, 18, 10, tzinfo=UTC).timestamp())
    assert values(collected, "bdbackup_job_running_since_timestamp_seconds", *JOB_LABELS) == {
        ("pg", "pgbackrest"): started  # the oldest unfinished run
    }


def test_a_gfs_job_running_since_comes_from_gfs_runs(env):
    touch_database(env)
    sql(
        env,
        "INSERT INTO gfs_runs (gfs_job, started_at, status) VALUES"
        " ('gfs-main', '2026-11-18T09:00:00+00:00', 'running'),"
        " ('gfs-main', '2026-11-18T10:00:00+00:00', 'completed'),"
        " ('other', '2026-11-18T08:00:00+00:00', 'running')",
    )
    sql(env, "UPDATE gfs_runs SET exit_code = 0, finished_at = started_at WHERE status='completed'")

    collected = collect(env)

    assert values(collected, "bdbackup_job_running_since_timestamp_seconds", *JOB_LABELS) == {
        ("gfs-main", "gfs"): int(datetime(2026, 11, 18, 9, tzinfo=UTC).timestamp())
    }


def test_no_running_run_means_no_running_since_series(env):
    add_pg(env)
    env.history.record_last_run("pg", "pgbackrest", 0, "exported")

    collected = collect(env)

    assert values(collected, "bdbackup_job_last_run_exit_code", *JOB_LABELS)  # it was collected
    assert values(collected, "bdbackup_job_running_since_timestamp_seconds", *JOB_LABELS) == {}


def test_a_pgbackrest_jobs_last_success_is_its_newest_successful_backup(env):
    add_pg(env)
    success(env, "pg", "L1", 111)
    success(env, "pg", "L2", 222)
    sql(env, "INSERT INTO backup_runs (job_name, backup_type, started_at, status, restore_info)"
             " VALUES ('pg', 'pgbackrest-full', '2026-11-18T10:00:00+00:00', 'failed', '{}')")
    newest = env.history.records(job="pg", successful=True)[0]

    collected = collect(env)

    pg = ("pg", "pgbackrest")
    assert values(collected, "bdbackup_job_last_success_timestamp_seconds", *JOB_LABELS) == {
        pg: epoch(newest.completed_at)
    }
    assert values(collected, "bdbackup_job_last_success_bytes", *JOB_LABELS) == {pg: 222}


def test_a_gfs_jobs_last_success_is_its_newest_completed_run_with_exit_code_0(env):
    touch_database(env)
    sql(
        env,
        "INSERT INTO gfs_runs (gfs_job, started_at, finished_at, status, exit_code) VALUES"
        " ('gfs-main', '2026-11-18T08:00:00+00:00', '2026-11-18T08:05:00+00:00', 'completed', 0),"
        " ('gfs-main', '2026-11-18T09:00:00+00:00', '2026-11-18T09:05:00+00:00', 'completed', 1),"
        " ('gfs-main', '2026-11-18T10:00:00+00:00', '2026-11-18T10:05:00+00:00', 'failed', NULL)",
    )

    collected = collect(env)

    expected = int(datetime(2026, 11, 18, 8, 5, tzinfo=UTC).timestamp())
    assert values(collected, "bdbackup_job_last_success_timestamp_seconds", *JOB_LABELS) == {
        ("gfs-main", "gfs"): expected
    }
    assert values(collected, "bdbackup_job_last_success_bytes", *JOB_LABELS) == {}  # none for gfs


FACTS = {"phases": {"write": 2.5, "verify": 3.0}, "label": "L1", "bytes": 111}


def test_phase_seconds_appear_only_when_the_facts_describe_the_newest_success(env):
    add_pg(env)
    success(env, "pg", "L1", 111)
    env.history.record_last_run("pg", "pgbackrest", 0, "exported", FACTS)

    described = collect(env)
    success(env, "pg", "L2", 222)  # a newer export the facts do not describe
    stale = collect(env)

    phases = values(described, "bdbackup_job_last_success_phase_seconds", *JOB_LABELS, "phase")
    assert phases == {("pg", "pgbackrest", "write"): 2.5, ("pg", "pgbackrest", "verify"): 3.0}
    assert values(stale, "bdbackup_job_last_success_phase_seconds", *JOB_LABELS, "phase") == {}


class Describing:
    """A job type's collector that decides which facts describe the newest success."""

    def __init__(self, answer):
        self.answer = answer

    def families(self):
        return []

    def probes(self, job, ctx):
        return []

    def collect(self, job, ctx, results):
        return []

    def describes(self, facts, record):
        return self.answer


@pytest.mark.parametrize("answer", [True, False])
def test_the_job_types_collector_decides_which_facts_describe_the_newest_success(env, answer):
    list_engines()  # load the built-in engines first, or they would replace the fake
    register_engine(
        EngineInfo(name="pgbackrest", backend=dict, family="postgres", metrics=Describing(answer))
    )
    add_pg(env)
    # The facts name L1 but the newest success is L2: only the collector's say counts.
    success(env, "pg", "L2", 222)
    env.history.record_last_run("pg", "pgbackrest", 0, "exported", FACTS)

    collected = collect(env)

    assert ("pg", "pgbackrest") in values(
        collected, "bdbackup_job_last_success_timestamp_seconds", *JOB_LABELS
    )
    phases = values(collected, "bdbackup_job_last_success_phase_seconds", *JOB_LABELS, "phase")
    assert bool(phases) is answer


# ---- history units ---------------------------------------------------------------------------


def units(collected):
    return values(collected, "bdbackup_history_units", "job_name", "state")


def history_fixture(env):
    """Records in every state: moved and still there, kept in place, expired and deleted."""
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    env.file_unit("c.tar.gz", days_ago=70)
    assert env.run_cli().exit_code == 0


def test_history_units_count_available_unavailable_and_deleted_records(env):
    history_fixture(env)
    (env.root / "BACKUP/files/b.tar.gz").unlink()  # missing, and GFS did not delete it
    other = env.root / "elsewhere.tar"
    other.write_bytes(b"other")
    env.history.run("other", "file", lambda: BackupResult(other, 5, True), {"kind": "file"})

    collected = collect(env)

    assert units(collected) == {
        ("files", "available"): 1,  # a, moved to the second stage
        ("files", "unavailable"): 1,  # b
        ("files", "deleted"): 1,  # c
        ("other", "available"): 1,  # a record GFS does not manage, and its own file
        ("other", "unavailable"): 0,
        ("other", "deleted"): 0,
    }


@pytest.mark.parametrize("tamper", ["nothing", "remove", "modify"])
def test_history_units_agree_with_gfs_store_available(env, tamper):
    history_fixture(env)
    moved = env.root / "NFS/daily/files/a.tar.gz"
    kept = env.root / "BACKUP/files/b.tar.gz"
    if tamper == "remove":
        moved.unlink()
    elif tamper == "modify":
        kept.write_bytes(b"replaced with something else")
    unmanaged = env.root / "elsewhere.tar"
    unmanaged.write_bytes(b"other")
    env.history.run("other", "file", lambda: BackupResult(unmanaged, 5, True), {"kind": "file"})
    if tamper != "nothing":
        unmanaged.write_bytes(b"other, changed")

    collected = collect(env)

    known = gfs_store.places(env.history)
    expected: dict[tuple[str, str], int] = {}
    for record in env.history.records(successful=True):
        in_store = gfs_store.available(record, known)
        deleted = record.id in known and known[record.id] == ()
        state = "deleted" if deleted else "available" if in_store else "unavailable"
        key = (record.job_name, state)
        expected[key] = expected.get(key, 0) + 1
    got = {key: count for key, count in units(collected).items() if count}
    assert got == expected
    assert sum(got.values()) == len(env.history.records(successful=True))


def test_identity_from_stat_is_the_identity_of_an_artifact(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"abc")
    directory = tmp_path / "directory"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(file)

    for path in (file, directory, link):
        assert stat_identity(os.stat(path)) == artifact_identity(path)
    st = os.stat(file)
    assert stat_identity(st) == json.dumps([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns])
    st = os.stat(directory)
    assert stat_identity(st) == json.dumps([st.st_dev, st.st_ino])


def test_history_units_are_absent_when_the_destination_probe_did_not_finish(env):
    history_fixture(env)
    before = units(collect(env))
    assert before  # present when the probe answers

    collected = collect(env, probe=lambda requests, timeout: Results({}, {DESTINATION: "timeout"}))

    assert units(collected) == {}
    assert MetricError("destination", "", "timeout") in collected.errors
    assert values(collected, "bdbackup_metrics_error", "source", "job_name", "reason") == {
        ("destination", "", "timeout"): 1
    }


def test_history_units_ask_the_probe_for_the_places_of_each_record(env):
    history_fixture(env)
    asked = []

    def probe(requests, timeout):
        asked.extend(requests)
        return in_process_probe(requests, timeout)

    collect(env, probe=probe)

    paths = {request.path for request in asked if request.path.name != MARKER}
    assert env.abs("NFS/daily/files/a.tar.gz") in paths
    assert env.abs("BACKUP/files/b.tar.gz") in paths
    assert all(isinstance(request, StatRequest) for request in asked)
    assert len(asked) == len(set(asked))  # each path once


def test_an_unreadable_history_is_one_error_and_no_history_series(env):
    history_fixture(env)
    with closing(sqlite3.connect(env.database)) as db:
        db.execute("PRAGMA user_version = 99")

    collected = collect(env)

    assert collected.errors == [MetricError("history", "", "unreadable")]
    assert units(collected) == {}
    assert values(collected, "bdbackup_gfs_stage_units", "job_name", "stage") == {}


# ---- GFS ---------------------------------------------------------------------------------------


def stage_figures(collected, count=3):
    units_ = values(collected, "bdbackup_gfs_stage_units", "job_name", "stage")
    bytes_ = values(collected, "bdbackup_gfs_stage_bytes", "job_name", "stage")
    return [
        (units_.get(("gfs-main", str(i))), bytes_.get(("gfs-main", str(i))))
        for i in range(1, count + 1)
    ]


def test_gfs_last_run_units_come_from_the_newest_finished_run(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    row = env.query(
        "SELECT moved, deleted, held, deferred, refused, failed, unmanaged FROM gfs_runs"
    )[0]

    collected = collect(env)

    actions = ("moved", "deleted", "held", "deferred", "refused", "failed", "unmanaged")
    assert values(collected, "bdbackup_gfs_last_run_units", "job_name", "action") == {
        ("gfs-main", action): count for action, count in zip(actions, row, strict=True)
    }


def test_gfs_last_run_units_are_absent_when_the_newest_finished_run_raised(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    assert values(collect(env), "bdbackup_gfs_last_run_units", "job_name", "action")  # present
    config = Config(env.config_path)
    with patch("bdbackup.gfs.runner.decide", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError):
            runner.run_job(config, config.get("gfs-main"), out=lambda _line: None)
    sql(env, "INSERT INTO gfs_runs (gfs_job, started_at, status)"
             " VALUES ('gfs-main', '2026-11-18T09:00:00+00:00', 'running')")

    collected = collect(env)

    assert values(collected, "bdbackup_gfs_last_run_units", "job_name", "action") == {}


def test_gfs_stage_info_maps_each_index_to_its_settings(env):
    collected = collect(env)

    labels = ("job_name", "stage", "period", "keep", "path")
    info = values(collected, "bdbackup_gfs_stage_info", *labels)
    assert info == {
        ("gfs-main", "1", "daily", "5d", str(env.abs("BACKUP"))): 1,
        ("gfs-main", "2", "daily", "20d", str(env.abs("NFS/daily"))): 1,
        ("gfs-main", "3", "weekly", "8w", str(env.abs("NFS/weekly"))): 1,
    }


STAGE_LINE = re.compile(r"Stage (.+): (\d+) units? \((\d+) bytes\)")


@pytest.mark.parametrize("runs", [1, 2])
def test_gfs_stage_figures_equal_the_report_of_the_run(env, runs):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    env.file_unit("c.tar.gz", days_ago=30)
    env.xtrabackup_unit(7)
    env.xtrabackup_unit(0)

    for _ in range(runs):
        code, text = env.run_api(datetime.now())
        reported = [(int(n), int(b)) for _, n, b in STAGE_LINE.findall(text)]
        collected = collect(env)

        assert len(reported) == 3 and any(count for count, _ in reported), text
        assert stage_figures(collected) == reported


def test_gfs_stage_figures_never_stat_the_share(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    baseline = stage_figures(collect(env))
    assert baseline[1][0] == 1
    roots = (str(env.abs("BACKUP")), str(env.abs("NFS")))
    real_stat = os.stat

    def refuse(path, *args, **kwargs):
        if isinstance(path, str | os.PathLike) and os.fspath(path).startswith(roots):
            raise OSError(5, "a hung share")
        return real_stat(path, *args, **kwargs)

    with (
        patch("os.stat", refuse),
        patch.object(Path, "stat", lambda self, **kw: refuse(self)),
    ):
        collected = collect(env, probe=lambda requests, timeout: Results({}, {DESTINATION: "ok"}))

    assert stage_figures(collected) == baseline


def test_collecting_makes_no_filesystem_call_on_a_share_even_with_a_pgbackrest_job(env):
    add_pg(env)  # a fed first stage: the collector must know it without resolving out_dir
    env.history.record_last_run("pg", "pgbackrest", 0, "exported")
    config = Config(env.config_path)  # loading resolves paths; collecting must not
    history = History(config.history)
    roots = (str(env.abs("BACKUP")), str(env.abs("NFS")))
    calls = []

    def watching(name):
        real = getattr(os, name)

        def wrapper(path, *args, **kwargs):
            if isinstance(path, str | bytes | os.PathLike) and os.fsdecode(path).startswith(roots):
                calls.append((name, os.fsdecode(path)))
            return real(path, *args, **kwargs)

        return wrapper

    names = ("stat", "lstat", "readlink", "scandir")
    with patch.multiple(os, **{name: watching(name) for name in names}):
        collected = collect_all(
            config.jobs, history, now=NOW, timeout_seconds=5,
            probe=lambda requests, timeout: Results({}, {DESTINATION: "ok"}),
        )

    assert calls == []
    assert ("gfs-main", "1", str(env.abs("BACKUP"))) in destination(collected)  # it knew it was fed


def test_a_record_under_the_first_stage_that_no_unit_row_names_is_a_first_stage_unit(env):
    env.file_unit("a.tar.gz", days_ago=0)  # recorded, and GFS has not run yet
    assert env.query("SELECT count(*) FROM gfs_units") == [(0,)]

    collected = collect(env)

    size = (env.root / "BACKUP/files/a.tar.gz").stat().st_size
    assert stage_figures(collected) == [(1, size), (0, 0), (0, 0)]


def test_a_unit_whose_locations_are_in_no_configured_stage_is_skipped(env):
    env.file_unit("a.tar.gz", days_ago=7)
    env.file_unit("b.tar.gz", days_ago=0)
    assert env.run_cli().exit_code == 0
    assert stage_figures(collect(env))[1][0] == 1
    sql(env, "UPDATE gfs_locations SET stage_path = '/not/a/stage'"
             " WHERE path LIKE '%NFS/daily%'")

    collected = collect(env)

    assert stage_figures(collected)[1] == (0, 0)


def destination(collected):
    return values(collected, "bdbackup_destination_ready", "job_name", "stage", "path")


def test_destination_ready_covers_each_later_stage_path(env):
    (env.root / "NFS/weekly/.bdbackup-destination").unlink()
    asked = []

    def probe(requests, timeout):
        asked.extend(requests)
        return in_process_probe(requests, timeout)

    collected = collect(env, probe=probe)

    assert destination(collected) == {
        ("gfs-main", "2", str(env.abs("NFS/daily"))): 1,
        ("gfs-main", "3", str(env.abs("NFS/weekly"))): 0,
    }
    markers = {env.abs("NFS/daily") / MARKER, env.abs("NFS/weekly") / MARKER}
    assert {r.path for r in asked if r.path.name == MARKER} == markers
    assert all(r.probe == DESTINATION for r in asked)


def test_destination_ready_includes_a_first_stage_a_pgbackrest_job_writes_into(env):
    add_pg(env)

    unmarked = destination(collect(env))
    (env.root / "BACKUP" / MARKER).touch()
    marked = destination(collect(env))

    first = ("gfs-main", "1", str(env.abs("BACKUP")))
    assert unmarked.get(first) == 0
    assert marked.get(first) == 1


def test_a_path_the_probe_did_not_report_reads_0(env):
    collected = collect(env, probe=lambda requests, timeout: Results({}, {DESTINATION: "timeout"}))

    assert destination(collected) == {
        ("gfs-main", "2", str(env.abs("NFS/daily"))): 0,
        ("gfs-main", "3", str(env.abs("NFS/weekly"))): 0,
    }


def test_a_marker_the_system_refused_reads_0(env):
    def probe(requests, timeout):
        answers = {r: Failure(13) for r in requests if r.path.parent.name == "weekly"}
        answers.update(in_process_probe([r for r in requests if r not in answers], timeout).answers)
        return Results(answers, {DESTINATION: "ok"})

    collected = collect(env, probe=probe)

    assert destination(collected) == {
        ("gfs-main", "2", str(env.abs("NFS/daily"))): 1,
        ("gfs-main", "3", str(env.abs("NFS/weekly"))): 0,
    }


# ---- isolation ---------------------------------------------------------------------------------

THINGS = Family("bdbackup_fake_things", "Things of the fake engine.", ("job_name",))


class FakeCollector:
    def __init__(self, *, families=(THINGS,), requests=(), samples=(), fail_in=None):
        self._families, self._requests, self._samples = families, list(requests), list(samples)
        self.fail_in = fail_in
        self.job = self.results = None

    def families(self):
        if self.fail_in == "families":
            raise RuntimeError("boom")
        return self._families

    def probes(self, job, ctx):
        if self.fail_in == "probes":
            raise RuntimeError("boom")
        self.job = job
        return self._requests

    def collect(self, job, ctx, results):
        if self.fail_in == "collect":
            raise RuntimeError("boom")
        self.results = results
        return self._samples


def collect_with_fake(env, fake, *, probe=in_process_probe):
    list_engines()  # load the built-in engines first, or they would replace the fake
    register_engine(EngineInfo(name="fake", backend=dict, metrics=fake))
    jobs = {**Config(env.config_path).jobs, "f1": Job("f1", "fake", {})}
    return collect(env, jobs=jobs, probe=probe)


def test_an_engines_collector_is_probed_collected_and_checked_against_its_families(env):
    marker = env.abs("NFS/daily") / MARKER
    fake = FakeCollector(
        requests=[StatRequest(marker)],
        samples=[Sample("bdbackup_fake_things", 7, {"job_name": "f1"})],
    )

    collected = collect_with_fake(env, fake)

    assert values(collected, "bdbackup_fake_things", "job_name") == {("f1",): 7}
    assert collected.errors == []
    assert fake.job.name == "f1"
    assert fake.results.stat(marker) is not None  # its request went to the probe
    assert destination(collected)  # and the built-in collectors still ran


@pytest.mark.parametrize("fail_in", ["families", "probes", "collect"])
def test_a_collector_that_raises_is_one_error_and_the_others_still_run(env, fail_in):
    fake = FakeCollector(
        samples=[Sample("bdbackup_fake_things", 7, {"job_name": "f1"})], fail_in=fail_in
    )

    collected = collect_with_fake(env, fake)

    assert collected.errors == [MetricError("collector", "f1", "failed")]
    assert values(collected, "bdbackup_metrics_error", "source", "job_name", "reason") == {
        ("collector", "f1", "failed"): 1
    }
    assert values(collected, "bdbackup_fake_things", "job_name") == {}
    assert len(destination(collected)) == 2  # the gfs collector did not notice
    assert len(values(collected, "bdbackup_gfs_stage_info", "job_name", "stage")) == 3


@pytest.mark.parametrize(
    "sample",
    [
        Sample("bdbackup_fake_things", 1, {"job_name": "f1", "extra": "x"}),
        Sample("bdbackup_fake_things", True, {"job_name": "f1"}),
        Sample("bdbackup_fake_undeclared", 1, {}),
        Sample("bdbackup_job_last_run_exit_code", 1, {"job_name": "f1", "job_type": "fake"}),
    ],
    ids=["wrong-labels", "not-a-number", "undeclared", "another-collectors-family"],
)
def test_samples_that_do_not_fit_the_registry_are_one_error_and_are_dropped(env, sample):
    good = Sample("bdbackup_fake_things", 7, {"job_name": "f1"})

    collected = collect_with_fake(env, FakeCollector(samples=[good, sample]))

    assert collected.errors == [MetricError("collector", "f1", "failed")]
    assert values(collected, "bdbackup_fake_things", "job_name") == {}  # none of its samples
    assert len(values(collected, "bdbackup_gfs_stage_info", "job_name", "stage")) == 3


def test_a_family_outside_the_engines_prefix_is_refused(env):
    stray = Family("bdbackup_other_things", "Not the fake engine's.", ("job_name",))
    fake = FakeCollector(
        families=[stray], samples=[Sample("bdbackup_other_things", 1, {"job_name": "f1"})]
    )

    collected = collect_with_fake(env, fake)

    assert collected.errors == [MetricError("collector", "f1", "failed")]
    assert values(collected, "bdbackup_other_things", "job_name") == {}


def test_the_destination_probe_failing_is_an_error_without_a_job_name(env):
    collected = collect(env, probe=lambda requests, timeout: Results({}, {DESTINATION: "failed"}))

    assert collected.errors == [MetricError("destination", "", "failed")]


def test_collecting_reads_the_history_without_changing_it(env):
    history_fixture(env)
    add_pg(env)
    env.history.record_last_run("pg", "pgbackrest", 0, "exported", FACTS)
    before = env.database.read_bytes()

    collected = collect(env)

    assert units(collected) and stage_figures(collected)[1][0]  # it did read something
    assert env.database.read_bytes() == before


def test_a_job_that_is_not_in_the_config_has_no_job_series(env):
    env.history.record_last_run("gone", "pgbackrest", 0, "exported")
    env.history.record_last_run("gfs-main", "gfs", 0, "completed")

    collected = collect(env)

    assert values(collected, "bdbackup_job_last_run_exit_code", *JOB_LABELS) == {
        ("gfs-main", "gfs"): 0
    }
