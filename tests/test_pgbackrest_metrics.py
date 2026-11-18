"""The pgBackRest collector: the last export, the repository's backups and its WAL archive."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from bdbackup.backends import BackupResult
from bdbackup.config import Config, Job
from bdbackup.engines import EngineInfo, _reset_for_tests, get_engine, list_engines, register_engine
from bdbackup.gfs.stages import MARKER
from bdbackup.history import History
from bdbackup.metrics import probe
from bdbackup.metrics.collector import ListPrefixRequest, MetricError
from bdbackup.metrics.probe import CHILD_SOURCE, run_probes
from bdbackup.metrics.render import Sample
from bdbackup.metrics.run import collect_all
from bdbackup.postgres import PgBackRest
from tests.pgbackrest_fake import fake_pgbackrest, info_json, missing_stanza_info
from tests.test_metrics_collect import FakeCollector, values
from tests.test_metrics_probe import hang_on_stat
from tests.test_pgbackrest_cli import NEWEST, SOON, make_site

RUN = "bdbackup.postgres.pgbackrest.subprocess.run"
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
JS = ("job_name", "stanza")
WAL_METRICS = (
    "bdbackup_pgbackrest_wal_archive_present",
    "bdbackup_pgbackrest_wal_archive_max_timeline",
    "bdbackup_pgbackrest_wal_archive_max_segment",
    "bdbackup_pgbackrest_wal_archive_max_mtime_seconds",
)
SEGMENT = "0000000100000000000000FF"


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---- info documents and the way a test collects --------------------------------------------


def backup(label, kind, stop, size=None, repo=None, db=1):
    info = {}
    if size is not None:
        info["size"] = size
    if repo is not None:
        info["repository"] = {"size": repo}
    return {
        "label": label,
        "type": kind,
        "timestamp": {"start": stop - 60, "stop": stop},
        "error": False,
        "info": info,
        "database": {"id": db},
    }


def stanza(backups=(), *, db=None, archive=(), code=0, name="main"):
    return [
        {
            "name": name,
            "repo": [{"key": 1, "cipher": "none"}],
            "db": [{"id": 1, "version": "16"}] if db is None else list(db),
            "backup": list(backups),
            "archive": list(archive),
            "status": {"code": code, "message": "ok"},
        }
    ]


def archive_entry(db_id, archive_id, low, high):
    entry = {"database": {"id": db_id, "repo-key": 1}, "id": archive_id}
    if low is not None:
        entry["min"] = low
    if high is not None:
        entry["max"] = high
    return entry


def scripted(document):
    """A stand-in for ``subprocess.run`` that prints ``document`` for ``info``."""

    def run(cmd, **kwargs):
        run.calls.append([str(arg) for arg in cmd])
        run.kwargs.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(document), stderr="")

    run.calls, run.kwargs = [], []
    return run


def failing(error):
    def run(cmd, **kwargs):
        raise error

    return run


def collect(site, runner, *, timeout=7, probe_=None):
    config = Config(site.config)
    kwargs = {} if probe_ is None else {"probe": probe_}
    with patch(RUN, runner):
        return collect_all(
            config.jobs, History(config.history), now=NOW, timeout_seconds=timeout, **kwargs
        )


def errors_of(collected):
    return values(collected, "bdbackup_metrics_error", "source", "job_name", "reason")


def repo_series(collected, name, *extra):
    return values(collected, f"bdbackup_pgbackrest_{name}", *JS, *extra)


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


# ---- registration ---------------------------------------------------------------------------


def test_the_collector_is_registered_with_the_engine_and_declares_its_families():
    collector = get_engine("pgbackrest").metrics

    names = {family.name for family in collector.families()}

    assert names == {
        "bdbackup_pgbackrest_export_info",
        "bdbackup_pgbackrest_export_full_age_seconds",
        "bdbackup_pgbackrest_info_duration_seconds",
        "bdbackup_pgbackrest_stanza_status",
        "bdbackup_pgbackrest_last_backup_timestamp_seconds",
        "bdbackup_pgbackrest_last_backup_bytes",
        "bdbackup_pgbackrest_last_backup_repo_bytes",
        "bdbackup_pgbackrest_backups",
        *WAL_METRICS,
    }


def test_describes_says_whether_the_facts_name_the_records_label():
    describes = getattr(get_engine("pgbackrest").metrics, "describes", None)

    class Record:
        restore_info = json.dumps({"label": "L1"})

    assert describes is not None
    assert describes({"label": "L1"}, Record) is True
    assert describes({"label": "L2"}, Record) is False
    assert describes({}, Record) is False


# ---- the last export ---------------------------------------------------------------------------


def test_export_info_and_the_full_age_come_from_the_last_export(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0

    collected = collect(site, fake_pgbackrest(site.repo))

    assert repo_series(collected, "export_info", "label") == {("pg", "main", NEWEST): 1}
    assert repo_series(collected, "export_full_age_seconds") == {("pg", "main"): 9000}
    assert (SOON - datetime(2026, 11, 9, 1, 1, tzinfo=UTC)).total_seconds() == 9000


def test_the_full_age_is_absent_when_the_facts_describe_another_export(tmp_path):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0
    artifact = tmp_path / "newer.tar"
    artifact.write_bytes(b"x")
    site.history.run(
        "pg", "pgbackrest-full", lambda: BackupResult(artifact, 1, True), {"label": "NEWER"}
    )

    collected = collect(site, fake_pgbackrest(site.repo))

    assert repo_series(collected, "export_info", "label") == {("pg", "main", "NEWER"): 1}
    assert repo_series(collected, "export_full_age_seconds") == {}


def test_without_an_export_there_are_no_export_series(tmp_path):
    site = make_site(tmp_path)

    collected = collect(site, fake_pgbackrest(site.repo))

    assert repo_series(collected, "backups", "type")  # the repository was read
    assert repo_series(collected, "export_info", "label") == {}
    assert repo_series(collected, "export_full_age_seconds") == {}


# ---- the repository ----------------------------------------------------------------------------

OLDER = "20261108-010000F"
DIFF = f"{NEWEST}_20261110-010000D"
INCR = f"{NEWEST}_20261111-010000I"


def test_the_repository_series_per_backup_type(tmp_path):
    fulls = tuple((label, True, False) for label in (OLDER, NEWEST, DIFF, INCR))
    site = make_site(tmp_path, fulls=fulls)
    document = info_json(site.repo, "main")
    repo_size = {b["label"]: b["info"]["repository"]["size"] for b in document[0]["backup"]}

    collected = collect(site, fake_pgbackrest(site.repo))

    assert repo_series(collected, "last_backup_timestamp_seconds", "type") == {
        ("pg", "main", "full"): epoch(2026, 11, 9, 1, 1),
        ("pg", "main", "diff"): epoch(2026, 11, 10, 1, 1),
        ("pg", "main", "incr"): epoch(2026, 11, 11, 1, 1),
    }
    assert repo_series(collected, "last_backup_bytes", "type") == {
        ("pg", "main", "full"): 20000,
        ("pg", "main", "diff"): 30000,
        ("pg", "main", "incr"): 40000,
    }
    assert repo_series(collected, "last_backup_repo_bytes", "type") == {
        ("pg", "main", "full"): repo_size[NEWEST],
        ("pg", "main", "diff"): repo_size[DIFF],
        ("pg", "main", "incr"): repo_size[INCR],
    }
    assert repo_series(collected, "backups", "type") == {
        ("pg", "main", "full"): 2,
        ("pg", "main", "diff"): 1,
        ("pg", "main", "incr"): 1,
    }


def test_a_type_without_a_backup_has_only_a_zero_count(tmp_path):
    site = make_site(tmp_path)

    collected = collect(site, fake_pgbackrest(site.repo))

    assert repo_series(collected, "backups", "type") == {
        ("pg", "main", "full"): 1,
        ("pg", "main", "diff"): 0,
        ("pg", "main", "incr"): 0,
    }
    only_full = {("pg", "main", "full")}
    for name in ("timestamp_seconds", "bytes", "repo_bytes"):
        assert set(repo_series(collected, f"last_backup_{name}", "type")) == only_full


def test_the_newest_of_a_type_is_by_stop_time_then_label(tmp_path):
    site = make_site(tmp_path)
    document = stanza([
        backup("20261101-010000F", "full", 500, size=1, repo=1),
        backup("20261102-010000F", "full", 400, size=2, repo=2),
        backup("20261103-010000F", "full", 500, size=3, repo=3),  # ties the first; the label wins
    ])

    collected = collect(site, scripted(document))

    full = ("pg", "main", "full")
    assert repo_series(collected, "last_backup_timestamp_seconds", "type") == {full: 500}
    assert repo_series(collected, "last_backup_bytes", "type") == {full: 3}
    assert repo_series(collected, "last_backup_repo_bytes", "type") == {full: 3}


def test_a_missing_key_leaves_only_that_series_out(tmp_path):
    site = make_site(tmp_path)
    no_stop = backup("20261101-010000F", "full", 100, size=1, repo=1)
    del no_stop["timestamp"]["stop"]
    document = stanza([
        no_stop,
        backup("20261102-010000D", "diff", 200, repo=5),  # no database size
        backup("20261103-010000I", "incr", 300, size=7),  # no repository size
    ])

    collected = collect(site, scripted(document))

    assert repo_series(collected, "backups", "type") == {
        ("pg", "main", "full"): 1, ("pg", "main", "diff"): 1, ("pg", "main", "incr"): 1,
    }
    assert set(repo_series(collected, "last_backup_timestamp_seconds", "type")) == {
        ("pg", "main", "diff"), ("pg", "main", "incr"),
    }
    assert set(repo_series(collected, "last_backup_bytes", "type")) == {("pg", "main", "incr")}
    assert set(repo_series(collected, "last_backup_repo_bytes", "type")) == {("pg", "main", "diff")}


def test_the_stanza_status_and_the_duration_of_the_call(tmp_path):
    site = make_site(tmp_path)
    ticks = iter([100.0, 102.5])

    with patch("bdbackup.postgres.metrics._clock", lambda: next(ticks)):
        collected = collect(site, fake_pgbackrest(site.repo))

    assert repo_series(collected, "stanza_status") == {("pg", "main"): 0}
    assert repo_series(collected, "info_duration_seconds") == {("pg", "main"): 2.5}
    assert collected.errors == []


def test_a_stanza_that_is_missing_has_its_status_code_and_no_backups(tmp_path):
    site = make_site(tmp_path)

    collected = collect(site, scripted(missing_stanza_info("main")))

    assert repo_series(collected, "stanza_status") == {("pg", "main"): 1}
    assert repo_series(collected, "backups", "type") == {
        ("pg", "main", "full"): 0, ("pg", "main", "diff"): 0, ("pg", "main", "incr"): 0,
    }
    assert repo_series(collected, "last_backup_timestamp_seconds", "type") == {}
    assert collected.errors == []


def test_a_stanza_that_is_not_in_the_output_is_output_invalid(tmp_path):
    site = make_site(tmp_path)

    collected = collect(site, scripted(stanza(name="another")))

    assert collected.errors == [MetricError("pgbackrest", "pg", "output_invalid")]
    assert repo_series(collected, "stanza_status") == {}


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (failing(FileNotFoundError(2, "No such file or directory")), "binary_missing"),
        (failing(PermissionError(13, "Permission denied")), "binary_missing"),
        (failing(subprocess.CalledProcessError(55, ["pgbackrest"], "", "ERROR\n")), "failed"),
        (failing(subprocess.TimeoutExpired(["pgbackrest"], 7)), "timeout"),
        (
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr=""),
            "output_invalid",
        ),
    ],
    ids=["binary-missing", "not-executable", "failed", "timeout", "output-invalid"],
)
def test_a_failed_info_call_is_the_pgbackrest_error_with_its_reason_and_no_repository_series(
    tmp_path, runner, reason
):
    site = make_site(tmp_path)
    assert site.run().exit_code == 0  # an export, so there is something besides the repository

    collected = collect(site, runner)

    assert collected.errors == [MetricError("pgbackrest", "pg", reason)]  # not source="collector"
    assert errors_of(collected) == {("pgbackrest", "pg", reason): 1}
    assert repo_series(collected, "stanza_status") == {}
    assert repo_series(collected, "info_duration_seconds") == {}
    assert repo_series(collected, "backups", "type") == {}
    assert repo_series(collected, "last_backup_timestamp_seconds", "type") == {}
    assert repo_series(collected, "export_info", "label") == {("pg", "main", NEWEST): 1}  # kept
    assert len(values(collected, "bdbackup_gfs_stage_info", "job_name", "stage")) == 1  # others ran


def test_the_info_call_is_the_jobs_own_command_with_the_metrics_timeout(tmp_path):
    site = make_site(tmp_path)
    fake = fake_pgbackrest(site.repo)

    collect(site, fake, timeout=7)

    job = Config(site.config).get("pg")
    assert fake.calls == [PgBackRest(**job.params).command("info", "--output=json")]
    assert fake.kwargs[0]["timeout"] == 7


def test_one_info_call_is_made_for_each_active_pgbackrest_job(tmp_path):
    inactive = (
        '[pg2]\ntype = "pgbackrest"\nstanza = "other"\nrepo_path = "repo"\n'
        'out_dir = "STAGE1/pg2"\nactive = false\n'
    )
    site = make_site(tmp_path, extra=inactive)
    fake = fake_pgbackrest(site.repo)

    collect(site, fake)

    assert len(fake.calls) == 1
    assert "--stanza=main" in fake.calls[0]


# ---- a collector's own source error ------------------------------------------------------------


def register_fake(collector):
    list_engines()  # load the built-in engines first, or they would replace the fake
    register_engine(EngineInfo(name="fake", backend=dict, metrics=collector))


def collect_fake(tmp_path, collector):
    site = make_site(tmp_path)
    register_fake(collector)
    config = Config(site.config)
    jobs = {**config.jobs, "f1": Job("f1", "fake", {})}
    with patch(RUN, fake_pgbackrest(site.repo)):
        return collect_all(jobs, History(config.history), now=NOW, timeout_seconds=7)


def reporting(error):
    class Reporting(FakeCollector):
        def collect(self, job, ctx, results):
            yield Sample("bdbackup_fake_things", 7, {"job_name": "f1"})
            yield error

    return Reporting()


def test_a_collector_may_report_its_own_source_error_and_its_samples_stay(tmp_path):
    collected = collect_fake(tmp_path, reporting(MetricError("fake", "f1", "odd")))

    assert collected.errors == [MetricError("fake", "f1", "odd")]
    assert errors_of(collected) == {("fake", "f1", "odd"): 1}
    assert values(collected, "bdbackup_fake_things", "job_name") == {("f1",): 7}


@pytest.mark.parametrize(
    "error",
    [MetricError("history", "f1", "unreadable"), MetricError("fake", "someone-else", "odd")],
    ids=["another-source", "another-job"],
)
def test_a_collector_may_not_report_another_source_or_job(tmp_path, error):
    # The same collector, reporting its own error, is accepted; only the claim differs.
    (tmp_path / "own").mkdir()
    (tmp_path / "claim").mkdir()
    own = collect_fake(tmp_path / "own", reporting(MetricError("fake", "f1", "odd")))
    assert own.errors == [MetricError("fake", "f1", "odd")]

    collected = collect_fake(tmp_path / "claim", reporting(error))

    assert collected.errors == [MetricError("collector", "f1", "failed")]
    assert values(collected, "bdbackup_fake_things", "job_name") == {}


# ---- WAL archive freshness (D10) ---------------------------------------------------------------

LOW = "000000010000000000000001"


def wal_doc(high, *, low=LOW, archive_id="16-1", db_id=1):
    return stanza(archive=[archive_entry(db_id, archive_id, low, high)])


class Recording:
    """A probe that records the requests it is given and answers them with the real children."""

    def __init__(self):
        self.requests = []

    def __call__(self, requests, timeout):
        self.requests.extend(requests)
        return run_probes(requests, timeout)

    def lists(self):
        return [r for r in self.requests if isinstance(r, ListPrefixRequest)]


def wal(collected, name, *extra):
    return values(collected, f"bdbackup_pgbackrest_wal_archive_{name}", *JS, "db_id", *extra)


def test_only_the_current_database_is_reported_after_a_stanza_upgrade(tmp_path):
    site = make_site(tmp_path)
    document = stanza(
        db=[{"id": 1, "version": "15"}, {"id": 2, "version": "16"}],
        archive=[
            archive_entry(1, "15-1", "000000010000000000000001", "000000010000000000000009"),
            archive_entry(2, "16-2", "000000020000000000000001", "000000020000000000000007"),
        ],
    )
    recording = Recording()

    collected = collect(site, scripted(document), probe_=recording)

    assert wal(collected, "present") == {("pg", "main", "2"): 1}
    assert wal(collected, "max_timeline") == {("pg", "main", "2"): 2}
    assert wal(collected, "max_segment") == {("pg", "main", "2"): 7}
    repo = Config(site.config).get("pg").params["repo_path"]
    assert recording.lists() == [
        ListPrefixRequest(
            Path(repo) / "archive" / "main" / "16-2" / "0000000200000000",
            "000000020000000000000007-",
            "repository",
        )
    ]


@pytest.mark.parametrize(
    ("archive", "present"),
    [
        ([], 0),
        ([archive_entry(1, "16-1", LOW, None)], 0),
        ([archive_entry(1, "16-1", None, SEGMENT)], 0),
        ([archive_entry(9, "16-9", LOW, SEGMENT)], 0),  # another database's archive
    ],
    ids=["no-entry", "no-max", "no-min", "other-database"],
)
def test_wal_archive_present_is_0_without_both_min_and_max_for_the_current_database(
    tmp_path, archive, present
):
    site = make_site(tmp_path)
    recording = Recording()

    collected = collect(site, scripted(stanza(archive=archive)), probe_=recording)

    assert wal(collected, "present") == {("pg", "main", "1"): present}
    assert wal(collected, "max_timeline") == {} and wal(collected, "max_segment") == {}
    assert recording.lists() == []  # nothing to look up
    assert collected.errors == []


@pytest.mark.parametrize(
    ("timeline", "log", "segment"),
    [(1, 0, 0xFF), (1, 1, 0), (2, 0x10, 0x3), (0xA, 0xFFFFFFFF, 0xFFFFFFFF)],
)
@pytest.mark.parametrize("case", [str.upper, str.lower], ids=["upper", "lower"])
def test_the_timeline_and_segment_come_from_the_24_hex_digits(
    tmp_path, timeline, log, segment, case
):
    site = make_site(tmp_path)
    high = case(f"{timeline:08X}{log:08X}{segment:08X}")
    assert len(high) == 24

    collected = collect(site, scripted(wal_doc(high)), probe_=Recording())

    assert wal(collected, "max_timeline") == {("pg", "main", "1"): timeline}
    assert wal(collected, "max_segment") == {("pg", "main", "1"): (log << 32) | segment}


@pytest.mark.parametrize(
    "high",
    ["XYZ", "0000000100000000000000F", "0000000100000000000000FFF", "0000000100000000000000GG"],
)
def test_a_max_that_is_not_24_hex_digits_gives_no_wal_figures_and_no_request(tmp_path, high):
    site = make_site(tmp_path)
    recording = Recording()

    collected = collect(site, scripted(wal_doc(high)), probe_=recording)

    assert wal(collected, "present") == {("pg", "main", "1"): 1}  # it has both min and max
    for name in ("max_timeline", "max_segment", "max_mtime_seconds"):
        assert wal(collected, name) == {}
    assert recording.lists() == []
    assert collected.errors == []


def wal_site(tmp_path, *, segments=(LOW, SEGMENT)):
    """A site whose repository holds archived WAL files; the newest one's mtime is set."""
    site = make_site(tmp_path, wal=segments)
    directory = site.repo / "archive" / "main" / "16-1" / SEGMENT[:16]
    newest = next(directory.glob(f"{SEGMENT}-*"))
    os.utime(newest, (1_700_000_123, 1_700_000_123))
    return site, directory


def test_the_mtime_is_that_of_the_one_matching_wal_file(tmp_path):
    site, _ = wal_site(tmp_path)
    document = info_json(site.repo, "main")
    assert document[0]["archive"][0]["max"] == SEGMENT  # the fake read it from the files

    collected = collect(site, fake_pgbackrest(site.repo))

    assert wal(collected, "max_mtime_seconds") == {("pg", "main", "1"): 1_700_000_123}
    assert wal(collected, "present") == {("pg", "main", "1"): 1}
    assert collected.errors == []


def unavailable(collected):
    return collected.errors == [MetricError("pgbackrest", "pg", "wal_mtime_unavailable")]


def test_no_matching_file_is_wal_mtime_unavailable_and_the_other_figures_stay(tmp_path):
    site, directory = wal_site(tmp_path)
    next(directory.glob(f"{SEGMENT}-*")).unlink()

    collected = collect(site, scripted(wal_doc(SEGMENT)))

    assert unavailable(collected)
    assert errors_of(collected) == {("pgbackrest", "pg", "wal_mtime_unavailable"): 1}
    assert wal(collected, "max_mtime_seconds") == {}
    assert wal(collected, "max_segment") == {("pg", "main", "1"): 255}
    assert wal(collected, "present") == {("pg", "main", "1"): 1}


def test_two_matching_files_are_wal_mtime_unavailable(tmp_path):
    site, directory = wal_site(tmp_path)
    (directory / f"{SEGMENT}-{'1' * 40}.gz").write_bytes(b"")

    collected = collect(site, scripted(wal_doc(SEGMENT)))

    assert unavailable(collected)
    assert wal(collected, "max_mtime_seconds") == {}


def test_a_missing_directory_is_wal_mtime_unavailable(tmp_path):
    site = make_site(tmp_path)

    collected = collect(site, scripted(wal_doc(SEGMENT)))

    assert unavailable(collected)
    assert wal(collected, "max_mtime_seconds") == {}


def hang_on_list(delay: float = 0.0) -> str:
    """A child that hangs when it is asked for a listing and answers stat requests like the real
    one."""
    return (
        "import io, json, sys, time\n"
        "text = sys.stdin.read()\n"
        "if any(r['kind'] == 'list' for r in json.loads(text)):\n"
        "    time.sleep(30)\n"
        "sys.stdin = io.StringIO(text)\n"
        f"exec({CHILD_SOURCE!r})\n"
    )


def use_child(monkeypatch, code):
    monkeypatch.setattr(probe, "_child_argv", lambda: [sys.executable, "-c", code])


def test_a_hung_repository_probe_loses_the_wal_mtime_and_nothing_else(tmp_path, monkeypatch):
    site, _ = wal_site(tmp_path)
    use_child(monkeypatch, hang_on_list())

    collected = collect(site, fake_pgbackrest(site.repo), timeout=1)

    assert unavailable(collected)
    assert wal(collected, "max_mtime_seconds") == {}
    assert wal(collected, "max_segment") == {("pg", "main", "1"): 255}
    ready = values(collected, "bdbackup_destination_ready", "job_name", "stage", "path")
    assert list(ready.values()) == [1]  # the destination probe answered: STAGE1 has its marker
    assert (Path(site.root) / "STAGE1" / MARKER).exists()


def test_a_hung_destination_probe_leaves_the_wal_mtime(tmp_path, monkeypatch):
    site, _ = wal_site(tmp_path)
    use_child(monkeypatch, hang_on_stat(0))

    collected = collect(site, fake_pgbackrest(site.repo), timeout=1)

    assert collected.errors == [MetricError("destination", "", "timeout")]
    assert wal(collected, "max_mtime_seconds") == {("pg", "main", "1"): 1_700_000_123}
    ready = values(collected, "bdbackup_destination_ready", "job_name", "stage", "path")
    assert list(ready.values()) == [0]


def test_the_collector_asks_for_one_directory_and_scans_nothing_in_the_parent(tmp_path):
    site, directory = wal_site(tmp_path)
    document = info_json(site.repo, "main")  # the fake reads the repository; only the code under
    recording = Recording()  # test is watched, so it answers from this document instead
    real_scandir = os.scandir
    scanned = []

    def watching(path=".", *args, **kwargs):
        scanned.append(os.fsdecode(path) if not isinstance(path, int) else path)
        return real_scandir(path, *args, **kwargs)

    with patch("os.scandir", watching):
        collected = collect(site, scripted(document), probe_=recording)

    assert wal(collected, "max_mtime_seconds") == {("pg", "main", "1"): 1_700_000_123}
    assert [(r.directory, r.prefix, r.probe) for r in recording.lists()] == [
        (directory.resolve(), f"{SEGMENT}-", "repository")
    ]
    assert not [path for path in scanned if str(site.repo) in str(path)]
