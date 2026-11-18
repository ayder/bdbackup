"""``run --validate`` checks that the metrics file can be written."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.validation import os_user

FILE_JOB = (
    '[history]\ndatabase = "state/history.sqlite3"\n'
    '[daily]\ntype = "file"\ntemplate_filename = "paths"\nchdir = "source"\n'
    'backup_dst = "archives/daily"\nformat = "tar.gz"\n'
)


def site(tmp_path, metrics: str | None) -> Path:
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "hello").write_text("hello")
    (tmp_path / "paths").write_text("hello\n")
    config = tmp_path / "config.toml"
    config.write_text(FILE_JOB + (metrics or ""))
    return config


def validate(config, *args):
    return CliRunner().invoke(main, ["run", "-c", str(config), "--validate", *args])


def metrics_lines(result):
    return [line for line in result.output.splitlines() if "Metrics" in line or "metrics" in line]


def test_a_writable_metrics_directory_is_ok(tmp_path):
    (tmp_path / "metrics").mkdir()
    config = site(tmp_path, '[metrics]\npath = "metrics/bdbackup.prom"\n')

    result = validate(config)

    directory = (tmp_path / "metrics").resolve()
    assert (
        f"[OK] Metrics directory: {directory} (read/write/search as OS user {os_user()})."
        in result.output.splitlines()
    ), result.output
    assert result.exit_code == 0, result.output
    assert not [line for line in metrics_lines(result) if "FAIL" in line]


def test_a_missing_metrics_directory_fails_with_the_fix(tmp_path):
    config = site(tmp_path, '[metrics]\npath = "metrics/bdbackup.prom"\n')

    result = validate(config)

    directory = (tmp_path / "metrics").resolve()
    lines = result.output.splitlines()
    assert f"[FAIL] Metrics directory: {directory} does not exist; directory is required." in lines
    assert f"  Required directory: mkdir -p -- {directory}" in lines
    assert f"  OS user {os_user()} needs read/write/search permissions on {directory}." in lines
    assert result.exit_code == 1, result.output
    assert not directory.exists()  # validation creates nothing


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_a_metrics_directory_that_is_not_writable_fails(tmp_path):
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics").chmod(0o500)
    config = site(tmp_path, '[metrics]\npath = "metrics/bdbackup.prom"\n')
    try:
        result = validate(config)
    finally:
        (tmp_path / "metrics").chmod(0o700)

    directory = (tmp_path / "metrics").resolve()
    assert (
        f"[FAIL] Metrics directory: OS user {os_user()} needs read/write/search "
        f"on directory {directory}."
    ) in result.output.splitlines()
    assert result.exit_code == 1, result.output


def test_an_existing_metrics_file_is_replaced_so_only_the_directory_needs_write_access(tmp_path):
    (tmp_path / "metrics").mkdir()
    target = tmp_path / "metrics" / "bdbackup.prom"
    target.write_text("# old\n")
    target.chmod(0o444)  # read-only: the atomic replace does not need to write it
    config = site(tmp_path, '[metrics]\npath = "metrics/bdbackup.prom"\n')

    result = validate(config)

    file_lines = [line for line in result.output.splitlines() if "Metrics file" in line]
    assert len(file_lines) == 1 and file_lines[0].startswith("[OK] Metrics file:"), result.output
    assert str(target.resolve()) in file_lines[0]
    assert "replaced" in file_lines[0]
    assert result.exit_code == 0, result.output


def test_a_directory_where_the_metrics_file_belongs_fails(tmp_path):
    (tmp_path / "metrics" / "bdbackup.prom").mkdir(parents=True)
    config = site(tmp_path, '[metrics]\npath = "metrics/bdbackup.prom"\n')

    result = validate(config)

    assert any(
        line.startswith("[FAIL] Metrics file:") and "is a directory" in line
        for line in result.output.splitlines()
    ), result.output
    assert result.exit_code == 1


def test_a_selected_job_still_shows_the_metrics_line(tmp_path):
    (tmp_path / "metrics").mkdir()
    config = site(tmp_path, '[metrics]\npath = "metrics/bdbackup.prom"\n')

    result = validate(config, "-j", "daily")

    assert any(line.startswith("[OK] Metrics directory:") for line in result.output.splitlines())


def test_without_a_metrics_section_there_is_no_metrics_line(tmp_path):
    config = site(tmp_path, None)

    result = validate(config)

    assert "Metrics" not in result.output
    assert result.exit_code == 0, result.output
