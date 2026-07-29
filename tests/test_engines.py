"""Tests for the pluggable backup-engine registry (bdbackup.engines)."""

from __future__ import annotations

import textwrap

import pytest

from bdbackup.config import Job, build_backend, supported_types
from bdbackup.engines import (
    EngineError,
    EngineInfo,
    _reset_for_tests,
    get_engine,
    list_engines,
    register_engine,
)
from bdbackup.mysql import MySQLBackup, XtraBackup


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    """Give each test a fresh registry isolated from the user's config dir."""
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


class FakeBackend:
    def __init__(self, out_dir: str = "."):
        self.out_dir = out_dir


def test_builtin_engines_registered():
    engines = list_engines()
    assert set(engines) == {"mysqldump", "xtrabackup"}
    assert engines["mysqldump"].backend is MySQLBackup
    assert engines["xtrabackup"].backend is XtraBackup
    assert engines["xtrabackup"].family == "mysql"


def test_supported_types_includes_engines_and_retention():
    assert supported_types() == {"file", "retention", "mysqldump", "xtrabackup"}


def test_unknown_engine_error_lists_available():
    with pytest.raises(EngineError) as exc:
        get_engine("pg-dump")
    msg = str(exc.value)
    assert "mysqldump" in msg and "xtrabackup" in msg
    assert "engines" in msg  # points at the user engine dir


def test_open_closed_runtime_registration():
    """A new engine requires zero edits to wiring code: register and build."""
    register_engine(
        EngineInfo(name="xtrabackup80", backend=FakeBackend, family="mysql")
    )
    backend = build_backend(
        Job(name="j", type="xtrabackup80", params={"out_dir": "/tmp/anywhere"})
    )
    assert isinstance(backend, FakeBackend)
    assert backend.out_dir == "/tmp/anywhere"


def test_user_engine_loaded_from_config_dir(monkeypatch, tmp_path):
    """A *.py dropped into <config dir>/engines/ self-registers on next lookup."""
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(cfg))
    engines_dir = cfg / "engines"
    engines_dir.mkdir(parents=True)
    (engines_dir / "mysql_shell.py").write_text(
        textwrap.dedent(
            """
            from bdbackup.engines import EngineInfo

            class MySQLShellDump:
                def __init__(self, out_dir="."):
                    self.out_dir = out_dir

            ENGINE = EngineInfo(
                name="mysql-shell", backend=MySQLShellDump, family="mysql",
            )
            """
        )
    )
    _reset_for_tests()
    info = get_engine("mysql-shell")
    backend = build_backend(
        Job(name="j", type="mysql-shell", params={"out_dir": "/somewhere"})
    )
    assert backend.out_dir == "/somewhere"
    assert info.family == "mysql"


def test_config_rejects_unknown_type_with_registered_names(tmp_path):
    from bdbackup.config import Config, ConfigError

    cfg = tmp_path / "c.toml"
    cfg.write_text('[j]\ntype = "pg_backup"\n')
    with pytest.raises(ConfigError) as exc:
        Config(cfg)
    msg = str(exc.value)
    assert "mysqldump" in msg and "xtrabackup" in msg
    assert "retention" in msg


def test_build_backend_file_type_still_direct():
    from bdbackup.filebackup import FileBackup

    backend = build_backend(
        Job(name="f", type="file", params={"backup_dst": "/tmp/d"})
    )
    assert isinstance(backend, FileBackup)
