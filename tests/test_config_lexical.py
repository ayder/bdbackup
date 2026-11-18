"""``Config(path, lexical=True)``: the mode ``bdbackup metrics`` reads the config in (D8).

Share paths are normalised, never resolved, so loading never touches a share that may hang.
Every other command keeps resolving them.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bdbackup.config import Config
from bdbackup.engines import _reset_for_tests

CONFIG = """[history]
database = "state/history.sqlite3"
restore_root = "{share}/restores"

[metrics]
path = "metrics/bdbackup.prom"

[gfs-main]
type = "gfs"
[[gfs-main.stage]]
paths = ["{share}/stage1"]
period = "daily"
keep = "5d"
[[gfs-main.stage]]
paths = ["{share}/stage2"]
period = "daily"
keep = "20d"

[pg]
type = "pgbackrest"
stanza = "main"
repo_path = "{share}/repo"
out_dir = "{share}/stage1/pg"
restore_root = "{share}/pg-restores"
"""


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("BDBACKUP_CONFIG_DIR", str(tmp_path / "cfg"))
    _reset_for_tests()
    yield
    _reset_for_tests()


def write_config(tmp_path, share="share", text=CONFIG) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text.format(share=share))
    return path


def recorded(root: Path, load):
    """The filesystem calls ``load()`` makes on a path under ``root``."""
    prefix = str(root)
    calls = []

    def watching(owner, name):
        real = getattr(owner, name)

        def wrapper(path, *args, **kwargs):
            if isinstance(path, str | bytes | os.PathLike) and os.fsdecode(path).startswith(prefix):
                calls.append((name, os.fsdecode(path)))
            return real(path, *args, **kwargs)

        return wrapper

    patches = [patch.object(os, name, watching(os, name))
               for name in ("stat", "lstat", "readlink", "scandir", "listdir")]
    patches.append(patch.object(os.path, "realpath", watching(os.path, "realpath")))
    for p in patches:
        p.start()
    try:
        load()
    finally:
        for p in patches:
            p.stop()
    return calls


def test_a_lexical_load_makes_no_filesystem_call_under_the_share(tmp_path):
    share = tmp_path / "share"
    for name in ("stage1", "stage2", "repo", "stage1/pg"):
        (share / name).mkdir(parents=True)
    path = write_config(tmp_path, share=share)

    calls = recorded(share, lambda: Config(path, lexical=True))

    assert calls == []


def test_a_normal_load_does_look_at_the_share(tmp_path):
    share = tmp_path / "share"
    for name in ("stage1", "stage2", "repo", "stage1/pg"):
        (share / name).mkdir(parents=True)
    path = write_config(tmp_path, share=share)

    calls = recorded(share, lambda: Config(path))

    assert calls  # so the recording above can see a call when there is one


def loaded(tmp_path, **kwargs):
    return Config(write_config(tmp_path, share=tmp_path / "share"), **kwargs)


def test_a_lexical_load_reads_what_a_normal_load_reads_when_no_path_is_a_link(tmp_path):
    normal = loaded(tmp_path)
    lexical = loaded(tmp_path, lexical=True)

    assert lexical.metrics == normal.metrics
    assert lexical.history == normal.history
    assert sorted(lexical.jobs) == sorted(normal.jobs) == ["gfs-main", "pg"]
    for name in lexical.jobs:
        assert lexical.jobs[name].params == normal.jobs[name].params
        assert lexical.jobs[name].restore_root == normal.jobs[name].restore_root
    assert lexical.get("pg").params["first_stage"] == tmp_path / "share" / "stage1"


def test_a_lexical_load_keeps_a_path_written_through_a_link_and_a_normal_load_resolves_it(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    path = write_config(tmp_path, share="link")

    normal = Config(path)
    lexical = Config(path, lexical=True)

    assert normal.get("gfs-main").params["stage"][0].paths == (tmp_path / "real" / "stage1",)
    assert lexical.get("gfs-main").params["stage"][0].paths == (tmp_path / "link" / "stage1",)
    assert lexical.get("pg").params["first_stage"] == tmp_path / "link" / "stage1"
    assert lexical.history.restore_root == tmp_path / "link" / "restores"
    assert lexical.get("pg").restore_root == tmp_path / "link" / "pg-restores"


def test_a_lexical_load_normalises_dots_and_relative_segments(tmp_path):
    path = write_config(tmp_path, share="share/./sub/..")

    lexical = Config(path, lexical=True)

    assert lexical.get("gfs-main").params["stage"][1].paths == (tmp_path / "share" / "stage2",)
    assert lexical.get("pg").params["repo_path"] == tmp_path / "share" / "repo"
    assert lexical.get("pg").params["out_dir"] == tmp_path / "share" / "stage1" / "pg"
    assert lexical.history.database == (tmp_path / "state" / "history.sqlite3").resolve()


def test_a_lexical_load_applies_the_same_rules(tmp_path):
    from bdbackup.config import ConfigError

    broken = CONFIG.replace('out_dir = "{share}/stage1/pg"', 'out_dir = "{share}/elsewhere"')
    path = write_config(tmp_path, share="share", text=broken)

    with pytest.raises(ConfigError, match="out_dir must be inside the first stage"):
        Config(path, lexical=True)
