"""The PBM guide's worked example runs locally as written in dry-run mode."""

import re
import subprocess
import tomllib
from pathlib import Path

from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "README_PBM.md"


def test_documented_pbm_job_dry_runs_with_gfs_and_copy(tmp_path, monkeypatch):
    assert GUIDE.is_file(), "the PBM guide must include a complete job example"
    example = re.search(r"^```toml\n(.*?)^```", GUIDE.read_text(encoding="utf-8"), re.M | re.S)
    assert example, "the PBM guide must include a complete TOML example first"
    settings = tomllib.loads(example[1])
    job = settings["mongo-nightly"]
    config = tmp_path / "config.toml"
    config.write_text(example[1], encoding="utf-8")
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "engine-config"))

    def unexpected(*args, **kwargs):
        raise AssertionError("the documented dry run launched an external command")

    monkeypatch.setattr(subprocess, "run", unexpected)
    result = CliRunner().invoke(
        main, ["run", "-c", str(config), "-j", "mongo-nightly", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    out_dir = (tmp_path / job["out_dir"]).resolve()
    copy = job["copy"]
    assert result.stdout.splitlines() == [
        f"Dry run; would take a {job['backup_type']} PBM backup and export it into {out_dir}",
        f"Dry run; configured copy: {copy['destination']} ({copy['method']})",
    ]
    first = Config(config).get("rotate-mongo").params["stage"][0].paths[0]
    assert out_dir.is_relative_to(first)
    assert set(tmp_path.iterdir()) == {config}
