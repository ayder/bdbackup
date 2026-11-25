"""PBM config and registry behavior through the same loader used by bdbackup."""

import json
import subprocess
from itertools import product
from pathlib import Path

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config, build_backend
from bdbackup.engines import list_engines
from bdbackup.mongodb.pbm import PBM

HISTORY = '[history]\ndatabase = "ledger.sqlite3"\nrestore_root = "restores"'
SETTINGS = {"storage_path": "pbm-storage", "out_dir": "backups"}
PREFERENCES = {
    "backup_type": ("logical", "physical", None),
    "binary": ("my-pbm", None),
}
VALID_SETTINGS = list(product(*PREFERENCES.values(), ("config-uri", "environment-uri")))


def job_table(name="mongo-main", params=None):
    params = SETTINGS if params is None else params
    body = "\n".join(f"{key} = {json.dumps(value)}" for key, value in params.items())
    return f"[{json.dumps(name)}]\ntype = \"pbm\"\n{body}"


def write_config(root, job=None, history=HISTORY, extra=""):
    config = root / "backup.toml"
    config.write_text("\n\n".join((history, job or job_table(), extra)))
    return config


def assert_usage(config, name, key, detail):
    result = CliRunner().invoke(main, ["history", "-c", str(config)])
    assert result.exit_code == 2, f"invalid PBM {key} must be a usage error: {result.output}"
    assert name in result.output and key in result.output, result.output
    assert detail in result.output, result.output


def test_pbm_is_listed_in_the_mongodb_engine_family():
    engine = list_engines().get("pbm")
    assert engine is not None and engine.family == "mongodb" and engine.backend is PBM
    assert engine.description, "the registry lists a description for the PBM engine"


@pytest.mark.parametrize(("backup_type", "binary", "uri_source"), VALID_SETTINGS)
def test_valid_pbm_keys_load(tmp_path, backup_type, binary, uri_source):
    mongodb_uri = (
        "mongodb://fixture-user:fixture-secret@localhost/" if uri_source == "config-uri" else None
    )
    options = {"backup_type": backup_type, "binary": binary, "mongodb_uri": mongodb_uri}
    params = SETTINGS | {key: value for key, value in options.items() if value is not None}
    path = write_config(tmp_path, job_table(params=params))
    job = Config(path).get("mongo-main")
    backend = build_backend(job)
    assert isinstance(backend, PBM)
    assert getattr(backend, "storage_path", None) == tmp_path.resolve() / "pbm-storage"
    assert backend.out_dir == tmp_path.resolve() / "backups"
    assert backend.backup_type == (backup_type or "logical")
    assert backend.binary == (binary or "pbm")
    assert backend.mongodb_uri == mongodb_uri


def test_pbm_requires_history(tmp_path):
    assert_usage(write_config(tmp_path, history=""), "mongo-main", "[history]", "require")


@pytest.mark.parametrize(("key", "value", "detail"), [
    ("storage_path", None, "required"),
    ("out_dir", None, "required"),
    ("backup_type", "incremental", "logical or physical"),
    ("backup_type", 3, "logical or physical"),
    ("timeout", 2, "unknown"),
    ("repo_path", 2, "unknown"),
    ("storage_path", 12, "path"),
    ("out_dir", "", "path"),
    ("binary", True, "string"),
    ("mongodb_uri", 123, "string"),
])
def test_pbm_rejects_invalid_keys(tmp_path, key, value, detail):
    params = SETTINGS | {key: value}
    if value is None:
        del params[key]
    assert_usage(write_config(tmp_path, job_table(params=params)), "mongo-main", key, detail)


@pytest.mark.parametrize("name", ["name:one", "name/one", "name one", "móngo"])
def test_pbm_rejects_unsafe_names(tmp_path, name):
    assert_usage(write_config(tmp_path, job_table(name)), name, "name", "shell-safe")


def test_pbm_accepts_the_safe_punctuation_in_job_names(tmp_path):
    name = "Mongo01._-+=@,%~"
    job = Config(write_config(tmp_path, job_table(name))).get(name)
    assert job.name == name
    assert job.params["storage_path"] == tmp_path.resolve() / "pbm-storage"


@pytest.mark.parametrize("key", [
    "out_dir", "[history] database", "[history] restore_root", "restore_root", "gfs stage path",
])
@pytest.mark.parametrize("layout", ["path-inside-storage", "storage-inside-path", "same-path"])
def test_pbm_keeps_storage_apart_from_every_writer(tmp_path, key, layout):
    storage, path = {
        "path-inside-storage": ("one", "one/two"),
        "storage-inside-path": ("one/two", "one"),
        "same-path": ("one", "one"),
    }[layout]
    params = SETTINGS | {"storage_path": storage}
    history, extra = HISTORY, ""
    if key.startswith("[history]"):
        field = key.removeprefix("[history] ")
        history = history.replace(
            f'{field} = "' + ("ledger.sqlite3" if field == "database" else "restores") + '"',
            f'{field} = "{path}"',
        )
    elif key == "gfs stage path":
        extra = (
            '[rotate]\ntype = "gfs"\n[[rotate.stage]]\nperiod = "daily"\nkeep = "7d"\n'
            f'paths = ["{path}"]'
        )
    else:
        params[key] = path
    config = write_config(tmp_path, job_table(params=params), history, extra)
    assert_usage(config, "mongo-main", key, "storage_path")


def test_pbm_jobs_can_share_storage(tmp_path):
    path = write_config(tmp_path, extra=job_table(
        "weekly", SETTINGS | {"backup_type": "physical", "out_dir": "weekly-output"},
    ))
    jobs = Config(path).jobs
    expected = tmp_path.resolve() / "pbm-storage"
    assert jobs["mongo-main"].params["storage_path"] == expected
    assert jobs["weekly"].params["storage_path"] == expected


def test_lexical_pbm_config_never_resolves_share_paths_or_starts_pbm(tmp_path, monkeypatch):
    config = write_config(tmp_path, job_table(params=SETTINGS | {"storage_path": "alias/../share"}))
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        assert path.name not in {"share", "backups", "restores"}, f"resolved share path {path}"
        return original_resolve(path, *args, **kwargs)

    def run(*args, **kwargs):
        pytest.fail("loading a PBM config must not start a process")

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(subprocess, "run", run)
    job = Config(config, lexical=True).get("mongo-main")
    assert job.params["storage_path"] == config.parent.resolve() / "share"
