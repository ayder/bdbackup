"""CLI smoke tests."""

from pathlib import Path

from click.testing import CliRunner

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
    result = runner.invoke(main, ["file", str(template), "--dst", str(dst)])
    assert result.exit_code == 0, result.output
    assert dst.with_suffix(".tar").exists()


def test_db_help():
    runner = CliRunner()
    for args in [["db", "--help"], ["db", "mysqldump", "--help"], ["db", "xtrabackup", "--help"]]:
        result = runner.invoke(main, args)
        assert result.exit_code == 0, args
