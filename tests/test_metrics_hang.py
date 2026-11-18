"""``bdbackup metrics`` in a real process, with a read or a collector that never returns (D8).

Each test starts ``python -c`` with a preamble that makes one function block, then calls
``bdbackup.cli.main``: no hook lives in the production code. The ``atexit`` marker proves how
the process ended: ``os._exit`` skips it, a normal exit runs it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import bdbackup

ROOT = Path(bdbackup.__file__).parent.parent
CONFIG = """[history]
database = "state/history.sqlite3"

[metrics]
path = "metrics/bdbackup.prom"
timeout_seconds = 1

[gfs-main]
type = "gfs"
[[gfs-main.stage]]
paths = ["STAGE1"]
period = "daily"
keep = "5d"
[[gfs-main.stage]]
paths = ["STAGE2"]
period = "daily"
keep = "20d"
"""
SELF = {
    "bdbackup_metrics_last_run_timestamp_seconds",
    "bdbackup_metrics_success",
    "bdbackup_metrics_error",
}
MARKER = textwrap.dedent(
    """
    import atexit
    atexit.register(lambda: open({marker!r}, "w").close())
    """
)
BLOCK_THE_CONFIG_READ = textwrap.dedent(
    """
    import threading
    import bdbackup.metrics_command as command
    command._read_config = lambda path: threading.Event().wait()
    """
)
BLOCK_A_COLLECTOR = textwrap.dedent(
    """
    import threading
    from bdbackup.engines import EngineInfo, list_engines, register_engine
    from bdbackup.metrics.registry import Family

    class Stuck:
        def families(self):
            return [Family("bdbackup_fake_things", "Things.", ("job_name",))]

        def probes(self, job, ctx):
            threading.Event().wait()

        def collect(self, job, ctx, results):
            return []

    list_engines()
    register_engine(EngineInfo(name="fake", backend=dict, metrics=Stuck()))
    """
)


def make_config(tmp_path, extra="") -> Path:
    (tmp_path / "metrics").mkdir(exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG + extra)
    return config


def run_metrics(tmp_path, config, *preambles):
    """Run the command in a new process; returns the process, the time it took and the marker."""
    marker = tmp_path / f"marker-{len(list(tmp_path.glob('marker-*')))}"
    code = "\n".join(
        [MARKER.format(marker=str(marker)), *preambles]
        + ["from bdbackup.cli import main", f"main(['metrics', '-c', {str(config)!r}])"]
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(PYTHONPATH=str(ROOT), BDBACKUP_CONFIG_DIR=str(tmp_path / "cfg"))
    started = time.monotonic()
    process = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT,
        timeout=60,
    )
    return process, time.monotonic() - started, marker


def prom(tmp_path) -> str:
    path = tmp_path / "metrics" / "bdbackup.prom"
    assert path.is_file(), "no metrics file was written"
    return path.read_text()


def series(text, family):
    return [line for line in text.splitlines() if re.match(rf"{family}(\{{| )", line)]


def families(text) -> set[str]:
    return {re.match(r"[a-z_0-9]+", line).group() for line in text.splitlines() if line[:1] != "#"}


def test_a_config_read_that_blocks_writes_only_the_self_series_and_ends_with_os_exit(tmp_path):
    config = make_config(tmp_path)

    process, elapsed, marker = run_metrics(tmp_path, config, BLOCK_THE_CONFIG_READ)

    assert process.returncode == 1, process.stderr
    assert elapsed < 10  # the deadline is 1 s; the rest is the interpreter and the margin
    text = prom(tmp_path)
    assert families(text) == SELF
    assert series(text, "bdbackup_metrics_success") == ["bdbackup_metrics_success 0"]
    assert series(text, "bdbackup_metrics_error") == [
        'bdbackup_metrics_error{source="config",job_name="",reason="timeout"} 1'
    ]
    assert not marker.exists()  # os._exit: no atexit handler ran
    assert "configuration" in process.stderr


def test_a_run_right_after_a_hung_one_is_not_locked_out(tmp_path):
    config = make_config(tmp_path)
    first, _, _ = run_metrics(tmp_path, config, BLOCK_THE_CONFIG_READ)
    assert first.returncode == 1, first.stderr

    second, _, marker = run_metrics(tmp_path, config)

    assert second.returncode in (0, 1), second.stderr  # not 3
    assert series(prom(tmp_path), "bdbackup_gfs_stage_info")  # a real run replaced the file
    assert marker.exists()  # a run that abandoned nothing ends normally


def test_a_collector_whose_probes_blocks_ends_the_run_within_two_deadlines(tmp_path):
    config = make_config(tmp_path, extra='[f1]\ntype = "fake"\n')

    process, elapsed, marker = run_metrics(tmp_path, config, BLOCK_A_COLLECTOR)

    assert process.returncode == 1, process.stderr
    assert elapsed < 12  # about two deadlines of 1 s, plus the interpreter and the margin
    text = prom(tmp_path)
    assert series(text, "bdbackup_metrics_error") == [
        'bdbackup_metrics_error{source="fake",job_name="f1",reason="timeout"} 1'
    ]
    assert series(text, "bdbackup_metrics_success") == ["bdbackup_metrics_success 0"]
    assert len(series(text, "bdbackup_gfs_stage_info")) == 2  # the others' figures are there
    assert series(text, "bdbackup_destination_ready")  # and the destination probe's
    assert not marker.exists()
    assert "fake" in process.stderr
