"""The pgbackrest engine: its keys, its configuration rules and the choice of a full."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupError, BackupResult
from bdbackup.cli import main
from bdbackup.config import Config, ConfigError, build_backend
from bdbackup.engines import _reset_for_tests, get_engine, list_engines
from bdbackup.gfs.stages import MARKER, fed_first_stages, pgbackrest_first_stage
from bdbackup.history import History, HistorySettings
from bdbackup.postgres import PgBackRest
from bdbackup.postgres.pgbackrest import ENGINE, TESTED_VERSIONS, Choice
from bdbackup.postgres.repository import FullBackup, RepositoryError
from tests.pgbackrest_fake import fake_pgbackrest, info_json, make_repo, missing_stanza_info

OLDER = "20261108-010000F"
NEWEST = "20261109-010000F"
DIFF = "20261109-010000F_20261110-010000D"
RUN = "bdbackup.postgres.pgbackrest.subprocess.run"


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    """Give each test a fresh registry isolated from the user's config dir."""
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---- registration ---------------------------------------------------------------------------


def test_registry_has_pgbackrest():
    assert "pgbackrest" in list_engines()
    info = get_engine("pgbackrest")
    assert info.family == "postgres"
    assert info.backend is PgBackRest
    assert info.description == "pgBackRest full backups exported as one tar"
    assert info == ENGINE


# ---- configuration --------------------------------------------------------------------------

GFS = """[gfs-main]
type = "gfs"
[[gfs-main.stage]]
paths = ["STAGE1"]
period = "daily"
keep = "5d"
[[gfs-main.stage]]
paths = ["NFS/weekly"]
period = "weekly"
keep = "8w"
"""


def write_config(
    tmp_path, *, history=True, gfs=True, out_dir="STAGE1/pg/main", repo_path="repo", extra=""
):
    text = '[history]\ndatabase = "state/history.sqlite3"\n' if history else ""
    text += GFS if gfs else ""
    text += (
        f'[pg]\ntype = "pgbackrest"\nstanza = "main"\nrepo_path = "{repo_path}"\n'
        f'out_dir = "{out_dir}"\n{extra}'
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_config_requires_history(tmp_path):
    with pytest.raises(ConfigError) as refused:
        Config(write_config(tmp_path, history=False, gfs=False))

    assert str(refused.value) == "Job 'pg': pgbackrest jobs require a [history] section"


@pytest.mark.parametrize(
    ("gfs", "out_dir", "message"),
    [
        pytest.param(False, "STAGE1/pg", "out_dir must be inside the first stage of a gfs job",
                     id="no-gfs-job"),
        pytest.param(True, "elsewhere/pg", "out_dir must be inside the first stage of a gfs job",
                     id="outside-every-stage"),
        pytest.param(True, ".", "out_dir must be inside the first stage of a gfs job",
                     id="above-the-first-stage"),
        pytest.param(True, "NFS/weekly/pg", "engines write only into the first stage",
                     id="in-a-later-stage"),
    ],
)
def test_config_requires_first_stage(tmp_path, gfs, out_dir, message):
    with pytest.raises(ConfigError, match=message) as refused:
        Config(write_config(tmp_path, gfs=gfs, out_dir=out_dir))

    assert "Job 'pg'" in str(refused.value)


@pytest.mark.parametrize(
    "repo_path",
    [
        pytest.param(".", id="repo-above-out-dir"),
        pytest.param("STAGE1/pg/main", id="repo-equal-to-out-dir"),
        pytest.param("STAGE1", id="repo-equal-to-the-first-stage"),
        pytest.param("STAGE1/pg/../pg", id="repo-with-dotdot-holding-out-dir"),
    ],
)
def test_config_rejects_out_dir_inside_repo(tmp_path, repo_path):
    with pytest.raises(ConfigError) as refused:
        Config(write_config(tmp_path, repo_path=repo_path))

    assert str(refused.value) == "Job 'pg': out_dir must be outside repo_path"


@pytest.mark.parametrize(
    "repo_path",
    [
        pytest.param("STAGE1/repo", id="repo-in-the-first-stage"),
        pytest.param("NFS/weekly", id="repo-equal-to-a-later-stage"),
        pytest.param("NFS/weekly/repo", id="repo-in-a-later-stage"),
        pytest.param("STAGE1/../STAGE1/repo", id="repo-with-dotdot-in-the-first-stage"),
    ],
)
def test_config_rejects_repo_inside_stage(tmp_path, repo_path):
    with pytest.raises(ConfigError) as refused:
        Config(write_config(tmp_path, out_dir="STAGE1/pg", repo_path=repo_path))

    assert str(refused.value) == "Job 'pg': repo_path must be outside every gfs stage path"


@pytest.mark.parametrize(
    "repo_path",
    [
        pytest.param("NFS", id="repo-above-a-later-stage"),
        pytest.param("NFS/../NFS", id="repo-with-dotdot-above-a-later-stage"),
    ],
)
def test_config_rejects_stage_inside_repo(tmp_path, repo_path):
    with pytest.raises(ConfigError) as refused:
        Config(write_config(tmp_path, out_dir="STAGE1/pg", repo_path=repo_path))

    assert str(refused.value) == "Job 'pg': a gfs stage path must be outside repo_path"


def test_config_accepts_repo_beside_the_stages(tmp_path):
    job = Config(write_config(tmp_path, repo_path="STAGE1-repo")).jobs["pg"]

    assert job.params["repo_path"] == tmp_path.resolve() / "STAGE1-repo"


@pytest.mark.parametrize("out_dir", ["STAGE1", "STAGE1/pg/main"])
def test_config_accepts_out_dir_in_or_equal_to_the_first_stage(tmp_path, out_dir):
    job = Config(write_config(tmp_path, out_dir=out_dir)).jobs["pg"]

    assert job.params["first_stage"] == tmp_path.resolve() / "STAGE1"


def test_config_injects_first_stage_and_lock(tmp_path):
    config = Config(write_config(tmp_path))
    params = config.jobs["pg"].params

    assert params["first_stage"] == tmp_path.resolve() / "STAGE1"
    assert params["lock_file"] == Path(f"{config.history.database}.pgbackrest-pg.lock")
    assert params["lock_file"].parent == tmp_path.resolve() / "state"


def test_config_resolves_path_keys_against_the_config_file(tmp_path):
    config = Config(write_config(tmp_path, extra='config_file = "pgbackrest.conf"\n'))
    params = config.jobs["pg"].params

    assert params["repo_path"] == tmp_path.resolve() / "repo"
    assert params["config_file"] == tmp_path.resolve() / "pgbackrest.conf"
    assert params["out_dir"] == tmp_path.resolve() / "STAGE1/pg/main"


@pytest.mark.parametrize("key", ["first_stage", "lock_file"])
def test_config_rejects_internal_keys(tmp_path, key):
    with pytest.raises(ConfigError) as refused:
        Config(write_config(tmp_path, extra=f'{key} = "x"\n'))

    message = str(refused.value)
    assert message.startswith("Job 'pg': ")
    assert key in message


@pytest.mark.parametrize("value", ["0", "-1", '"12"', "true", "1.5"])
def test_config_rejects_bad_max_age(tmp_path, value):
    with pytest.raises(ConfigError) as refused:
        Config(write_config(tmp_path, extra=f"max_full_age_hours = {value}\n"))

    assert str(refused.value) == "Job 'pg': max_full_age_hours must be a positive whole number"


def test_config_keeps_a_good_max_age_and_repo(tmp_path):
    params = Config(write_config(tmp_path, extra="max_full_age_hours = 36\nrepo = 2\n")).jobs[
        "pg"
    ].params

    assert params["max_full_age_hours"] == 36
    assert params["repo"] == 2


@pytest.mark.parametrize("value", ["0", "-1", '"1"', "true", "1.5"])
def test_config_rejects_bad_repo(tmp_path, value):
    with pytest.raises(ConfigError) as refused:
        Config(write_config(tmp_path, extra=f"repo = {value}\n"))

    assert str(refused.value) == "Job 'pg': repo must be a whole number of 1 or more"


def test_pgbackrest_job_builds_through_the_registry(tmp_path):
    backend = build_backend(Config(write_config(tmp_path)).jobs["pg"])

    assert isinstance(backend, PgBackRest)
    assert backend.stanza == "main"
    assert backend.first_stage == tmp_path.resolve() / "STAGE1"


def test_pgbackrest_first_stage_and_fed_first_stages(tmp_path):
    config = Config(write_config(tmp_path))

    first = pgbackrest_first_stage(config.jobs["pg"], config.jobs)

    assert first == tmp_path.resolve() / "STAGE1"
    assert fed_first_stages(config.jobs) == {first}


def test_fed_first_stages_counts_inactive_job(tmp_path):
    config = Config(write_config(tmp_path, extra="active = false\n"))

    assert config.jobs["pg"].active is False
    assert fed_first_stages(config.jobs) == {tmp_path.resolve() / "STAGE1"}


def test_fed_first_stages_is_empty_without_a_pgbackrest_job(tmp_path):
    config = Config(write_config(tmp_path))
    jobs = {name: job for name, job in config.jobs.items() if job.type == "gfs"}

    assert fed_first_stages(jobs) == set()


def test_pgbackrest_first_stage_refuses_a_job_without_out_dir(tmp_path):
    config = Config(write_config(tmp_path))
    job = config.jobs["pg"]
    del job.params["out_dir"]

    with pytest.raises(ValueError, match="out_dir must be inside the first stage of a gfs job"):
        pgbackrest_first_stage(job, config.jobs)


# ---- the constructor and its commands -------------------------------------------------------


def make_engine(tmp_path, **overrides):
    params = {
        "stanza": "main",
        "repo_path": tmp_path / "repo",
        "out_dir": tmp_path / "stage1" / "pg",
        "first_stage": tmp_path / "stage1",
        "lock_file": tmp_path / "state" / "history.sqlite3.pgbackrest-pg.lock",
    }
    params.update(overrides)
    return PgBackRest(**params)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"stanza": ""}, id="empty-stanza"),
        pytest.param({"stanza": "  "}, id="blank-stanza"),
        pytest.param({"repo": 0}, id="repo-zero"),
        pytest.param({"repo": -1}, id="repo-negative"),
        pytest.param({"repo": True}, id="repo-bool"),
        pytest.param({"repo": "1"}, id="repo-text"),
        pytest.param({"repo": 1.5}, id="repo-float"),
        pytest.param({"max_full_age_hours": 0}, id="age-zero"),
        pytest.param({"max_full_age_hours": -5}, id="age-negative"),
        pytest.param({"max_full_age_hours": True}, id="age-bool"),
        pytest.param({"max_full_age_hours": "12"}, id="age-text"),
        pytest.param({"max_full_age_hours": 1.5}, id="age-float"),
    ],
)
def test_constructor_rejects_bad_values(tmp_path, overrides):
    with pytest.raises(ValueError):
        make_engine(tmp_path, **overrides)


def test_constructor_defaults(tmp_path):
    engine = make_engine(tmp_path)

    assert engine.repo == 1
    assert engine.binary == "pgbackrest"
    assert engine.config_file is None
    assert engine.max_full_age_hours is None


def test_command_shape(tmp_path):
    engine = make_engine(
        tmp_path,
        repo=2,
        binary="/usr/bin/pgbackrest",
        config_file=tmp_path / "pgbackrest.conf",
    )

    assert engine.command("info", "--output=json") == [
        "/usr/bin/pgbackrest",
        f"--config={tmp_path / 'pgbackrest.conf'}",
        "--repo=2",
        f"--repo2-path={tmp_path / 'repo'}",
        "--stanza=main",
        "info",
        "--output=json",
    ]


def test_command_shape_without_config_file(tmp_path):
    assert make_engine(tmp_path).command("info") == [
        "pgbackrest",
        "--repo=1",
        f"--repo1-path={tmp_path / 'repo'}",
        "--stanza=main",
        "info",
    ]


def test_unit_path(tmp_path):
    engine = make_engine(tmp_path)

    assert engine.unit_path(NEWEST) == tmp_path / "stage1" / "pg" / f"main-{NEWEST}.tar"


def test_version_runs_the_binary_alone(tmp_path):
    run = fake_pgbackrest(make_repo(tmp_path), version="2.58.0")
    engine = make_engine(tmp_path, binary="pgbackrest-x", config_file=tmp_path / "x.conf")

    with patch(RUN, run):
        assert engine.version() == "2.58.0"

    assert run.calls == [["pgbackrest-x", "version"]]


def test_version_refuses_other_output(tmp_path):
    engine = make_engine(tmp_path)
    garbled = subprocess.CompletedProcess([], 0, stdout="something else\n", stderr="")

    with patch(RUN, return_value=garbled), pytest.raises(BackupError, match="version"):
        engine.version()


def test_info_runs_the_info_command_and_parses_json(tmp_path):
    repo = make_repo(tmp_path)
    run = fake_pgbackrest(repo)
    engine = make_engine(tmp_path, repo_path=repo, config_file=tmp_path / "x.conf")

    with patch(RUN, run):
        document = engine.info()

    assert document == info_json(repo, "main")
    assert run.calls == [engine.command("info", "--output=json")]


@pytest.mark.parametrize("output", ["not json", '{"name": "main"}', "[1]"])
def test_info_refuses_output_that_is_not_the_info_document(tmp_path, output):
    engine = make_engine(tmp_path)
    produced = subprocess.CompletedProcess([], 0, stdout=output, stderr="")

    with (
        patch(RUN, return_value=produced),
        pytest.raises(BackupError, match="did not print a JSON list of stanzas"),
    ):
        engine.info()


def test_commands_run_with_check_capture_and_text(tmp_path):
    seen = []

    def run(cmd, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="pgBackRest 2.58.0\n", stderr="")

    with patch(RUN, run):
        make_engine(tmp_path).version()

    assert seen == [{"check": True, "capture_output": True, "text": True}]


def test_a_failing_command_becomes_a_backup_error_with_the_last_stderr_line(tmp_path):
    engine = make_engine(tmp_path)
    stderr = "P00   INFO: info command begin\nP00  ERROR: [055]: unable to open file\n\n"

    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(55, cmd, output="", stderr=stderr)

    with patch(RUN, run), pytest.raises(BackupError) as failed:
        engine.info()

    assert "P00  ERROR: [055]: unable to open file" in str(failed.value)
    assert "info command begin" not in str(failed.value)


def test_a_failing_command_without_stderr_still_says_why(tmp_path):
    engine = make_engine(tmp_path)

    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(2, cmd, output="", stderr="")

    with patch(RUN, run), pytest.raises(BackupError, match="exit status 2"):
        engine.version()


# ---- restore info ---------------------------------------------------------------------------


def test_restore_info(tmp_path):
    full = FullBackup(
        label=NEWEST,
        stop=datetime(2026, 11, 9, 1, 1, tzinfo=UTC),
        pg_version="16",
        page_errors=True,
        repo_bytes=1234,
    )

    assert make_engine(tmp_path).restore_info(full, "2.58.0") == {
        "kind": "pgbackrest",
        "stanza": "main",
        "label": NEWEST,
        "pgbackrest_version": "2.58.0",
        "pg_version": "16",
        "page_errors": True,
    }


# ---- choosing a full ------------------------------------------------------------------------


def _settings(tmp_path):
    return HistorySettings(
        database=tmp_path / "state" / "history.sqlite3", restore_root=tmp_path / "restores"
    )


@pytest.fixture
def history(tmp_path):
    return History(_settings(tmp_path))


def record_export(history, tmp_path, label, *, job="pg", succeeds=True):
    """Put one attempt to export ``label`` in the history, successful or failed."""
    unit = tmp_path / f"{job}-{label}.tar"
    unit.write_bytes(b"a tar")

    def action():
        if not succeeds:
            raise BackupError("export failed")
        return BackupResult(unit, size_bytes=5, success=True)

    info = {"kind": "pgbackrest", "stanza": "main", "label": label}
    if succeeds:
        history.run(job, "pgbackrest-full", action, info)
    else:
        with pytest.raises(BackupError):
            history.run(job, "pgbackrest-full", action, info)


def choose(tmp_path, history, **make_repo_args):
    repo = make_repo(tmp_path, **make_repo_args)
    with patch(RUN, fake_pgbackrest(repo)):
        return make_engine(tmp_path, repo_path=repo).choose(history, "pg")


def test_choose_new_full(tmp_path, history):
    choice = choose(
        tmp_path,
        history,
        fulls=((OLDER, True, False), (NEWEST, True, False), (DIFF, True, False)),
    )

    assert isinstance(choice, Choice)
    assert choice.new is True
    assert choice.full.label == NEWEST
    assert choice.full.pg_version == "16"
    assert choice.full.page_errors is False


def test_choose_already_exported(tmp_path, history):
    record_export(history, tmp_path, NEWEST)

    choice = choose(tmp_path, history, fulls=((OLDER, True, False), (NEWEST, True, False)))

    assert choice.new is False
    assert choice.full.label == NEWEST


def test_choose_failed_record_does_not_count(tmp_path, history):
    record_export(history, tmp_path, NEWEST, succeeds=False)

    assert choose(tmp_path, history).new is True


def test_choose_another_label_does_not_count(tmp_path, history):
    record_export(history, tmp_path, OLDER)

    choice = choose(tmp_path, history, fulls=((OLDER, True, False), (NEWEST, True, False)))

    assert choice.new is True
    assert choice.full.label == NEWEST


def test_choose_another_job_does_not_count(tmp_path, history):
    record_export(history, tmp_path, NEWEST, job="other")

    assert choose(tmp_path, history).new is True


def test_choose_before_the_history_database_exists(tmp_path, history):
    assert not history.settings.database.exists()

    assert choose(tmp_path, history).new is True
    assert not history.settings.database.exists()


@pytest.mark.parametrize(
    ("repo_args", "message"),
    [
        pytest.param({"fulls": ()}, "stanza main has no full backup", id="no-full"),
        pytest.param(
            {"fulls": ((DIFF, True, False),)}, "stanza main has no full backup", id="only-a-diff"
        ),
        pytest.param(
            {"fulls": ((NEWEST, False, False),)},
            f"full {NEWEST} was taken without archive-copy; "
            "take fulls with --archive-copy or archive-copy=y",
            id="no-archive-copy",
        ),
        pytest.param(
            {"cipher": True},
            "encrypted repositories are not supported in 0.7.3",
            id="encrypted",
        ),
    ],
)
def test_choose_refusals(tmp_path, history, repo_args, message):
    with pytest.raises(BackupError) as refused:
        choose(tmp_path, history, **repo_args)

    assert str(refused.value) == message


def test_choose_judges_only_the_newest_full_for_archive_copy(tmp_path, history):
    choice = choose(tmp_path, history, fulls=((OLDER, False, False), (NEWEST, True, False)))

    assert choice.full.label == NEWEST


def test_choose_refuses_a_cipher_before_anything_else(tmp_path, history):
    record_export(history, tmp_path, NEWEST)

    with pytest.raises(RepositoryError) as refused:
        choose(tmp_path, history, fulls=((NEWEST, False, False),), cipher=True)

    assert str(refused.value) == "encrypted repositories are not supported in 0.7.3"


def test_choose_refuses_a_missing_archive_copy_even_when_the_label_was_exported(
    tmp_path, history
):
    record_export(history, tmp_path, NEWEST)

    with pytest.raises(BackupError, match="without archive-copy"):
        choose(tmp_path, history, fulls=((NEWEST, False, False),))


# ---- preflight, for run --validate ----------------------------------------------------------

BINARY = "/usr/bin/pgbackrest"
NO_SUCH_FILE = FileNotFoundError(2, "No such file or directory", "pgbackrest")


def preflight_site(tmp_path, *, marker=True, repo_args=None, **overrides):
    """A repository and a first stage with an engine over them; returns (engine, stage)."""
    repo = make_repo(tmp_path, **(repo_args or {}))
    stage = tmp_path / "stage1"
    stage.mkdir()
    if marker:
        (stage / MARKER).write_text("")
    return make_engine(tmp_path, repo_path=repo, **overrides), stage


def scripted(info, version="2.58.0"):
    """A stand-in for ``subprocess.run`` that answers from fixed values, not from a repository.

    A string is what the command prints (for ``version``, the version number) and an exception
    is raised. Every command it receives is appended to ``run.calls``.
    """

    def run(cmd, **kwargs):
        run.calls.append(list(cmd))
        command = next(arg for arg in cmd[1:] if not arg.startswith("-"))
        answer = version if command == "version" else info
        if isinstance(answer, Exception):
            raise answer
        stdout = f"pgBackRest {answer}\n" if command == "version" else answer
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    run.calls = []
    return run


def preflight(engine, run, *, which=BINARY, euid=1000):
    with (
        patch(RUN, run),
        patch("shutil.which", return_value=which),
        patch("os.geteuid", return_value=euid),
    ):
        return engine.preflight()


def failures(lines):
    return [line for line in lines if line[0] == "FAIL"]


def test_preflight_passes_on_a_good_site(tmp_path):
    engine, stage = preflight_site(tmp_path)
    run = fake_pgbackrest(engine.repo_path)

    lines = preflight(engine, run)

    assert lines == [
        ("OK", f"Executable: {BINARY}"),
        ("OK", "pgBackRest 2.58.0"),
        ("OK", "not running as root"),
        ("OK", "stanza main is listed by pgbackrest info"),
        ("OK", "repository is not encrypted"),
        ("OK", f"{tmp_path / 'repo' / 'backup' / 'main'}: readable and searchable"),
        ("OK", f"{tmp_path / 'repo' / 'archive' / 'main'}: readable and searchable"),
        ("OK", f"{stage}: {MARKER} present"),
        (
            "OK",
            f"Output directory: {stage / 'pg'} does not exist; "
            f"{stage} is writable, so it can be created",
        ),
    ]
    assert run.calls == [["pgbackrest", "version"], engine.command("info", "--output=json")]
    assert not engine.out_dir.exists()


def test_preflight_passes_on_a_stanza_without_a_full(tmp_path):
    engine, _ = preflight_site(tmp_path, repo_args={"fulls": ()})

    lines = preflight(engine, fake_pgbackrest(engine.repo_path))

    assert failures(lines) == []
    assert ("OK", "stanza main is listed by pgbackrest info") in lines
    assert ("OK", "repository is not encrypted") in lines


def test_preflight_binary_not_on_path(tmp_path):
    engine, stage = preflight_site(tmp_path)

    lines = preflight(engine, scripted(NO_SUCH_FILE, NO_SUCH_FILE), which=None)

    assert failures(lines) == [
        ("FAIL", "Required executable 'pgbackrest' is missing or not executable in PATH."),
        ("FAIL", "pgbackrest version failed: No such file or directory"),
        ("FAIL", "pgbackrest info failed: No such file or directory"),
    ]
    assert ("OK", "not running as root") in lines
    assert ("OK", f"{stage}: {MARKER} present") in lines
    assert len(lines) == 6  # checks 5 and 6 are skipped, 3, 7 and 8 still run


def test_preflight_version_command_fails(tmp_path):
    engine, _ = preflight_site(tmp_path)
    failed = subprocess.CalledProcessError(
        1, ["pgbackrest", "version"], output="", stderr="P00   INFO: x\nP00  ERROR: [025]: bad\n"
    )
    info = json.dumps(info_json(engine.repo_path, "main"))

    lines = preflight(engine, scripted(info, version=failed))

    assert failures(lines) == [("FAIL", "pgbackrest version failed: P00  ERROR: [025]: bad")]
    assert ("OK", "stanza main is listed by pgbackrest info") in lines


@pytest.mark.parametrize("version", TESTED_VERSIONS)
def test_validate_tested_version_is_ok_without_a_note(tmp_path, version):
    engine, _ = preflight_site(tmp_path)

    lines = preflight(engine, fake_pgbackrest(engine.repo_path, version=version))

    assert lines[1] == ("OK", f"pgBackRest {version}")


def test_tested_versions():
    assert TESTED_VERSIONS == ("2.58.0", "2.60.0")


def test_validate_untested_version_is_ok_with_note(tmp_path):
    engine, _ = preflight_site(tmp_path)

    lines = preflight(engine, fake_pgbackrest(engine.repo_path, version="2.59.0"))

    assert lines[1] == (
        "OK",
        "pgBackRest 2.59.0 (not a tested version: 2.58.0, 2.60.0)",
    )
    assert failures(lines) == []


def test_preflight_refuses_root_and_runs_the_other_checks(tmp_path):
    engine, stage = preflight_site(tmp_path)

    lines = preflight(engine, fake_pgbackrest(engine.repo_path), euid=0)

    assert failures(lines) == [
        (
            "FAIL",
            "pgbackrest jobs do not run as root; run as the OS user that owns the repository",
        )
    ]
    assert ("OK", "stanza main is listed by pgbackrest info") in lines
    assert ("OK", f"{stage}: {MARKER} present") in lines


def test_preflight_info_command_fails(tmp_path):
    engine, _ = preflight_site(tmp_path)
    failed = subprocess.CalledProcessError(
        55, ["pgbackrest"], output="", stderr="P00  ERROR: [055]: unable to open file\n"
    )

    lines = preflight(engine, scripted(failed))

    assert failures(lines) == [
        ("FAIL", "pgbackrest info failed: P00  ERROR: [055]: unable to open file")
    ]


def test_preflight_info_output_is_not_a_stanza_list(tmp_path):
    engine, _ = preflight_site(tmp_path)

    lines = preflight(engine, scripted("not json"))

    assert failures(lines) == [
        ("FAIL", "pgbackrest info did not print a JSON list of stanzas")
    ]


def test_preflight_stanza_not_listed(tmp_path):
    engine, _ = preflight_site(tmp_path, stanza="other")
    info = json.dumps(info_json(engine.repo_path, "main"))

    lines = preflight(engine, scripted(info))

    assert failures(lines) == [("FAIL", "stanza 'other' is not in the pgbackrest info output")]
    assert not any("encrypted" in message or "readable" in message for _, message in lines)


def test_preflight_stanza_info_reports_missing(tmp_path):
    # Real pgBackRest lists an unknown stanza, with exit 0, as status 1.
    engine, _ = preflight_site(tmp_path, stanza="nosuch")

    lines = preflight(engine, scripted(json.dumps(missing_stanza_info("nosuch"))))

    assert failures(lines) == [
        ("FAIL", "stanza 'nosuch': pgbackrest info reports status 1: missing stanza path")
    ]
    assert not any("encrypted" in message or "readable" in message for _, message in lines)


def test_run_refuses_a_stanza_info_reports_missing(tmp_path):
    engine, _ = preflight_site(tmp_path, stanza="nosuch")
    history = History(HistorySettings(tmp_path / "h.sqlite3", tmp_path / "restores"))

    with (
        patch("bdbackup.postgres.pgbackrest.subprocess.run",
              side_effect=scripted(json.dumps(missing_stanza_info("nosuch")))),
        pytest.raises(BackupError, match="status 1: missing stanza path"),
    ):
        engine.choose(history, "pg-main")


def test_preflight_skips_encryption_and_directories_when_info_fails(tmp_path):
    # The repository is encrypted and its archive directory is gone: both would fail if checked.
    engine, stage = preflight_site(tmp_path, repo_args={"cipher": True})
    shutil.rmtree(engine.repo_path / "archive" / "main")
    failed = subprocess.CalledProcessError(55, ["pgbackrest"], output="", stderr="ERROR: x\n")

    lines = preflight(engine, scripted(failed))

    assert lines == [
        ("OK", f"Executable: {BINARY}"),
        ("OK", "pgBackRest 2.58.0"),
        ("OK", "not running as root"),
        ("FAIL", "pgbackrest info failed: ERROR: x"),
        ("OK", f"{stage}: {MARKER} present"),
        (
            "OK",
            f"Output directory: {stage / 'pg'} does not exist; "
            f"{stage} is writable, so it can be created",
        ),
    ]


def test_preflight_refuses_an_encrypted_repository_and_checks_the_directories(tmp_path):
    engine, _ = preflight_site(tmp_path, repo_args={"cipher": True})

    lines = preflight(engine, fake_pgbackrest(engine.repo_path))

    assert failures(lines) == [("FAIL", "encrypted repositories are not supported in 0.7.3")]
    assert ("OK", f"{tmp_path / 'repo' / 'backup' / 'main'}: readable and searchable") in lines


@pytest.mark.parametrize("damage", ["missing", "no-search", "no-read"])
@pytest.mark.parametrize("part", ["backup", "archive"])
def test_preflight_repository_directories(tmp_path, part, damage):
    engine, _ = preflight_site(tmp_path)
    directory = engine.repo_path / part / "main"
    info = json.dumps(info_json(engine.repo_path, "main"))  # read before the damage
    if damage == "missing":
        shutil.rmtree(directory)
    else:
        directory.chmod({"no-search": 0o644, "no-read": 0o311}[damage])

    try:
        lines = preflight(engine, scripted(info))
    finally:
        if directory.exists():
            directory.chmod(0o755)

    assert failures(lines) == [("FAIL", f"{directory}: not a readable and searchable directory")]


def test_preflight_marker_missing(tmp_path):
    engine, stage = preflight_site(tmp_path, marker=False)

    lines = preflight(engine, fake_pgbackrest(engine.repo_path))

    assert failures(lines) == [
        (
            "FAIL",
            f"{stage}: {MARKER} missing; run --initialize-structure for its gfs job",
        )
    ]


def test_preflight_output_directory_exists_and_is_writable(tmp_path):
    engine, stage = preflight_site(tmp_path)
    engine.out_dir.mkdir()

    lines = preflight(engine, fake_pgbackrest(engine.repo_path))

    assert lines[-1] == ("OK", f"Output directory: {stage / 'pg'} is writable")
    assert failures(lines) == []


def test_preflight_output_directory_nearest_existing_parent(tmp_path):
    engine, stage = preflight_site(tmp_path, out_dir=tmp_path / "stage1" / "a" / "b")
    (stage / "a").mkdir()

    lines = preflight(engine, fake_pgbackrest(engine.repo_path))

    assert lines[-1] == (
        "OK",
        f"Output directory: {stage / 'a' / 'b'} does not exist; "
        f"{stage / 'a'} is writable, so it can be created",
    )
    assert not engine.out_dir.exists()


def test_preflight_output_directory_not_writable(tmp_path):
    engine, stage = preflight_site(tmp_path)
    engine.out_dir.mkdir()
    engine.out_dir.chmod(0o555)

    try:
        lines = preflight(engine, fake_pgbackrest(engine.repo_path))
    finally:
        engine.out_dir.chmod(0o755)

    assert failures(lines) == [
        ("FAIL", f"Output directory: {stage / 'pg'} is not a writable directory")
    ]


def test_preflight_output_directory_parent_not_writable(tmp_path):
    engine, stage = preflight_site(tmp_path)
    stage.chmod(0o555)

    try:
        lines = preflight(engine, fake_pgbackrest(engine.repo_path))
    finally:
        stage.chmod(0o755)

    assert failures(lines) == [
        (
            "FAIL",
            f"Output directory: {stage / 'pg'} does not exist "
            f"and {stage} is not a writable directory",
        )
    ]


def test_preflight_output_directory_looks_no_further_up_than_the_first_stage(tmp_path):
    engine, stage = preflight_site(tmp_path)
    shutil.rmtree(stage)  # tmp_path is writable, but the share is not mounted

    lines = preflight(engine, fake_pgbackrest(engine.repo_path))

    assert failures(lines) == [
        ("FAIL", f"{stage}: {MARKER} missing; run --initialize-structure for its gfs job"),
        (
            "FAIL",
            f"Output directory: {stage / 'pg'} does not exist "
            f"and {stage} is not a writable directory",
        ),
    ]


def test_preflight_stage_that_cannot_be_searched_is_a_fail_not_an_exception(tmp_path):
    engine, stage = preflight_site(tmp_path)
    stage.chmod(0o000)

    try:
        lines = preflight(engine, fake_pgbackrest(engine.repo_path))
    finally:
        stage.chmod(0o755)

    marker, out_dir = failures(lines)
    assert marker[1].startswith(f"{stage}: ")
    assert MARKER in marker[1]
    assert out_dir == (
        "FAIL",
        f"Output directory: {stage / 'pg'} does not exist and {stage} is not a writable directory",
    )


# ---- run --validate -------------------------------------------------------------------------

VALIDATE_CONFIG = """[history]
database = "state/history.sqlite3"

[gfs-main]
type = "gfs"
[[gfs-main.stage]]
paths = ["STAGE1"]
period = "daily"
keep = "5d"

[pg]
type = "pgbackrest"
stanza = "main"
repo_path = "repo"
out_dir = "STAGE1/pg"
"""


def validate_site(tmp_path, *, marker=True, extra=""):
    repo = make_repo(tmp_path)
    stage = tmp_path / "STAGE1"
    stage.mkdir()
    if marker:
        (stage / MARKER).write_text("")
    config = tmp_path / "config.toml"
    config.write_text(VALIDATE_CONFIG + extra)
    return config, repo.resolve(), stage.resolve()


def validate(config, repo):
    with patch(RUN, fake_pgbackrest(repo)), patch("shutil.which", return_value=BINARY):
        return CliRunner().invoke(main, ["run", "-c", str(config), "-j", "pg", "--validate"])


def test_validate_command_reports_each_check(tmp_path):
    config, repo, stage = validate_site(tmp_path)

    result = validate(config, repo)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    for expected in (
        f"[OK] Executable: {BINARY}",
        "[OK] pgBackRest 2.58.0",
        "[OK] not running as root",
        "[OK] stanza main is listed by pgbackrest info",
        "[OK] repository is not encrypted",
        f"[OK] {repo / 'backup' / 'main'}: readable and searchable",
        f"[OK] {repo / 'archive' / 'main'}: readable and searchable",
        f"[OK] {stage}: {MARKER} present",
        f"[OK] Output directory: {stage / 'pg'} does not exist; "
        f"{stage} is writable, so it can be created",
    ):
        assert expected in lines, result.output
    assert "not implemented" not in result.output
    assert "Validation passed." in lines


def test_validate_command_fails_on_a_missing_marker(tmp_path):
    config, repo, stage = validate_site(tmp_path, marker=False)

    result = validate(config, repo)

    assert result.exit_code == 1, result.output
    assert (
        f"[FAIL] {stage}: {MARKER} missing; run --initialize-structure for its gfs job"
        in result.output.splitlines()
    )
    assert "Validation failed; see requirements above." in result.output


def test_validate_command_missing_binary_is_a_fail_line_not_a_traceback(tmp_path):
    config, _, _ = validate_site(tmp_path, extra='binary = "bdbackup-no-such-pgbackrest"\n')

    result = CliRunner().invoke(main, ["run", "-c", str(config), "-j", "pg", "--validate"])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    lines = result.output.splitlines()
    assert (
        "[FAIL] Required executable 'bdbackup-no-such-pgbackrest' is missing "
        "or not executable in PATH."
    ) in lines
    assert "[FAIL] pgbackrest version failed: No such file or directory" in lines
    assert "[FAIL] pgbackrest info failed: No such file or directory" in lines
    assert "Traceback" not in result.output
