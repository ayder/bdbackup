"""``bdbackup metrics -c CONFIG``: the textfile, its exit codes and what it never touches."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.engines import _reset_for_tests
from bdbackup.metrics import probe
from bdbackup.utils import process_lock
from tests.pgbackrest_fake import fake_pgbackrest
from tests.test_metrics_probe import hang_on_stat
from tests.test_pgbackrest_cli import make_site

RUN = "bdbackup.postgres.pgbackrest.subprocess.run"
SELF = {
    "bdbackup_metrics_last_run_timestamp_seconds",
    "bdbackup_metrics_success",
    "bdbackup_metrics_error",
}
HISTORY = '[history]\ndatabase = "state/history.sqlite3"\n'
METRICS = '[metrics]\npath = "metrics/bdbackup.prom"\ntimeout_seconds = 5\n'


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


def metrics_site(tmp_path, *, metrics=METRICS):
    site = make_site(tmp_path, extra="\n" + metrics)
    (tmp_path / "metrics").mkdir()
    return site


def metrics_file(tmp_path) -> Path:
    return tmp_path / "metrics" / "bdbackup.prom"


def written(tmp_path) -> str:
    """The text of the metrics file; a missing file is a failed assertion."""
    path = metrics_file(tmp_path)
    assert path.is_file(), "no metrics file was written"
    return path.read_text()


def invoke(config, runner=None):
    runner = runner if runner is not None else fake_pgbackrest(config.parent / "repo")
    with patch(RUN, runner):
        return CliRunner().invoke(main, ["metrics", "-c", str(config)])


def lines(text, family):
    return [
        line for line in text.splitlines() if re.match(rf"{family}(\{{| )", line)
    ]


def families(text) -> set[str]:
    return {re.match(r"[a-z_0-9]+", line).group() for line in text.splitlines() if line[:1] != "#"}


def only_self_series(text, *error_lines):
    assert families(text) == SELF, text
    assert lines(text, "bdbackup_metrics_success") == ["bdbackup_metrics_success 0"]
    assert lines(text, "bdbackup_metrics_error") == list(error_lines)


def config_error_line(reason="invalid"):
    return f'bdbackup_metrics_error{{source="config",job_name="",reason="{reason}"}} 1'


# ---- a run that works ------------------------------------------------------------------------


def test_a_run_writes_the_file_prints_nothing_and_exits_0(tmp_path, caplog):
    site = metrics_site(tmp_path)
    caplog.set_level(logging.INFO)
    before = int(time.time())

    result = invoke(site.config)

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    text = written(tmp_path)
    assert stat.S_IMODE(metrics_file(tmp_path).stat().st_mode) == 0o644
    assert lines(text, "bdbackup_metrics_success") == ["bdbackup_metrics_success 1"]
    [stamp] = lines(text, "bdbackup_metrics_last_run_timestamp_seconds")
    assert before <= int(stamp.rsplit(" ", 1)[1]) <= int(time.time())
    assert lines(text, "bdbackup_metrics_error") == []
    assert lines(text, "bdbackup_gfs_stage_info")
    assert lines(text, "bdbackup_pgbackrest_stanza_status") == [
        'bdbackup_pgbackrest_stanza_status{job_name="pg",stanza="main"} 0'
    ]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert not list((tmp_path / "metrics").glob(".*"))  # no temporary file is left


def test_the_metrics_timeout_reaches_the_info_call(tmp_path):
    site = metrics_site(tmp_path)
    fake = fake_pgbackrest(site.repo)

    assert invoke(site.config, fake).exit_code == 0

    assert [kwargs.get("timeout") for kwargs in fake.kwargs] == [5]


def test_a_failing_pgbackrest_gives_exit_1_success_0_and_the_other_families(tmp_path, caplog):
    site = metrics_site(tmp_path)
    caplog.set_level(logging.INFO)

    def broken(cmd, **kwargs):
        raise subprocess.CalledProcessError(55, cmd, "", "ERROR\n")

    result = invoke(site.config, broken)

    assert result.exit_code == 1, result.output
    text = written(tmp_path)
    assert lines(text, "bdbackup_metrics_success") == ["bdbackup_metrics_success 0"]
    assert lines(text, "bdbackup_metrics_error") == [
        'bdbackup_metrics_error{source="pgbackrest",job_name="pg",reason="failed"} 1'
    ]
    assert lines(text, "bdbackup_gfs_stage_info")
    assert lines(text, "bdbackup_pgbackrest_stanza_status") == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "pgbackrest" in warnings[0].getMessage()


def test_the_run_never_changes_the_history_the_stages_or_the_repository(tmp_path):
    site = metrics_site(tmp_path)
    assert site.run().exit_code == 0  # an export, so the history and the stage hold something

    def snapshot():
        found = {}
        for path in sorted(tmp_path.rglob("*")):
            relative = path.relative_to(tmp_path).as_posix()
            if relative.startswith("metrics") or relative.endswith(".metrics.lock"):
                continue  # what the command writes: its file and its own lock
            found[relative] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
            )
        return found

    before = snapshot()
    assert any(name.startswith("state/history.sqlite3") for name in before)
    assert any(name.startswith("repo/") for name in before)
    assert any(name.startswith("STAGE1/") for name in before)

    assert invoke(site.config).exit_code == 0

    assert snapshot() == before
    assert metrics_file(tmp_path).exists()


def test_a_probe_that_times_out_is_the_destination_error_and_the_lock_is_released(tmp_path):
    site = metrics_site(tmp_path, metrics='[metrics]\npath = "metrics/bdbackup.prom"\n'
                                          "timeout_seconds = 1\n")
    children = []
    real_popen = subprocess.Popen

    def spying(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    started = time.monotonic()
    # A probe child that hangs before it answers a stat: nothing but the child is abandoned, so
    # the command ends normally (a killed child leaves no thread behind).
    with (
        patch.object(probe, "_child_argv", lambda: [sys.executable, "-c", hang_on_stat(0)]),
        patch.object(subprocess, "Popen", spying),
    ):
        first = invoke(site.config)
    elapsed = time.monotonic() - started
    for child in children:
        child.wait(timeout=10)  # killed by the probe, reaped here

    assert first.exit_code == 1, first.output
    assert elapsed < 6
    text = written(tmp_path)
    assert lines(text, "bdbackup_metrics_success") == ["bdbackup_metrics_success 0"]
    assert lines(text, "bdbackup_metrics_error") == [
        'bdbackup_metrics_error{source="destination",job_name="",reason="timeout"} 1'
    ]
    ready = lines(text, "bdbackup_destination_ready")
    assert ready and all(line.endswith(" 0") for line in ready)

    second = invoke(site.config)  # the real child; and the lock was released

    assert second.exit_code == 0, second.output
    text = written(tmp_path)
    assert lines(text, "bdbackup_metrics_success") == ["bdbackup_metrics_success 1"]
    assert all(line.endswith(" 1") for line in lines(text, "bdbackup_destination_ready"))


# ---- before the lock: only local files are read -----------------------------------------------

NO_FILE = [
    pytest.param("[history\n", id="toml-does-not-parse"),
    pytest.param(HISTORY, id="no-metrics-section"),
    pytest.param(HISTORY + "[metrics]\ntimeout_seconds = 5\n", id="no-path"),
    pytest.param(HISTORY + '[metrics]\npath = "metrics/m.txt"\n', id="path-not-prom"),
    pytest.param(HISTORY + '[metrics]\npath = ""\n', id="path-empty"),
]


@pytest.mark.parametrize("text", NO_FILE)
def test_a_config_without_a_usable_metrics_path_is_a_usage_error_and_writes_nothing(
    tmp_path, text
):
    config = tmp_path / "config.toml"
    config.write_text(text)

    result = invoke(config)

    assert result.exit_code == 2, result.output
    assert list(tmp_path.rglob("*.prom")) == []
    assert list(tmp_path.rglob("*.lock")) == []


CONFIG_ERRORS = [
    pytest.param('[metrics]\npath = "metrics/bdbackup.prom"\n', id="no-history"),
    pytest.param(
        '[history]\nrestore_root = "r"\n[metrics]\npath = "metrics/bdbackup.prom"\n',
        id="history-without-database",
    ),
    pytest.param(
        HISTORY + '[metrics]\npath = "metrics/bdbackup.prom"\ntimeout_seconds = 0\n',
        id="bad-timeout",
    ),
    pytest.param(
        HISTORY + METRICS + '[bad]\ntype = "nonsense"\n', id="job-of-an-unknown-type"
    ),
]


@pytest.mark.parametrize("text", CONFIG_ERRORS)
def test_a_config_error_with_a_known_metrics_path_writes_the_config_file_and_exits_2(
    tmp_path, caplog, text
):
    (tmp_path / "metrics").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(text)
    caplog.set_level(logging.INFO)

    result = invoke(config)

    assert result.exit_code == 2, result.output
    only_self_series(written(tmp_path), config_error_line())
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_a_config_error_before_the_lock_takes_no_lock(tmp_path):
    (tmp_path / "metrics").mkdir()
    config = tmp_path / "config.toml"
    config.write_text('[metrics]\npath = "metrics/bdbackup.prom"\n')

    result = invoke(config)

    assert result.exit_code == 2, result.output
    assert metrics_file(tmp_path).is_file()  # the config file was written, and without a lock
    assert list(tmp_path.rglob("*.lock")) == []


# ---- the lock ----------------------------------------------------------------------------------


def test_a_busy_lock_exits_3_and_leaves_the_file_untouched(tmp_path, caplog):
    site = metrics_site(tmp_path)
    metrics_file(tmp_path).write_text("OLD\n")
    caplog.set_level(logging.INFO)
    lock = Path(str((tmp_path / "state" / "history.sqlite3").resolve()) + ".metrics.lock")

    with process_lock(lock):
        result = invoke(site.config)

    assert result.exit_code == 3, result.output
    assert metrics_file(tmp_path).read_text() == "OLD\n"
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        f"Another metrics run holds {lock}"
    ]


def test_the_lock_is_released_when_the_run_ends(tmp_path):
    site = metrics_site(tmp_path)
    assert invoke(site.config).exit_code == 0
    assert metrics_file(tmp_path).is_file()
    metrics_file(tmp_path).unlink()

    assert invoke(site.config).exit_code == 0  # not 3
    assert metrics_file(tmp_path).is_file()


# ---- the file cannot be written ----------------------------------------------------------------


def test_a_missing_directory_is_exit_1_and_an_error_log(tmp_path, caplog):
    site = metrics_site(tmp_path, metrics='[metrics]\npath = "absent/bdbackup.prom"\n')
    caplog.set_level(logging.INFO)

    result = invoke(site.config)

    assert result.exit_code == 1, result.output
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "absent" in errors[0] and "bdbackup.prom" in errors[0]
    assert not (tmp_path / "absent").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_directory_that_is_not_writable_is_exit_1_and_an_error_log(tmp_path, caplog):
    site = metrics_site(tmp_path)
    caplog.set_level(logging.INFO)
    (tmp_path / "metrics").chmod(0o500)
    try:
        result = invoke(site.config)
    finally:
        (tmp_path / "metrics").chmod(0o700)

    assert result.exit_code == 1, result.output
    assert [r for r in caplog.records if r.levelno == logging.ERROR]
    assert not metrics_file(tmp_path).exists()
