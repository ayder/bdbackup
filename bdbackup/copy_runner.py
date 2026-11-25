"""Drain a job's previously recorded SSH deliveries."""

from collections.abc import Callable

from bdbackup.history import History
from bdbackup.ssh_copy import CopySettings


def run_job(
    history: History, job_name: str, settings: CopySettings, out: Callable[[str], None],
) -> int:
    return 0
