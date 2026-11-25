"""Host-side tests of the PBM witness in the installed-wheel smoke script."""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts.smoke_install import _check_pbm_dry_run


def dry_run_line(work):
    return f"Dry run; would take a physical PBM backup and export it into {work / 'pbm-units'}\n"


def test_smoke_exercises_installed_pbm_cli_without_credentials_or_a_server(tmp_path, monkeypatch):
    work = tmp_path.resolve()
    cli = work / "venv" / "bin" / "bdbackup"
    calls = []
    monkeypatch.setenv("PBM_MONGODB_URI", "mongodb://fixture.invalid/")

    def run(argv, *, process_env):
        calls.append(argv)
        assert argv == [str(cli), "run", "-c", "pbm.toml", "-j", "mongo", "--dry-run"]
        options = tomllib.loads((work / "pbm.toml").read_text())
        assert options["history"]["database"] == "pbm-state/history.sqlite3"
        assert options["mongo"] == {
            "type": "pbm", "backup_type": "physical",
            "storage_path": "pbm-storage", "out_dir": "pbm-units",
        }
        assert "PBM_MONGODB_URI" not in process_env
        assert os.access(Path(process_env["PATH"]) / "pbm", os.X_OK)
        return subprocess.CompletedProcess(argv, 0, dry_run_line(work), "")

    _check_pbm_dry_run(work, cli, run)
    assert len(calls) == 1, "the smoke check must execute the installed wheel's PBM dry run"


@pytest.mark.parametrize("bad", ["line", "pbm-invoked", "pbm-state"])
def test_smoke_refuses_a_pbm_dry_run_that_acts_or_reports_the_wrong_output(tmp_path, bad):
    work = tmp_path.resolve()

    def run(argv, *, process_env):
        stdout = dry_run_line(work)
        if bad == "line":
            stdout = "wrong dry-run line\n"
        elif bad == "pbm-invoked":
            Path(process_env["BDBACKUP_SMOKE_PBM_MARKER"]).touch()
        else:
            (work / bad).mkdir()
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    with pytest.raises(RuntimeError, match="Installed wheel PBM dry run"):
        _check_pbm_dry_run(work, work / "bdbackup", run)
