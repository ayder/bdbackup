"""A uid with no passwd entry (a container, an LDAP outage) is named ``uid <n>`` in the reports.

``pwd.getpwuid`` raises ``KeyError`` for it. ``run --validate`` and ``run --initialize-structure``
name the OS user, and must not turn that into a traceback.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.gfs.stages import MARKER

UID = f"uid {os.geteuid()}"
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")

CONFIG = (
    '[history]\ndatabase = "state/h.sqlite3"\n'
    '[gfs-main]\ntype = "gfs"\n'
    '[[gfs-main.stage]]\npaths = ["BACKUP"]\nperiod = "daily"\nkeep = "5d"\n'
    '[[gfs-main.stage]]\npaths = ["NFS/daily"]\nperiod = "daily"\nkeep = "20d"\n'
)


def no_passwd_entry(uid: int):
    raise KeyError(f"getpwuid(): uid not found: {uid}")


def site(root: Path) -> Path:
    (root / "BACKUP").mkdir()
    (root / "NFS" / "daily").mkdir(parents=True)
    (root / "NFS" / "daily" / MARKER).write_text("")
    config = root / "config.toml"
    config.write_text(CONFIG)
    return config


def invoke(monkeypatch, *args):
    with monkeypatch.context() as patched:
        patched.setattr(pwd, "getpwuid", no_passwd_entry)
        return CliRunner().invoke(main, ["run", *map(str, args)])


def assert_no_crash(result, exit_code):
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert "Traceback" not in result.output
    assert result.exit_code == exit_code, result.output


def test_validate_names_the_uid_in_its_first_line(tmp_path, monkeypatch):
    config = site(tmp_path.resolve())

    result = invoke(monkeypatch, "-c", config, "--validate")

    lines = result.output.splitlines()
    assert lines[:1] == [f"Validation as OS user {UID}; no backup, prune or GRANT is executed."], (
        result.output
    )
    assert lines[-1] == "Validation passed.", result.output
    assert_no_crash(result, 0)


@NOT_ROOT
def test_validate_names_the_uid_in_a_fail_line(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    config = site(root)
    (root / "NFS" / "daily").chmod(0o500)
    try:
        result = invoke(monkeypatch, "-c", config, "--validate")
    finally:
        (root / "NFS" / "daily").chmod(0o700)

    lines = result.output.splitlines()
    assert f"[FAIL] Stage path {root / 'NFS' / 'daily'}: not writable by OS user {UID}" in lines, (
        result.output
    )
    assert lines[-1] == "Validation failed; see requirements above."
    assert_no_crash(result, 1)


@NOT_ROOT
def test_initialize_structure_names_the_uid_in_a_fail_line(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    config = site(root)
    (root / "NFS" / "daily").chmod(0o500)
    try:
        result = invoke(monkeypatch, "-c", config, "-j", "gfs-main", "--initialize-structure")
    finally:
        (root / "NFS" / "daily").chmod(0o700)

    lines = result.output.splitlines()
    assert f"FAIL {root / 'NFS' / 'daily'}: not writable by OS user {UID}" in lines, result.output
    assert_no_crash(result, 1)
