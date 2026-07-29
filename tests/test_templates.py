"""Tests for named exclusion templates (bdbackup.templates)."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup import templates
from bdbackup.cli import main
from bdbackup.filebackup import FileBackup
from bdbackup.templates import (
    ExclusionTemplate,
    TemplateError,
    build_matcher,
    get_template,
    list_templates,
    register_template,
    resolve_patterns,
)


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reset the template registry and point the config dir at a temp path."""
    templates._reset_for_tests()
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    yield
    templates._reset_for_tests()


def _make_project(root: Path) -> None:
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_text("cache")
    (root / "pkg" / "mod.py").write_text("code")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "pip.py").write_text("venv")
    (root / "uv.lock").write_text("lock")
    (root / "pyproject.toml").write_text("project")


def _archive_members(archive: Path) -> set[str]:
    with tarfile.open(archive, "r") as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


# --- registry ---------------------------------------------------------------


def test_builtin_python_dev_is_registered():
    template = get_template("python-dev")
    assert "uv.lock" in template.patterns
    assert "__pycache__/" in template.patterns
    assert ".venv/" in template.patterns


def test_unknown_template_lists_available_names():
    with pytest.raises(TemplateError, match="python-dev"):
        get_template("cobol-dev")


def test_register_template_api_extends_registry():
    register_template(ExclusionTemplate(name="go-dev", patterns=("vendor/", "vendor/**")))
    assert set(list_templates()) == {"python-dev", "go-dev"}
    assert resolve_patterns(["python-dev", "go-dev"])[:2] == list(
        get_template("python-dev").patterns[:2]
    )


def test_user_template_loaded_from_config_dir(tmp_path: Path):
    user_dir = tmp_path / "cfg" / "templates"
    user_dir.mkdir(parents=True)
    (user_dir / "custom_dev.py").write_text(
        "from bdbackup.templates import ExclusionTemplate\n"
        "TEMPLATE = ExclusionTemplate(name='custom-dev', patterns=('*.img',), "
        "description='user preset')\n"
    )
    template = get_template("custom-dev")
    assert template.patterns == ("*.img",)
    assert template.description == "user preset"


def test_broken_user_template_does_not_crash_discovery(tmp_path: Path):
    user_dir = tmp_path / "cfg" / "templates"
    user_dir.mkdir(parents=True)
    (user_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    assert "python-dev" in list_templates()


# --- matcher ----------------------------------------------------------------


def test_matcher_basename_dir_and_negation():
    matcher = build_matcher(["*.log", "!keep.log", "cache/"])
    root = Path("/proj")
    assert matcher.matches(Path("/proj/a/b/app.log"), False, [root])
    assert not matcher.matches(Path("/proj/keep.log"), False, [root])
    assert matcher.matches(Path("/proj/x/cache"), True, [root])
    assert not matcher.matches(Path("/proj/x/cache"), False, [root])


def test_matcher_relative_path_pattern():
    matcher = build_matcher(["tests/containers/*img"])
    root = Path("/proj")
    assert matcher.matches(Path("/proj/tests/containers/disk.img"), False, [root])
    assert matcher.matches(Path("/proj/tests/containers/disk.qcow2img"), False, [root])
    assert not matcher.matches(Path("/proj/other/disk.img"), False, [root])


def test_matcher_anchored_pattern():
    matcher = build_matcher(["/build/"])
    root = Path("/proj")
    assert matcher.matches(Path("/proj/build"), True, [root])
    # A same-named directory inside the candidate also matches; anchoring
    # prevents matches relative to *other* roots.
    assert not matcher.matches(Path("/elsewhere/build"), True, [root])


# --- FileBackup integration -------------------------------------------------


def test_file_backup_excludes_python_dev_artifacts(tmp_path: Path):
    src = tmp_path / "proj"
    src.mkdir()
    _make_project(src)
    template = tmp_path / "template.txt"
    template.write_text("proj\n")

    fb = FileBackup(
        backup_dst=tmp_path / "archive",
        template_filename=template,
        chdir=tmp_path,
        exclude_templates=["python-dev"],
    )
    result = fb.backup()
    members = _archive_members(result.path)
    assert members == {"proj/pkg/mod.py", "proj/pyproject.toml"}


def test_file_backup_template_combines_with_other_excludes(tmp_path: Path):
    src = tmp_path / "proj"
    src.mkdir()
    _make_project(src)
    (src / "notes.log").write_text("log")
    template = tmp_path / "template.txt"
    template.write_text("proj\n")

    fb = FileBackup(
        backup_dst=tmp_path / "archive",
        template_filename=template,
        chdir=tmp_path,
        exclude_pattern=["*.log"],
        exclude_templates=["python-dev"],
    )
    result = fb.backup()
    members = _archive_members(result.path)
    assert members == {"proj/pkg/mod.py", "proj/pyproject.toml"}


def test_file_backup_unknown_template_raises(tmp_path: Path):
    with pytest.raises(TemplateError):
        FileBackup(backup_dst=tmp_path / "archive", exclude_templates=["nope"])


def test_relative_exclude_resolves_against_chdir(tmp_path: Path):
    src = tmp_path / "proj"
    (src / "uploads" / "tmp").mkdir(parents=True)
    (src / "uploads" / "tmp" / "scratch.bin").write_text("tmp")
    (src / "uploads" / "keep.txt").write_text("keep")
    template = tmp_path / "template.txt"
    template.write_text("proj\n")

    fb = FileBackup(
        backup_dst=tmp_path / "archive",
        template_filename=template,
        chdir=tmp_path,
        exclude=["proj/uploads/tmp"],  # relative to chdir, not the process CWD
    )
    result = fb.backup()
    members = _archive_members(result.path)
    assert members == {"proj/uploads/keep.txt"}


# --- CLI --------------------------------------------------------------------


def _write_cli_project(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "proj"
    src.mkdir()
    _make_project(src)
    template = tmp_path / "template.txt"
    template.write_text("proj\n")
    return template, tmp_path / "archive"


def test_cli_exclude_template_end_to_end(tmp_path: Path):
    template, archive = _write_cli_project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "file",
            "--template",
            str(template),
            "-d",
            str(archive),
            "-c",
            str(tmp_path),
            "--exclude-template",
            "python-dev",
        ],
    )
    assert result.exit_code == 0, result.output
    members = _archive_members(archive.with_suffix(".tar"))
    assert members == {"proj/pkg/mod.py", "proj/pyproject.toml"}


def test_cli_exclude_template_repeated_and_comma_separated(tmp_path: Path):
    register_template(ExclusionTemplate(name="logs-dev", patterns=("*.log",)))
    template, archive = _write_cli_project(tmp_path)
    (tmp_path / "proj" / "debug.log").write_text("log")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "file",
            "--template",
            str(template),
            "-d",
            str(archive),
            "-c",
            str(tmp_path),
            "--exclude-template",
            "python-dev,logs-dev",
            "--exclude-template",
            "python-dev",
        ],
    )
    assert result.exit_code == 0, result.output
    members = _archive_members(archive.with_suffix(".tar"))
    assert members == {"proj/pkg/mod.py", "proj/pyproject.toml"}


def test_cli_unknown_template_is_usage_error(tmp_path: Path):
    template, archive = _write_cli_project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "file",
            "--template",
            str(template),
            "-d",
            str(archive),
            "--exclude-template",
            "cobol-dev",
        ],
    )
    assert result.exit_code == 2
    assert "cobol-dev" in result.output
    assert "python-dev" in result.output


def test_cli_dry_run_lists_only_included_files(tmp_path: Path, caplog):
    template, archive = _write_cli_project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--logging",
            "DEBUG",
            "file",
            "--template",
            str(template),
            "-d",
            str(archive),
            "-c",
            str(tmp_path),
            "--exclude-template",
            "python-dev",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not archive.with_suffix(".tar").exists()
    backed_up = [
        line.split("Would back up: ", 1)[1]
        for line in caplog.text.splitlines()
        if "Would back up: " in line
    ]
    assert any("mod.py" in line for line in backed_up)
    assert not any("uv.lock" in line for line in backed_up)
    assert not any("__pycache__" in line for line in backed_up)
    assert not any(".venv" in line for line in backed_up)


# --- TOML config ------------------------------------------------------------


def test_config_job_with_exclude_templates(tmp_path: Path):
    src = tmp_path / "proj"
    src.mkdir()
    _make_project(src)
    template = tmp_path / "template.txt"
    template.write_text("proj\n")
    archive = tmp_path / "archive"
    cfg_path = tmp_path / "jobs.toml"
    cfg_path.write_text(
        f"""
[files]
type = "file"
backup_dst = "{archive}"
template_filename = "{template}"
chdir = "{tmp_path}"
exclude_templates = ["python-dev"]
"""
    )
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--config", str(cfg_path), "files"])
    assert result.exit_code == 0, result.output
    members = _archive_members(archive.with_suffix(".tar"))
    assert members == {"proj/pkg/mod.py", "proj/pyproject.toml"}
