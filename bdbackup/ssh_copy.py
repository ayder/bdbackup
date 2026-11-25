"""Settings for copying a published backup to an SSH server."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CopySettings:
    destination: str
    method: str
    host: str
    path: str
    user: str | None = None
    port: int | None = None
    ssh_config: Path | None = None
    bwlimit: int | None = None
