"""The collectors' ``probes()`` phase: each in a daemon thread, joined against one deadline (D8).

A collector still in ``probes()`` at the deadline is abandoned, its ``collect()`` is skipped and
the run goes on without it; the run then ends with ``os._exit`` (proven in test_metrics_hang.py).
"""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import patch

import pytest

from bdbackup.config import Config, Job
from bdbackup.engines import EngineInfo, _reset_for_tests, list_engines, register_engine
from bdbackup.history import History
from bdbackup.metrics.collector import MetricError
from bdbackup.metrics.gfs import GfsCollector
from bdbackup.metrics.registry import Registry
from bdbackup.metrics.render import Sample
from bdbackup.metrics.run import collect_all
from tests.test_gfs_run import Env
from tests.test_metrics_collect import NOW, THINGS, FakeCollector, add_pg, values

STUCK = 6  # seconds a blocked collector waits, if nobody releases it: bounded, never forever


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def release():
    """The event a blocked collector waits on; set when the test ends, so no thread lingers."""
    event = threading.Event()
    yield event
    event.set()


class Blocking(FakeCollector):
    def __init__(self, release):
        super().__init__(samples=[Sample("bdbackup_fake_things", 7, {"job_name": "f1"})])
        self.release = release
        self.collected = False

    def probes(self, job, ctx):
        self.release.wait(STUCK)
        return []

    def collect(self, job, ctx, results):
        self.collected = True
        return self._samples


def jobs_with_fakes(env, collector, *names):
    """The configured jobs and one ``fake`` job per name, collected by ``collector``."""
    list_engines()  # load the built-in engines first, or they would replace the fake
    register_engine(EngineInfo(name="fake", backend=dict, metrics=collector))
    config = Config(env.config_path)
    return config, {**config.jobs, **{name: Job(name, "fake", {}) for name in names}}


def run(config, jobs, timeout=1, **kwargs):
    return collect_all(jobs, History(config.history), now=NOW, timeout_seconds=timeout, **kwargs)


def figures(collected):
    return {
        "stage_info": len(values(collected, "bdbackup_gfs_stage_info", "job_name", "stage")),
        "destination": len(
            values(collected, "bdbackup_destination_ready", "job_name", "stage", "path")
        ),
    }


def test_an_engine_collector_whose_probes_blocks_is_abandoned_with_its_timeout_error(env, release):
    fake = Blocking(release)
    config, jobs = jobs_with_fakes(env, fake, "f1")

    started = time.monotonic()
    collected = run(config, jobs)
    elapsed = time.monotonic() - started

    assert elapsed < 4
    assert collected.abandoned is True
    assert collected.errors == [MetricError("fake", "f1", "timeout")]
    assert values(collected, "bdbackup_metrics_error", "source", "job_name", "reason") == {
        ("fake", "f1", "timeout"): 1
    }
    assert fake.collected is False  # its collect() is skipped
    assert values(collected, "bdbackup_fake_things", "job_name") == {}
    assert figures(collected) == {"stage_info": 3, "destination": 2}  # the others, and the probes


def test_a_built_in_collector_whose_probes_blocks_is_a_collector_timeout(env, release):
    env.file_unit("a.tar.gz", days_ago=0)
    config = Config(env.config_path)

    def blocked(self, job, ctx):
        release.wait(STUCK)
        return []

    started = time.monotonic()
    with patch.object(GfsCollector, "probes", blocked):
        collected = run(config, config.jobs)
    elapsed = time.monotonic() - started

    assert elapsed < 4
    assert collected.abandoned is True
    assert collected.errors == [MetricError("collector", "gfs-main", "timeout")]
    assert figures(collected) == {"stage_info": 0, "destination": 0}  # the gfs collector's
    assert values(collected, "bdbackup_history_units", "job_name", "state")  # the others' remain


def test_a_pgbackrest_info_that_blocks_after_the_kill_is_an_abandoned_collector(env, release):
    add_pg(env)
    env.history.record_last_run("pg", "pgbackrest", 0, "exported")
    config = Config(env.config_path)

    def stuck(cmd, **kwargs):  # what subprocess.run does when the killed child cannot be waited for
        release.wait(STUCK)
        raise subprocess.TimeoutExpired(cmd, 1)

    started = time.monotonic()
    with patch("bdbackup.postgres.pgbackrest.subprocess.run", stuck):
        collected = run(config, config.jobs)
    elapsed = time.monotonic() - started

    assert elapsed < 4
    assert collected.abandoned is True
    assert collected.errors == [MetricError("pgbackrest", "pg", "timeout")]
    assert figures(collected)["stage_info"] == 3
    assert values(collected, "bdbackup_job_last_run_exit_code", "job_name", "job_type")


def test_blocked_collectors_share_one_deadline(env, release):
    config, jobs = jobs_with_fakes(env, Blocking(release), "f1", "f2", "f3")

    started = time.monotonic()
    collected = run(config, jobs)
    elapsed = time.monotonic() - started

    assert elapsed < 2.6  # one wait for the three, not three waits
    assert sorted(collected.errors) == [
        MetricError("fake", name, "timeout") for name in ("f1", "f2", "f3")
    ]


def test_a_run_with_no_blocked_collector_is_not_abandoned_and_returns_its_registry(env):
    fake = FakeCollector(samples=[Sample("bdbackup_fake_things", 7, {"job_name": "f1"})])
    config, jobs = jobs_with_fakes(env, fake, "f1")

    collected = run(config, jobs)

    assert collected.abandoned is False
    assert isinstance(collected.registry, Registry)
    assert collected.registry.get("bdbackup_fake_things") == THINGS  # the engine's, declared
    assert collected.registry.get("bdbackup_job_last_run_exit_code") is not None  # and the built-in
    assert values(collected, "bdbackup_fake_things", "job_name") == {("f1",): 7}
