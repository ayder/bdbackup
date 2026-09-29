"""Engine conditions GFS relies on: xtrabackup retention 0 and timestamped file archives."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from tests.conftest import write_checkpoints


def invoke(*args):
    return CliRunner().invoke(main, list(map(str, args)))


@pytest.fixture
def old_chain(tmp_path):
    day = tmp_path / "physical/db" / (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    write_checkpoints(day / "Full_000000")
    (day / "Full_Latest").symlink_to("Full_000000")
    (day / ".full_success").touch()
    return day / "Full_000000"


def xtrabackup_config(tmp_path, retention_days):
    path = tmp_path / "config.toml"
    path.write_text(
        '[xb]\ntype="xtrabackup"\nbackup_root="physical/db"\nbinary="mariadb-backup"\n'
        f"retention_days={retention_days}\n"
    )
    return path


@pytest.mark.parametrize("surface", ["prune-cli", "full-cli", "config-run"])
def test_retention_zero_disables_engine_deletion(tmp_path, old_chain, physical_runner, surface):
    root = tmp_path / "physical"
    with patch("subprocess.run", side_effect=physical_runner):
        if surface == "prune-cli":
            result = invoke("xtrabackup", "prune", "--database", "db", "-r", root,
                            "--retention", "0")
        elif surface == "full-cli":
            result = invoke("xtrabackup", "full", "--database", "db", "-r", root,
                            "--retention", "0", "--binary", "mariadb-backup")
        else:
            result = invoke("run", "--config", xtrabackup_config(tmp_path, 0), "xb")
    assert result.exit_code == 0, result.output
    assert old_chain.is_dir()
    if surface == "prune-cli":
        assert "Engine deletion is disabled (retention 0); nothing pruned" in result.output


def test_retention_days_setting_accepts_zero_not_negative(tmp_path):
    zero = invoke("run", "--validate", "--config", xtrabackup_config(tmp_path, 0))
    assert "retention_days must be" not in zero.output, zero.output
    negative = invoke("run", "--validate", "--config", xtrabackup_config(tmp_path, -1))
    assert "retention_days must be an integer >= 0" in negative.output, negative.output
