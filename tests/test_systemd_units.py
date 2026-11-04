"""The shipped systemd units keep their promised settings and match the real CLI."""

import configparser
import shlex
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup.cli import main

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "contrib" / "systemd"
SERVICES = ["bdbackup@.service", "bdbackup-incremental@.service"]
TIMERS = {
    "bdbackup@.timer": "bdbackup@%i.service",
    "bdbackup-incremental@.timer": "bdbackup-incremental@%i.service",
}
LEFT_TO_DROP_INS = {
    "CPUQuota", "CPUWeight", "IOWeight", "IOReadBandwidthMax", "IOWriteBandwidthMax",
    "MemoryHigh", "MemoryMax", "MemorySwapMax", "PrivateTmp", "ProtectSystem", "ProtectHome",
}


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string((UNITS / name).read_text())
    return parser


@pytest.mark.parametrize("name", SERVICES)
def test_service_runs_as_mysql_and_dies_as_one_cgroup(name):
    service = unit(name)["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "mysql"
    assert service["Group"] == "mysql"
    assert service["KillMode"] == "control-group"
    assert service["Environment"] == "BDBACKUP_CONFIG=/etc/bdbackup/config.toml"


@pytest.mark.parametrize("name", [*SERVICES, *TIMERS])
def test_job_name_is_not_unescaped(name):
    """%I would turn mysql-prod into mysql/prod."""
    assert "%i" in (UNITS / name).read_text()
    assert "%I" not in (UNITS / name).read_text()


@pytest.mark.parametrize("name", SERVICES)
def test_limits_and_sandboxing_are_left_to_drop_ins(name):
    assert LEFT_TO_DROP_INS.isdisjoint(unit(name)["Service"])


@pytest.mark.parametrize(("name", "target"), TIMERS.items())
def test_timer_has_no_default_schedule_and_no_catch_up(name, target):
    timer = unit(name)
    assert timer["Timer"]["Unit"] == target
    assert timer["Timer"]["Persistent"] == "false"
    assert "OnCalendar" not in timer["Timer"]
    assert timer["Install"]["WantedBy"] == "timers.target"


@pytest.mark.parametrize(
    ("name", "exit_code", "output"),
    [
        ("bdbackup@.service", 0, "Dry run; would take a full backup"),
        # Past argument parsing; a dry incremental needs a full, so a usage error (2) is the bug.
        ("bdbackup-incremental@.service", 1, ""),
    ],
)
def test_exec_start_is_a_command_the_cli_accepts(tmp_path, name, exit_code, output):
    config = tmp_path / "config.toml"
    config.write_text('[mysql-prod]\ntype="xtrabackup"\nbackup_root="backups"\n')
    command = unit(name)["Service"]["ExecStart"]
    command = command.replace("${BDBACKUP_CONFIG}", str(config)).replace("%i", "mysql-prod")
    program, *args = shlex.split(command)
    assert program == "bdbackup"
    result = CliRunner().invoke(main, [*args, "--dry-run"])
    assert result.exit_code == exit_code
    assert output in result.output


def test_sdist_carries_the_units():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "/contrib" in project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]


@pytest.mark.parametrize(
    "fact",
    [
        "ln -s",  # the binary link: without it the unit fails with 203/EXEC
        "chmod 640",  # the config holds passwords and mysql must read it
        "OnCalendar=",  # the schedule comes before enable
        "--validate",  # proves the mysql account's access before a first run
        "MemoryHigh=",  # the first limit to set
        "cgroup v2",
        "Never run one job from both",
    ],
)
def test_systemd_document_states_what_an_operator_trips_on(fact):
    assert fact in (ROOT / "docs" / "README_SYSTEMD.md").read_text()


@pytest.mark.parametrize("document", ["README.md", "CLAUDE.md"])
def test_systemd_document_is_linked(document):
    assert "README_SYSTEMD.md" in (ROOT / document).read_text()
