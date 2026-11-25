"""Percona Backup for MongoDB engine."""

from bdbackup.engines import EngineInfo


class PBM:
    """Configuration for a PBM backup job."""

    def __init__(self, **params):
        self.params = params


ENGINE = EngineInfo(name="pbm", backend=PBM, family="mongodb")
