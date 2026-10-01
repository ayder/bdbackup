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


def test_retention_zero_disables_engine_deletion(tmp_path, old_chain, physical_runner):
    with patch("subprocess.run", side_effect=physical_runner):
        result = invoke("run", "-c", xtrabackup_config(tmp_path, 0), "-j", "xb")
    assert result.exit_code == 0, result.output
    assert old_chain.is_dir()


def test_retention_days_setting_accepts_zero_not_negative(tmp_path):
    zero = invoke("run", "--validate", "--config", xtrabackup_config(tmp_path, 0))
    assert "retention_days must be" not in zero.output, zero.output
    negative = invoke("run", "--validate", "--config", xtrabackup_config(tmp_path, -1))
    assert "retention_days must be an integer >= 0" in negative.output, negative.output


@pytest.fixture
def file_config(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/hello").write_text("recover me")
    (tmp_path / "paths").write_text("hello\n")
    path = tmp_path / "config.toml"
    path.write_text(
        '[history]\ndatabase="state/history.sqlite3"\nrestore_root="recovery"\n'
        '[daily]\ntype="file"\ntemplate_filename="paths"\n'
        'chdir="source"\nbackup_dst="archives/daily"\nformat="tar.gz"\ntimestamp=true\n'
    )
    return path


def file_records(config):
    from bdbackup.config import Config
    from bdbackup.history import History

    return History(Config(config).history).records()


def test_file_timestamp_writes_one_archive_per_run(file_config):
    from bdbackup.filebackup import FileBackup

    root = file_config.parent
    stamps = ["2026-09-29-020000", "2026-09-30-020000"]
    with patch.object(FileBackup, "_stamp", side_effect=stamps):
        for _ in stamps:
            result = invoke("run", "-c", file_config, "-j", "daily")
            assert result.exit_code == 0, result.output
    names = [f"daily-{stamp}.tar.gz" for stamp in stamps]
    for name in names:
        assert (root / "archives" / name).is_file(), f"missing {name}"
    assert not (root / "archives/daily.tar.gz").exists()
    records = file_records(file_config)
    assert all(record.available for record in records)
    assert sorted(record.path for record in records) == sorted(
        str((root / "archives" / name).resolve()) for name in names
    )


def test_file_timestamp_never_overwrites(file_config):
    import hashlib

    from bdbackup.filebackup import FileBackup

    archives = file_config.parent / "archives"

    def digests():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in archives.iterdir()
                if p.suffix != ".lock"}

    with patch.object(FileBackup, "_stamp", return_value="2026-09-29-020000"):
        assert invoke("run", "-c", file_config, "-j", "daily").exit_code == 0
        before = digests()
        (file_config.parent / "source/hello").write_text("new content")
        result = invoke("run", "-c", file_config, "-j", "daily")
    assert result.exit_code == 1, result.output
    assert digests() == before
    assert not list(archives.glob("*.tmp"))
    assert file_records(file_config)[0].status == "failed"


def test_timestamp_setting_must_be_boolean(file_config):
    file_config.write_text(file_config.read_text().replace("timestamp=true", 'timestamp="yes"'))
    result = invoke("run", "--validate", "--config", file_config)
    assert "timestamp must be true or false" in result.output, result.output
