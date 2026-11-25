"""The published SSH configuration example is runnable with a local dry run."""

import re
import subprocess
import tomllib
from pathlib import Path

from click.testing import CliRunner

from bdbackup.cli import main

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "README_COPY.md"


def test_documented_copy_example_can_dry_run_without_ssh(tmp_path, monkeypatch):
    assert GUIDE.is_file(), "the SSH copy guide must contain a runnable TOML example"
    snippet = re.search(r"^```toml\n(.*?)^```", GUIDE.read_text(), re.M | re.S)
    assert snippet, "the SSH copy guide must show a complete TOML example first"
    text = snippet[1]
    example = tomllib.loads(text)
    job = example["files-daily"]
    source = tmp_path / job["chdir"]
    source.mkdir()
    (source / "payload").write_text("fixture")
    (tmp_path / job["template_filename"]).write_text("payload\n")
    config = tmp_path / "config.toml"
    config.write_text(text)

    def unexpected(*args, **kwargs):
        raise AssertionError("the documented dry run launched an external command")

    monkeypatch.setattr(subprocess, "run", unexpected)
    result = CliRunner().invoke(main, ["run", "-c", str(config), "-j", "files-daily", "--dry-run"])

    assert result.exit_code == 0, result.output
    copy = job["copy"]
    assert f"Dry run; configured copy: {copy['destination']} ({copy['method']})" in result.stdout
    assert not (tmp_path / example["history"]["database"]).exists()
