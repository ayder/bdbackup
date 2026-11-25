"""Percona Backup for MongoDB engine."""

from pathlib import Path

from bdbackup.backends import BackupError, BackupResult
from bdbackup.engines import EngineInfo


class PBM:
    """Configuration for a PBM backup job."""

    def __init__(
        self,
        storage_path: str | Path,
        out_dir: str | Path,
        backup_type: str = "logical",
        binary: str = "pbm",
        mongodb_uri: str | None = None,
    ):
        self.storage_path = Path(storage_path)
        self.out_dir = Path(out_dir)
        self.backup_type = backup_type
        self.binary = binary
        self.mongodb_uri = mongodb_uri

    def backup(self) -> BackupResult:
        raise BackupError("PBM backup export is not available")


ENGINE = EngineInfo(
    name="pbm",
    backend=PBM,
    description="Export a Percona Backup for MongoDB backup as a tar archive",
    family="mongodb",
)
