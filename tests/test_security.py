"""Security-specific tests for credential handling."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from bdbackup.mysqlbackup import MySQLBackup
from bdbackup.utils import mysql_cnf_file, redact_cmd
from bdbackup.xtrabackup import XtraBackup


def test_redact_cmd_masks_secrets():
    cmd = ["foo", "bar", "secret", "baz"]
    redacted = redact_cmd(cmd, ["secret"])
    assert redacted == ["foo", "bar", "***", "baz"]


def test_mysql_cnf_file_permissions():
    with mysql_cnf_file(user="root", password="hunter2", host="db", port=3307) as path:
        assert path.exists()
        mode = path.stat().st_mode
        assert mode & 0o777 == 0o600
        text = path.read_text(encoding="utf-8")
        assert "user=root" in text
        assert "password=hunter2" in text
        assert "host=db" in text
        assert "port=3307" in text
    assert not path.exists()


def test_mysqlbackup_uses_defaults_extra_file(tmp_path: Path):
    called_cmds: list[list[str]] = []

    class FakeStdout:
        def read(self, _size: int = -1) -> bytes:
            return b""

    def fake_popen(cmd, **kwargs):
        called_cmds.append(cmd)
        mock = Mock()
        mock.stdout = FakeStdout()
        mock.returncode = 0
        mock.wait = Mock(return_value=0)
        mock.__enter__ = Mock(return_value=mock)
        mock.__exit__ = Mock(return_value=False)
        return mock

    mb = MySQLBackup(
        out_dir=tmp_path,
        user="backup",
        password="secret123",
        host="localhost",
        port=3306,
        options=["--single-transaction"],
    )
    with patch("subprocess.Popen", side_effect=fake_popen):
        mb.backup("mydb")

    assert len(called_cmds) == 1
    cmd = called_cmds[0]
    assert cmd[0] == "mysqldump"
    assert any(part.startswith("--defaults-extra-file=") for part in cmd)
    assert "--password=secret123" not in cmd
    assert "secret123" not in " ".join(cmd)


def test_xtrabackup_uses_defaults_extra_file(tmp_path: Path):
    called_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        called_cmds.append(cmd)
        # The command line includes the target tmp dir; create it so rename works.
        for part in cmd:
            if part.startswith("--target-dir="):
                target = Path(part.split("=", 1)[1])
                target.mkdir(parents=True, exist_ok=True)
        result = subprocess.CompletedProcess(cmd, returncode=0, stdout="ok")
        return result

    xb = XtraBackup(
        backup_root=tmp_path,
        user="xtrabackup",
        password="secret123",
        binary="xtrabackup",
    )

    with patch("subprocess.run", side_effect=fake_run):
        xb.full_backup()

    assert len(called_cmds) == 1
    cmd = called_cmds[0]
    assert cmd[0] == "xtrabackup"
    assert any(part.startswith("--defaults-extra-file=") for part in cmd)
    assert "--password=secret123" not in cmd
    assert "secret123" not in " ".join(cmd)
