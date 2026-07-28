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
    result = runner.invoke(
        main,
        ["file", "--template", str(template), "-d", str(dst)],
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
    assert "--database" in result.output
    assert "full" in result.output
    assert "incremental" in result.output
