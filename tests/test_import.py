"""Smoke tests for package imports."""

import tomllib
from importlib.metadata import version
from pathlib import Path

import bdbackup
from bdbackup import FileBackup, MySQLBackup, XtraBackup


def test_version():
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(project.read_text())["project"]["version"]
    assert bdbackup.__version__ == version("bdbackup") == expected


def test_imports():
    assert FileBackup is not None
    assert MySQLBackup is not None
    assert XtraBackup is not None
