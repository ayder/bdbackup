"""bdbackup: file and database backups with verification, retention, and guided recovery."""

from importlib.metadata import version

from bdbackup.filebackup import FileBackup
from bdbackup.mysql import MySQLBackup, XtraBackup

__version__ = version("bdbackup")

__all__ = [
    "FileBackup",
    "MySQLBackup",
    "XtraBackup",
    "__version__",
]
