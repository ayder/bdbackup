"""The ``[metrics]`` section: where the textfile goes and how long a metrics run may wait."""

from __future__ import annotations

import pytest

from bdbackup.config import Config, ConfigError
from bdbackup.metrics.settings import MetricsSettings

HISTORY = '[history]\ndatabase = "state/history.sqlite3"\n'
JOB = (
    '[daily]\ntype = "file"\ntemplate_filename = "paths"\nchdir = "source"\n'
    'backup_dst = "archives/daily"\nformat = "tar.gz"\n'
)


def load(tmp_path, text: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return Config(path)


def loaded(tmp_path, text: str) -> Config:
    """Load a configuration that must be accepted; a refusal is a failed assertion."""
    try:
        return load(tmp_path, text)
    except ConfigError as exc:
        raise AssertionError(f"a valid configuration was refused: {exc}") from exc


def test_the_metrics_section_is_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path.parent)  # a relative path is the config's, not the process's

    config = loaded(tmp_path, HISTORY + '[metrics]\npath = "prom/bdbackup.prom"\n' + JOB)

    expected = (tmp_path / "prom" / "bdbackup.prom").resolve()
    assert config.metrics == MetricsSettings(path=expected, timeout_seconds=60)


def test_an_absolute_path_is_kept(tmp_path):
    target = tmp_path / "elsewhere" / "bdbackup.prom"

    config = loaded(tmp_path, HISTORY + f'[metrics]\npath = "{target}"\n')

    assert config.metrics.path == target.resolve()


def test_a_home_directory_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = loaded(tmp_path, HISTORY + '[metrics]\npath = "~/metrics/bdbackup.prom"\n')

    assert config.metrics.path == (tmp_path / "home" / "metrics" / "bdbackup.prom").resolve()


def test_the_section_may_come_before_history(tmp_path):
    config = loaded(tmp_path, '[metrics]\npath = "m.prom"\n' + HISTORY)

    assert config.metrics.path == (tmp_path / "m.prom").resolve()


@pytest.mark.parametrize("timeout", [1, 5, 600])
def test_timeout_seconds_is_a_positive_whole_number(tmp_path, timeout):
    config = loaded(
        tmp_path, HISTORY + f'[metrics]\npath = "m.prom"\ntimeout_seconds = {timeout}\n'
    )

    assert config.metrics.timeout_seconds == timeout


@pytest.mark.parametrize("timeout", ["0", "-1", "1.5", '"60"', "true", "[60]"])
def test_a_timeout_that_is_not_a_positive_whole_number_is_refused(tmp_path, timeout):
    text = HISTORY + f'[metrics]\npath = "m.prom"\ntimeout_seconds = {timeout}\n'

    with pytest.raises(ConfigError, match="timeout_seconds must be a positive whole number"):
        load(tmp_path, text)


def test_path_is_required(tmp_path):
    with pytest.raises(ConfigError, match=r"\[metrics\] path is required"):
        load(tmp_path, HISTORY + "[metrics]\ntimeout_seconds = 30\n")


@pytest.mark.parametrize("path", ['""', '"   "', "5", '"bdbackup.txt"', '"bdbackup"', '"dir/"'])
def test_path_must_be_a_nonempty_string_ending_in_prom(tmp_path, path):
    with pytest.raises(ConfigError, match=r"path must be a nonempty filesystem path ending in"):
        load(tmp_path, HISTORY + f"[metrics]\npath = {path}\n")


@pytest.mark.parametrize(
    "text",
    [
        HISTORY + '[metrics]\npath = "m.prom"\nretention = 3\n',
        'metrics = "m.prom"\n' + HISTORY,  # before the first table, so a key of the top level
    ],
    ids=["unknown-key", "not-a-table"],
)
def test_other_keys_are_refused(tmp_path, text):
    with pytest.raises(ConfigError, match=r"\[metrics\] accepts only path and timeout_seconds"):
        load(tmp_path, text)


def test_metrics_requires_history(tmp_path):
    with pytest.raises(ConfigError, match=r"\[metrics\] requires a \[history\] section"):
        load(tmp_path, '[metrics]\npath = "m.prom"\n' + JOB)


def test_metrics_is_no_longer_read_as_a_job(tmp_path):
    config = loaded(tmp_path, HISTORY + JOB + '[metrics]\npath = "m.prom"\n')

    assert list(config.jobs) == ["daily"]


def test_a_job_named_metrics_is_now_a_configuration_error(tmp_path):
    text = HISTORY + '[metrics]\ntype = "file"\ntemplate_filename = "paths"\n'

    with pytest.raises(ConfigError, match=r"\[metrics\] accepts only path and timeout_seconds"):
        load(tmp_path, text)


def test_a_configuration_without_the_section_has_no_metrics_and_the_same_jobs(tmp_path):
    config = loaded(tmp_path, HISTORY + JOB)

    assert config.metrics is None
    assert list(config.jobs) == ["daily"]
    assert config.history is not None
