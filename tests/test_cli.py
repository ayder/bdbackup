"""CLI smoke tests."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bdbackup.backends import BackupResult
from bdbackup.cli import main


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "bdbackup" in result.output


def test_file_backup(tmp_path: Path):
    template = tmp_path / "template.txt"
    template.write_text("hello.txt\n")
    src = tmp_path / "hello.txt"
    src.write_text("world")

    dst = tmp_path / "backup"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["file", "--template", str(template), "-d", str(dst), "-c", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert dst.with_suffix(".tar").exists()


def test_mysqldump_help():
    runner = CliRunner()
    result = runner.invoke(main, ["mysqldump", "--help"])
    assert result.exit_code == 0
    assert "--database" in result.output


def test_xtrabackup_help():
    runner = CliRunner()
    result = runner.invoke(main, ["xtrabackup", "--help"])
    assert result.exit_code == 0
    assert "full" in result.output
    assert "incremental" in result.output
    assert "prune" in result.output


def test_mysqldump_full_without_database():
    runner = CliRunner()
    with patch("bdbackup.cli.MySQLBackup") as mock_cls:
        instance = mock_cls.return_value
        instance.backup.return_value = BackupResult(Path("/tmp/all-databases.sql.gz"), success=True)
        result = runner.invoke(main, ["mysqldump", "--full"])
        assert result.exit_code == 0, result.output
        instance.backup.assert_called_once_with(None)


def test_mysqldump_database_required_without_full():
    runner = CliRunner()
    result = runner.invoke(main, ["mysqldump"])
    assert result.exit_code != 0
    assert "--database is required" in result.output
