"""``bdbackup metrics -c CONFIG``: write the Prometheus textfile; the driver of one metrics run.

The run is read-only and never blocks on a share (D5, D8). In order:

1. A minimal read of the config file, local files only: ``[metrics]`` and ``[history] database``.
2. The lock ``<history database>.metrics.lock``.
3. The full configuration, read in lexical mode in a daemon thread under ``timeout_seconds``.
4. The collectors: each ``probes()`` in a daemon thread under one deadline, then the probe
   children under another (``metrics.run.collect_all``); ``collect()``, rendering and writing
   touch no share.
5. The atomic write. A run that abandoned a thread ends with ``os._exit`` once the file is
   renamed and stderr flushed, so a thread stuck in the kernel cannot hold the process.

Exit codes: 0 written with no error series; 1 written with an error series, or the file could
not be written; 2 usage or invalid config; 3 another metrics run holds the lock.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import click

from bdbackup.config import Config
from bdbackup.history import History
from bdbackup.metrics import families as f
from bdbackup.metrics.collector import MetricError
from bdbackup.metrics.render import Sample, render
from bdbackup.metrics.run import collect_all
from bdbackup.metrics.settings import DEFAULT_TIMEOUT_SECONDS, parse_metrics
from bdbackup.metrics.write import write
from bdbackup.utils import process_lock, real_path

logger = logging.getLogger("bdbackup.metrics")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


@dataclass(frozen=True)
class _Minimal:
    """What the local read of the config file found."""

    path: Path  # the metrics file
    timeout: int
    database: Path | None
    problem: str | None  # why the config is invalid, when the minimal read can tell


def _read_minimal(config_path: Path) -> _Minimal:
    """Read only the config file: ``[metrics]`` and ``[history] database``, both local.

    Raises ``click.UsageError`` when the file does not parse or has no usable ``[metrics]``
    ``path`` (nothing can be written then). Any other problem comes back as ``problem``.
    """
    config_path = Path(config_path).expanduser().resolve()
    base = config_path.parent
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise click.UsageError(f"Cannot read config {config_path}: {exc}") from exc
    section = data.get("metrics")
    if not isinstance(section, dict) or "path" not in section:
        raise click.UsageError("Configure [metrics] path in the TOML file first")
    value = section["path"]
    if not isinstance(value, str) or not value.strip() or not value.endswith(".prom"):
        raise click.UsageError("[metrics] path must be a nonempty filesystem path ending in .prom")
    path = (base / Path(value).expanduser()).resolve()
    problem, timeout = None, DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = parse_metrics(section, base).timeout_seconds
    except ValueError as exc:
        problem = str(exc)
    history = data.get("history")
    database = None
    if history is None:
        problem = problem or "[metrics] requires a [history] section"
    elif not isinstance(history, dict) or set(history) - {"database", "restore_root"}:
        problem = problem or "[history] accepts only database and restore_root"
    else:
        value = history.get("database")
        if not isinstance(value, str) or not value.strip() or value == ":memory:":
            problem = problem or "[history] database must be a nonempty filesystem path"
        else:
            database = real_path(base / Path(value).expanduser())
    return _Minimal(path, timeout, database, problem)


def _read_config(path: Path) -> Config:
    """The full configuration, read in lexical mode; the one place a test makes it block."""
    return Config(path, lexical=True)


@dataclass
class _Outcome:
    finished: bool = False
    value: object = None
    error: Exception | None = None


def _within(seconds: float, call) -> _Outcome:
    """Run ``call`` in a daemon thread; wait ``seconds`` for it. ``finished`` is false on a hang."""
    outcome = _Outcome()
    done = threading.Event()

    def work() -> None:
        try:
            outcome.value = call()
        except Exception as exc:
            outcome.error = exc
        finally:
            done.set()

    threading.Thread(target=work, daemon=True).start()
    outcome.finished = done.wait(seconds)
    return outcome


def _self_samples(errors: list[MetricError], *, with_errors: bool = True) -> list[Sample]:
    """The series every file has: the end of this run, whether it was clean and each error.

    ``collect_all`` has written the error series of its own run already (``with_errors=False``).
    """
    samples = [
        Sample(f.LAST_RUN, int(time.time())),
        Sample(f.SUCCESS, 0 if errors else 1),
    ]
    for error in errors if with_errors else ():
        samples.append(Sample(f.ERROR, 1, {
            "source": error.source, "job_name": error.job_name, "reason": error.reason,
        }))
    return samples


def _write(path: Path, text: str) -> bool:
    """Write the file; a failure is logged and is ``False``."""
    try:
        write(path, text)
    except OSError as exc:
        logger.error("Cannot write %s: %s", path, exc)
        return False
    return True


def _write_self_only(path: Path, error: MetricError) -> bool:
    """A file with only the self series, for a run that could not read its configuration."""
    return _write(path, render(f.builtin_registry(), _self_samples([error])))


def _hard_exit(code: int) -> None:
    """End the process now: a thread stuck in the kernel must not delay the exit."""
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: S110 - nothing is left to report it to
            pass
    os._exit(code)


def _run_locked(config_path: Path, minimal: _Minimal) -> int:
    outcome = _within(minimal.timeout, lambda: _read_config(config_path))
    if not outcome.finished:
        logger.error(
            "Reading the configuration took longer than %s seconds", minimal.timeout
        )
        _write_self_only(minimal.path, MetricError("config", "", "timeout"))
        _hard_exit(EXIT_FAILED)
    if outcome.error is not None:
        logger.error("Invalid configuration: %s", outcome.error)
        _write_self_only(minimal.path, MetricError("config", "", "invalid"))
        return EXIT_USAGE
    config = outcome.value
    collected = collect_all(
        config.jobs, History(config.history), now=datetime.now(UTC),
        timeout_seconds=minimal.timeout,
    )
    try:
        text = render(
            collected.registry,
            [*collected.samples, *_self_samples(collected.errors, with_errors=False)],
        )
    except ValueError as exc:
        logger.error("The metrics could not be rendered: %s", exc)
        written = False
    else:
        written = _write(minimal.path, text)
    code = EXIT_OK if written and not collected.errors else EXIT_FAILED
    if collected.abandoned:
        _hard_exit(code)
    return code


def run_metrics(config_path: Path) -> int:
    """Run ``bdbackup metrics``; return its exit code, or raise ``click.UsageError`` (exit 2)."""
    minimal = _read_minimal(config_path)
    if minimal.problem is not None:
        logger.error("Invalid configuration: %s", minimal.problem)
        _write_self_only(minimal.path, MetricError("config", "", "invalid"))
        return EXIT_USAGE
    lock = Path(f"{minimal.database}.metrics.lock")
    with ExitStack() as stack:
        try:
            stack.enter_context(process_lock(lock))
        except BlockingIOError:
            logger.error("Another metrics run holds %s", lock)
            return EXIT_LOCKED
        return _run_locked(config_path, minimal)
