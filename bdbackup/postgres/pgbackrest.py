"""The pgBackRest engine (stub for the failing tests; the implementation follows)."""

from __future__ import annotations

import subprocess  # noqa: F401  (the tests patch subprocess.run in this module)
from dataclasses import dataclass

from bdbackup.engines import EngineInfo
from bdbackup.postgres.repository import FullBackup


@dataclass(frozen=True)
class Choice:
    full: FullBackup
    new: bool


class PgBackRest:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError


ENGINE = EngineInfo(name="pgbackrest", backend=PgBackRest)
