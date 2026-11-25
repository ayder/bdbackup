"""Drain a job's previously recorded SSH deliveries."""

import logging
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from bdbackup.gfs import store
from bdbackup.history import BackupRecord, History, HistoryError
from bdbackup.ssh_copy import CopyCommandError, CopyResult, CopySettings, copy_artifact
from bdbackup.utils import process_lock

logger = logging.getLogger(__name__)


def run_job(
    history: History, job_name: str, settings: CopySettings, out: Callable[[str], None],
) -> int:
    """Return 0 for success, 1 for a failed attempt, or 3 for a busy copy lock."""
    # A new backup will migrate and enqueue before it tries the flock.
    if not history.has_delivery_schema():
        return 0
    attempted: set[int] = set()
    failed = False
    with ExitStack() as lock:
        try:
            lock.enter_context(process_lock(f"{history.settings.database}.copy-{job_name}.lock"))
        except BlockingIOError:
            out(f"Copy {job_name}: deferred: another run of {job_name} is copying")
            return 3

        while True:
            record_id = history.next_uncopied(job_name, settings.destination, attempted)
            if record_id is None:
                # A backup enqueues under the same SQLite write lock. It must either
                # appear in this scan or commit after the flock has been released.
                with history._connect(create=True) as db:
                    record_id = history.next_uncopied(
                        job_name, settings.destination, attempted, db=db,
                    )
                    if record_id is None:
                        lock.close()
                        break

            attempted.add(record_id)
            if not _attempt(history, record_id, settings, out):
                failed = True

    return 1 if failed else 0


def _source(history: History, record: BackupRecord) -> Path | None:
    places = store.places(history)
    if record.id in places:
        return next(
            (place.member for place in places[record.id]
             if place.identity is not None and store.place_available(place)),
            None,
        )
    return Path(record.path) if record.available else None


def _copy_record(
    history: History, record: BackupRecord, settings: CopySettings, name: str,
) -> CopyResult:
    source = _source(history, record)
    if source is None:
        raise CopyCommandError("source_unavailable", None)
    try:
        return copy_artifact(settings, source, name, record.checksum)
    except CopyCommandError as exc:
        if exc.failure_class == "source_unavailable":
            # A source that vanished after selection can have moved to another stage.
            raise CopyCommandError(
                "transfer", "local artifact disappeared after selection",
            ) from None
        raise


def _attempt(
    history: History, record_id: int, settings: CopySettings, out: Callable[[str], None],
) -> bool:
    record = history.get(record_id)
    if record.path is None or record.checksum is None:
        raise HistoryError(f"Backup ID {record_id} has no artifact path or checksum")
    name = Path(record.path).name
    attempt = history.start_delivery_attempt(record_id, settings.method, datetime.now(UTC))
    line = f"Copy {record.job_name}: {name}"
    try:
        result = _copy_record(history, record, settings, name)
    except (CopyCommandError, OSError) as exc:
        if isinstance(exc, CopyCommandError):
            failure_class, diagnostic = exc.failure_class, exc.diagnostic
        else:
            failure_class, diagnostic = "other", str(exc)
        status = "abandoned" if failure_class == "source_unavailable" else "failed"
        history.finish_delivery_attempt(
            record_id, attempt, status, datetime.now(UTC), failure_class=failure_class,
        )
        out(f"{line} failed: {failure_class}")
        if diagnostic:
            logger.error("%s: %s", line, diagnostic)
        return False

    history.finish_delivery_attempt(
        record_id, attempt, "copied", result.ended_at, bytes_sent=result.bytes_sent,
    )
    if result.already_there:
        detail = "already there"
    else:
        seconds = max(0, int((result.ended_at - result.started_at).total_seconds()))
        detail = f"{settings.method}, {result.bytes_sent} bytes, {seconds} s"
    out(f"{line} -> {settings.destination} ({detail})")
    return True
