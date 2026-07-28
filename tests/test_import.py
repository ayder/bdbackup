"""Smoke tests for package imports."""

import bdbackup
from bdbackup import FileBackup, MySQLBackup, XtraBackup


def test_version():
    assert bdbackup.__version__


def test_imports():
    assert FileBackup is not None
    assert MySQLBackup is not None
    assert XtraBackup is not None
