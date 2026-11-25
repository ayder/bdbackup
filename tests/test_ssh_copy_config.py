"""Only eligible jobs may copy to a well-formed SSH destination."""

import json
import os
import subprocess

import pytest
from click.testing import CliRunner

from bdbackup.cli import main
from bdbackup.config import Config, build_backend
from bdbackup.engines import _reset_for_tests

HISTORY = '[history]\ndatabase = "state/history.sqlite3"\n'
FILE = '[daily]\ntype = "file"\nbackup_dst = "local/daily"\ntimestamp = true\n'
MYSQL = '[daily]\ntype = "mysqldump"\ndatabase = "one/two: β"\nout_dir = "dumps"\n'
PG = '[daily]\ntype = "pgbackrest"\nstanza = "main-1"\nrepo_path = "repo"\nout_dir = "stage/pg"\n'
GFS = '[rotation]\ntype = "gfs"\n[[rotation.stage]]\npaths = ["stage"]\nperiod = "daily"\n'
GFS += 'keep = "5d"\n'
DESTINATION = "ssh://archive@Vault.example:2222/remote/daily%20+@=,_~"
COPY = f'[daily.copy]\ndestination = "{DESTINATION}"\nmethod = "scp"\n'


@pytest.fixture(autouse=True)
def no_external_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "runtime-config"))
    _reset_for_tests()

    def unexpected(*args, **kwargs):
        pytest.fail(f"config load launched a process: {args}")

    monkeypatch.setattr(subprocess, "run", unexpected)
    yield
    _reset_for_tests()


def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def rejected(path, key, detail="", job="daily"):
    result = CliRunner().invoke(main, ["history", "--config", str(path)])
    assert result.exit_code == 2, "copy configuration rejection must exit 2"
    assert f"Job '{job}'" in result.output
    assert key in result.output
    assert detail in result.output


@pytest.mark.parametrize("job", [FILE, MYSQL, GFS + PG], ids=["file", "mysqldump", "pgbackrest"])
@pytest.mark.parametrize("method", ["rsync", "scp"])
def test_copy_settings_are_loaded_separately_from_backend(tmp_path, job, method):
    section = COPY.replace('"scp"', json.dumps(method))
    path = write_config(tmp_path, HISTORY + job + section)
    loaded = Config(path).get("daily")
    settings = loaded.copy
    assert settings is not None, "eligible job must retain the configured copy settings"
    assert settings.destination == DESTINATION
    assert settings.method == method
    assert settings.host == "Vault.example"
    assert settings.port == 2222
    assert settings.user == "archive"
    assert settings.path == "/remote/daily%20+@=,_~"
    assert settings.ssh_config is None
    assert settings.bwlimit is None
    assert "copy" not in loaded.params
    build_backend(loaded)


def test_relative_ssh_config_and_bwlimit(tmp_path):
    (tmp_path / "ssh files").mkdir()
    path = write_config(
        tmp_path, HISTORY + FILE + COPY + 'ssh_config = "ssh files/config"\nbwlimit = 13\n',
    )
    settings = Config(path).get("daily").copy
    assert settings is not None, "copy settings must retain optional values"
    assert (settings.ssh_config, settings.bwlimit) == (tmp_path / "ssh files/config", 13)


def test_lexical_ssh_config_keeps_a_symlink_spelling(tmp_path, monkeypatch):
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    path = write_config(tmp_path, HISTORY + FILE + COPY + 'ssh_config = "alias/config"\n')
    assert Config(path).get("daily").copy is not None, "copy config must load before lexical check"
    seen = []
    real_lstat = os.lstat

    def track(value, *args, **kwargs):
        if value == str(tmp_path / "alias"):
            seen.append(value)
        return real_lstat(value, *args, **kwargs)

    monkeypatch.setattr("os.lstat", track)
    settings = Config(path, lexical=True).get("daily").copy
    assert settings.ssh_config == tmp_path / "alias/config"
    assert seen == []


@pytest.mark.parametrize("url,user,host,port", [
    ("ssh://vault/dir", None, "vault", None),
    ("ssh://u@[2001:db8::1]:1/dir", "u", "2001:db8::1", 1),
    ("ssh://vault:65535/", None, "vault", 65535),
])
def test_copy_destination_optional_authority_parts(tmp_path, url, user, host, port):
    path = write_config(tmp_path, HISTORY + FILE + COPY.replace(DESTINATION, url))
    settings = Config(path).get("daily").copy
    assert settings is not None, "literal SSH destination must be accepted"
    assert (settings.user, settings.host, settings.port) == (user, host, port)


@pytest.mark.parametrize("url", [
    "sftp://vault/path", "ssh:/vault/path", "ssh:///path", "ssh://:22/path",
    "ssh://alice:secret@vault/path", "ssh://vault/path?q=1", "ssh://vault/path?",
    "ssh://vault/path#local", "ssh://vault/path#", "ssh://vault:0/path",
    "ssh://vault:65536/path", "ssh://vault:-1/path", "ssh://vault:abc/path",
    "ssh://vault:/path", "ssh://vault", "ssh://vault:relative",
    "ssh://vault/a/./b", "ssh://vault/a/../b", "ssh://vault/..",
    "ssh://vault/a b", "ssh://vault/a;echo", "ssh://vault/a'é", "ssh://vault/a\\b",
    "ssh://vault/a\nb", "ssh://-F/path", "ssh://bad name/path", "ssh://bad;name@vault/path",
    "ssh://[not-v6]/path",
])
def test_invalid_copy_destination_exits_2(tmp_path, url):
    copy = '[daily.copy]\nmethod = "rsync"\ndestination = ' + json.dumps(url, ensure_ascii=False)
    rejected(write_config(tmp_path, HISTORY + FILE + copy), "copy.destination")


@pytest.mark.parametrize("text,key", [
    ('copy = "scp"\n', "copy"),
    (COPY.replace('method = "scp"\n', ""), "copy.method"),
    (COPY.replace('"scp"', '"sftp"'), "copy.method"),
    (COPY.replace('"scp"', '["scp"]'), "copy.method"),
    (COPY.replace(f'destination = "{DESTINATION}"\n', ""), "copy.destination"),
    (COPY.replace(f'"{DESTINATION}"', "12"), "copy.destination"),
    (COPY + "identity_file = 1\n", "copy.identity_file"),
    (COPY + "ssh_config = 4\n", "copy.ssh_config"),
    (COPY + 'ssh_config = ""\n', "copy.ssh_config"),
    (COPY + "bwlimit = 0\n", "copy.bwlimit"),
    (COPY + "bwlimit = -1\n", "copy.bwlimit"),
    (COPY + "bwlimit = 1.5\n", "copy.bwlimit"),
    (COPY + "bwlimit = true\n", "copy.bwlimit"),
    (COPY + 'bwlimit = "10"\n', "copy.bwlimit"),
])
def test_bad_copy_table_keys_exit_2(tmp_path, text, key):
    rejected(write_config(tmp_path, HISTORY + FILE + text), key)


def test_copy_requires_history(tmp_path):
    rejected(write_config(tmp_path, FILE + COPY), "copy", "[history]")


@pytest.mark.parametrize("job,key", [
    (FILE.replace("timestamp = true\n", ""), "timestamp"),
    (FILE.replace("true", "false"), "timestamp"),
    (FILE.replace("true", '"yes"'), "timestamp"),
    (FILE.replace("local/daily", "local/archive space"), "backup_dst"),
    (FILE.replace("local/daily", "local/archive;bad"), "backup_dst"),
    (GFS + PG.replace("main-1", "main bad"), "stanza"),
    (GFS + PG.replace("main-1", "one/two"), "stanza"),
])
def test_copy_rejects_jobs_that_cannot_make_safe_unique_names(tmp_path, job, key):
    rejected(write_config(tmp_path, HISTORY + job + COPY), key, "copy")


@pytest.mark.parametrize("kind", ["gfs", "xtrabackup", "custom-copy-test", "mysqldump"])
def test_copy_rejects_unsupported_engines(tmp_path, kind):
    if kind in {"mysqldump", "custom-copy-test"}:
        plugins = tmp_path / "runtime-config/engines"
        plugins.mkdir(parents=True)
        (plugins / "foreign.py").write_text(
            "from bdbackup.engines import EngineInfo\n"
            f"ENGINE = EngineInfo({kind!r}, object, 'test engine')\n",
        )
    job = GFS.replace("rotation", "daily") if kind == "gfs" else f'[daily]\ntype = "{kind}"\n'
    rejected(write_config(tmp_path, HISTORY + job + COPY), "copy", "built-in")


def test_copy_destination_conflict_is_by_written_host_port_and_path(tmp_path):
    other = (FILE + COPY).replace("daily]", "second]").replace(
        "daily.copy]", "second.copy]",
    ).replace("archive@", "another-user@")
    path = write_config(tmp_path, HISTORY + FILE + COPY + other)
    rejected(path, "copy.destination", "daily", job="second")


@pytest.mark.parametrize("url", [
    DESTINATION.replace("Vault", "vault"),
    DESTINATION.replace(":2222", ":02222"),
    DESTINATION + "/",
])
def test_distinct_written_destinations_are_not_merged(tmp_path, url):
    other = (FILE + COPY).replace("daily]", "second]").replace(
        "daily.copy]", "second.copy]",
    ).replace(DESTINATION, url)
    path = write_config(tmp_path, HISTORY + FILE + COPY + other)
    config = Config(path)
    assert config.get("second").copy is not None, "distinct written destinations must load"
