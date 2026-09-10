"""Regressions for the release audit's security, data-loss and CLI failures."""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bdbackup.backends import BackupError, BackupResult
from bdbackup.cli import main
from bdbackup.config import Config, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.mysql.helpers import mysql_cnf_file
from bdbackup.retention import BackupFile, Policy
from bdbackup.templates import build_matcher
from bdbackup.utils import process_lock
from tests.conftest import write_checkpoints


def file_job(tmp_path, entries="hello\n", **kwargs):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "hello").write_text("must survive")
    template = tmp_path / "paths"
    template.write_text(entries.replace("\\n", "\n"))
    return FileBackup(tmp_path / "daily", template, chdir=source, **kwargs)


@pytest.mark.parametrize("member_name", ["../escaped", "/absolute-escape"])
def test_restore_rejects_escaping_regular_members(tmp_path, member_name):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        member = tarfile.TarInfo(member_name)
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(BackupError):
        FileBackup.restore_archive(archive, tmp_path / "restore")
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_restore_rejects_escaping_links_and_special_files(tmp_path, kind):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        member = tarfile.TarInfo("bad")
        member.type = kind
        member.linkname = "../outside"
        tar.addfile(member)
    with pytest.raises(BackupError):
        FileBackup.restore_archive(archive, tmp_path / "restore")


def test_restore_rejects_existing_destination_symlink(tmp_path):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        member = tarfile.TarInfo("linked/data")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "restore"
    dest.mkdir()
    (dest / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BackupError):
        FileBackup.restore_archive(archive, dest)
    assert not (outside / "data").exists()


@pytest.mark.parametrize("format", ["tar", "tar.gz"])
def test_roundtrip_preserves_directories_links_and_metadata(tmp_path, format):
    fb = file_job(tmp_path, ".\n", format=format)
    source = fb.chdir
    (source / "empty").mkdir(mode=0o750)
    os.utime(source / "empty", (1_600_000_000, 1_600_000_000))
    os.link(source / "hello", source / "hardlink")
    (source / "file-link").symlink_to("hello")
    (source / "dir-link").symlink_to("empty", target_is_directory=True)
    result = fb.backup()
    restored = tmp_path / "restore"
    restored.mkdir()
    cli = CliRunner().invoke(main, ["restore", str(result.path), "-d", str(restored)])
    assert cli.exit_code == 0, cli.output
    assert (restored / "hello").read_text() == "must survive"
    assert (restored / "hardlink").stat().st_ino == (restored / "hello").stat().st_ino
    assert (restored / "file-link").is_symlink()
    assert (restored / "dir-link").is_symlink()
    assert (restored / "empty").stat().st_mode & 0o777 == 0o750
    assert (restored / "empty").stat().st_mtime == 1_600_000_000


def test_follow_symlinks_keeps_alias_names_and_rejects_cycles(tmp_path):
    fb = file_job(tmp_path, ".\n", follow_symlinks=True)
    (fb.chdir / "alias").symlink_to("hello")
    result = fb.backup()
    fb.restore(tmp_path / "restore", result.path)
    assert not (tmp_path / "restore" / "alias").is_symlink()
    assert (tmp_path / "restore" / "alias").read_text() == "must survive"
    previous = result.path.read_bytes()
    (fb.chdir / "cycle").symlink_to(".", target_is_directory=True)
    with pytest.raises(BackupError, match="cycle"):
        fb.backup()
    assert result.path.read_bytes() == previous


@pytest.mark.parametrize("failure", ["missing", "read", "verify", "changed"])
def test_failed_replacement_preserves_previous_archive(tmp_path, failure):
    fb = file_job(tmp_path)
    result = fb.backup()
    original = result.path.read_bytes()
    if failure == "missing":
        fb.backup_paths.append(Path("missing"))
        context = pytest.raises(FileNotFoundError)
        with context:
            fb.backup()
    else:
        original_add = tarfile.TarFile.add

        def add(tar, path, **kwargs):
            if failure == "read":
                raise PermissionError("unreadable")
            original_add(tar, path, **kwargs)
            if failure == "changed":
                Path(path).write_text("changed while reading")

        if failure == "verify":
            with patch.object(fb, "verify", side_effect=BackupError("corrupt")):
                with pytest.raises(BackupError):
                    fb.backup()
        else:
            with patch.object(tarfile.TarFile, "add", add):
                with pytest.raises((PermissionError, BackupError)):
                    fb.backup()
    assert result.path.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))
    with process_lock(fb.lock_filename):
        pass  # Failure released the writer lock.


def test_unreadable_directory_fails_backup(tmp_path):
    fb = file_job(tmp_path, ".\n")
    with patch("bdbackup.filebackup.os.scandir", side_effect=PermissionError("unreadable")):
        with pytest.raises(PermissionError):
            fb.backup()
    assert not fb.tar_filename.exists()


def test_dry_run_does_not_create_output_directories(tmp_path):
    fb = file_job(tmp_path)
    fb = FileBackup(tmp_path / "not-created" / "daily", fb.template_filename, chdir=fb.chdir)
    assert fb.backup(dry_run=True).success
    assert not (tmp_path / "not-created").exists()


def test_destination_inside_source_is_never_archived(tmp_path):
    fb = file_job(tmp_path, ".\n")
    fb = FileBackup(fb.chdir / "daily", fb.template_filename, chdir=fb.chdir)
    result = fb.backup()
    result = fb.backup()
    with tarfile.open(result.path) as tar:
        assert tar.getnames() == [".", "hello"]


def test_excluded_directory_contents_and_negation(tmp_path):
    fb = file_job(tmp_path, ".\n", exclude_pattern=["node_modules"])
    (fb.chdir / "node_modules").mkdir()
    (fb.chdir / "node_modules" / "secret").write_text("excluded")
    with tarfile.open(fb.backup().path) as tar:
        assert "node_modules/secret" not in tar.getnames()
    fb.exclude_patterns = []
    fb._template_matcher = build_matcher(["node_modules/", "!node_modules/keep.txt"])
    (fb.chdir / "node_modules" / "keep.txt").write_text("keep")
    with tarfile.open(fb.backup().path) as tar:
        assert "node_modules/secret" not in tar.getnames()
        assert "node_modules/keep.txt" in tar.getnames()


def test_zstd_has_explicit_python_requirement(tmp_path):
    if sys.version_info < (3, 14):
        with pytest.raises(ValueError, match="Python 3.14"):
            file_job(tmp_path, format="tar.zst")
    else:
        fb = file_job(tmp_path, format="tar.zst")
        fb.restore(tmp_path / "restored", fb.backup().path)
        assert (tmp_path / "restored" / "hello").read_text() == "must survive"


def test_cli_process_returns_failure_for_missing_input(tmp_path):
    fb = file_job(tmp_path, "missing\n")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "bdbackup.cli",
            "file",
            "--template",
            str(fb.template_filename),
            "-c",
            str(fb.chdir),
            "-d",
            str(fb.backup_dst),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not fb.tar_filename.exists()


def test_cli_lock_contention_is_exit_three(tmp_path):
    fb = file_job(tmp_path)
    with process_lock(fb.lock_filename):
        result = CliRunner().invoke(
            main,
            [
                "file",
                "--template",
                str(fb.template_filename),
                "-c",
                str(fb.chdir),
                "-d",
                str(fb.backup_dst),
            ],
        )
    assert result.exit_code == 3


def test_password_prompt_and_parallel_database_dispatch(tmp_path):
    with patch("bdbackup.cli.MySQLBackup") as backend:
        backend.return_value.backup_all.return_value = [BackupResult(tmp_path / "dump")]
        result = CliRunner().invoke(
            main,
            ["mysqldump", "--database", "db1,db2", "--jobs", "4", "-p"],
            input="synthetic-secret\n",
        )
    assert result.exit_code == 0, result.output
    assert "synthetic-secret" not in result.output
    assert backend.call_args.kwargs["password"] == "synthetic-secret"  # noqa: S105
    backend.return_value.backup_all.assert_called_once_with(["db1", "db2"])
    backend.return_value.verify.assert_called_once()


def test_full_dump_retains_safe_and_custom_options(tmp_path):
    with patch("bdbackup.cli.MySQLBackup") as backend:
        backend.return_value.backup.return_value = BackupResult(tmp_path / "dump")
        result = CliRunner().invoke(main, ["mysqldump", "--full", "--options=--hex-blob"])
    assert result.exit_code == 0
    options = backend.call_args.kwargs["options"]
    mb = MySQLBackup(options=options)
    assert "--all-databases" in mb.options
    assert "--databases" not in mb.options
    assert {"--single-transaction", "--routines", "--events", "--hex-blob"} <= set(mb.options)


@pytest.mark.parametrize(
    ("value", "serialized"),
    [
        ("hash#value", '"hash#value"'),
        ('quote"value', '"quote\\"value"'),
        ("back\\slash", '"back\\\\slash"'),
        (" spaced ", '" spaced "'),
        ("", '""'),
    ],
)
def test_credentials_are_quoted_and_escaped(value, serialized):
    with mysql_cnf_file(user="backup", password=value) as path:
        assert f"password={serialized}\n" in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
    assert not path.exists()


@pytest.mark.parametrize("value", ["line\nbreak", "tab\there", "nul\0here"])
def test_credentials_reject_control_characters(value):
    with pytest.raises(ValueError, match="control"):
        with mysql_cnf_file(user="backup", password=value):
            pass


@pytest.mark.parametrize("kind", ["empty", "truncated", "crc"])
def test_dump_verification_rejects_invalid_stream(tmp_path, kind):
    path = tmp_path / "dump.gz"
    data = gzip.compress(b"" if kind == "empty" else b"SQL content")
    if kind == "truncated":
        data = data[:-4]
    if kind == "crc":
        data = data[:-8] + b"\0\0\0\0" + data[-4:]
    path.write_bytes(data)
    with pytest.raises(BackupError):
        MySQLBackup().verify(BackupResult(path))


def test_dump_separates_stderr_and_publishes_only_after_verification(tmp_path, monkeypatch):
    binary = tmp_path / "mysqldump"
    binary.write_text(
        '#!/bin/sh\nprintf "CREATE TABLE test (id INT);\\n"\nprintf "synthetic warning\\n" >&2\n'
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    backend = MySQLBackup(tmp_path / "dumps")
    first = backend.backup("test")
    assert b"warning" not in gzip.decompress(first.path.read_bytes())
    second = backend.backup("test")
    assert first.path != second.path
    with patch.object(backend, "verify", side_effect=BackupError("corrupt")):
        with pytest.raises(BackupError):
            backend.backup("test")
    assert sorted(backend.out_dir.iterdir()) == sorted([first.path, second.path])
    binary.write_text('#!/bin/sh\nprintf "failure" >&2\nexit 7\n')
    with pytest.raises(BackupError, match="failure"):
        backend.backup("test")
    assert sorted(backend.out_dir.iterdir()) == sorted([first.path, second.path])


def test_config_expands_home_and_resolves_relative_paths(tmp_path, monkeypatch):
    original_expanduser = os.path.expanduser
    with patch(
        "os.path.expanduser",
        side_effect=lambda value: str(tmp_path) if value == "~" else original_expanduser(value),
    ):
        file_job(tmp_path)
        cfg = tmp_path / "jobs.toml"
        cfg.write_text(
            '[files]\ntype="file"\nbackup_dst="daily"\n'
            'template_filename="~/paths"\nchdir="source"\n'
        )
        job = Config(cfg).get("files")
        assert build_backend(job).backup().success


def test_metadata_module_uses_standard_logger():
    from bdbackup.dboperations import MySQLOps

    with patch.object(MySQLOps, "_connect"):
        ops = MySQLOps()
    assert isinstance(ops.logger, logging.Logger)


def test_retention_keeps_intermediate_dependencies():
    full = BackupFile("full", "full", datetime(2026, 7, 1), 1, "name")
    old = BackupFile("old", "incr", datetime(2026, 7, 2), 1, "name")
    recent = BackupFile("recent", "incr", datetime(2026, 7, 29), 1, "name")
    Policy().apply_to_incrementals([full], [old, recent], datetime(2026, 7, 29).date())
    assert full.keep and old.keep and recent.keep


def test_unverified_physical_backup_cannot_prune_good_backup(tmp_path, physical_runner):
    xb = XtraBackup(tmp_path / "backups")
    with patch("subprocess.run", side_effect=physical_runner):
        with patch.object(xb, "_utc_now", return_value=datetime.now(UTC) - timedelta(days=10)):
            previous = xb.full_backup()
    with patch.object(xb, "_run", return_value=None):
        with pytest.raises(BackupError):
            xb.full_backup()
    assert previous.path.exists()
    assert not list(xb.backup_root.rglob("*.tmp"))


def test_physical_chain_uses_dependencies_across_midnight_and_new_full(tmp_path, physical_runner):
    xb = XtraBackup(tmp_path / "backups")
    commands = []

    def record(cmd):
        commands.append(cmd)
        physical_runner(cmd)

    first_day = datetime(2026, 7, 29, 12, tzinfo=UTC)
    with patch.object(xb, "_run", side_effect=record):
        with patch.object(xb, "_utc_now", return_value=first_day):
            full = xb.full_backup()
            inc1 = xb.incremental_backup()
        with patch.object(xb, "_utc_now", return_value=first_day + timedelta(hours=13)):
            inc2 = xb.incremental_backup()
            inc3 = xb.incremental_backup()
            replacement = xb.full_backup()
            new_inc = xb.incremental_backup()
    assert inc2.path != inc3.path
    meta = json.loads((inc3.path / xb.METADATA).read_text())
    assert (xb.backup_root / meta["parent"]) == inc2.path
    assert (xb.backup_root / meta["full"]) == full.path
    assert json.loads((new_inc.path / xb.METADATA).read_text())["parent"] == str(
        replacement.path.relative_to(xb.backup_root)
    )
    assert (full.path.parent / "Full_Latest").exists()
    assert inc1.path.exists()


def test_physical_chain_rejects_missing_prerequisite_and_ignores_temporary(
    tmp_path, physical_runner
):
    xb = XtraBackup(tmp_path / "backups")
    with patch("subprocess.run", side_effect=physical_runner):
        full = xb.full_backup()
        inc1 = xb.incremental_backup()
        inc2 = xb.incremental_backup()
        write_checkpoints(inc1.path.parent / "999999.tmp", 100, 99999)
        assert xb._chain(full.path) == [inc1.path, inc2.path]
        (inc1.path / xb.METADATA).unlink()
        with pytest.raises(BackupError, match="metadata"):
            xb.incremental_backup()


def test_prune_and_backup_share_lock(tmp_path, physical_runner):
    xb = XtraBackup(tmp_path / "backups")
    with patch("subprocess.run", side_effect=physical_runner):
        full = xb.full_backup()
    with process_lock(xb.lock_path):
        with pytest.raises(BlockingIOError):
            xb.prune()
    assert full.path.exists()


@pytest.mark.parametrize("binary", ["xtrabackup", "mariabackup"])
def test_prepare_copies_decompresses_and_merges_chain(tmp_path, physical_runner, binary):
    xb = XtraBackup(tmp_path / "backups", binary=binary)
    with patch("subprocess.run", side_effect=physical_runner):
        full = xb.full_backup()
        inc1 = xb.incremental_backup()
        inc2 = xb.incremental_backup()
    (full.path / "data.ibd.zst").write_bytes(b"compressed fixture")
    original = (full.path / "xtrabackup_checkpoints").read_bytes()
    commands = []

    def prepare_command(cmd):
        commands.append(cmd)
        assert cmd[1].startswith("--defaults-extra-file=")
        target = Path(next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--target-dir=")))
        assert target != full.path
        if "--prepare" in cmd:
            write_checkpoints(target, end=300, kind="full-prepared")

    dest = tmp_path / "recovery"
    with patch.object(xb, "_run", side_effect=prepare_command):
        assert xb.prepare(inc2.path, dest) == dest
    prepares = [cmd for cmd in commands if "--prepare" in cmd]
    assert "--decompress" in commands[0]
    assert len(prepares) == 3
    assert ("--apply-log-only" in prepares[0]) == (binary == "xtrabackup")
    assert ("--apply-log-only" in prepares[1]) == (binary == "xtrabackup")
    assert "--apply-log-only" not in prepares[-1]
    assert any(arg.startswith("--incremental-dir=") for arg in prepares[-1])
    assert (full.path / "xtrabackup_checkpoints").read_bytes() == original
    assert inc1.path.exists() and inc2.path.exists()
    assert not (dest / xb.METADATA).exists()


def test_failed_prepare_does_not_publish_or_modify_backups(tmp_path, physical_runner):
    xb = XtraBackup(tmp_path / "backups")
    with patch("subprocess.run", side_effect=physical_runner):
        full = xb.full_backup()
    original = (full.path / "xtrabackup_checkpoints").read_bytes()
    with patch.object(xb, "_run", side_effect=BackupError("prepare failed")):
        with pytest.raises(BackupError):
            xb.prepare(full.path, tmp_path / "recovery")
    assert not (tmp_path / "recovery").exists()
    assert (full.path / "xtrabackup_checkpoints").read_bytes() == original


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v9.9.9", ""])
def test_release_gate_rejects_branches_and_mismatched_tags(ref):
    from scripts.check_release import check_release

    with pytest.raises(SystemExit, match="matching pyproject.toml"):
        check_release(ref)


def test_release_gate_accepts_matching_version():
    from bdbackup import __version__
    from scripts.check_release import check_release

    check_release(f"refs/tags/v{__version__}")


def test_release_gate_uses_only_pyproject(tmp_path):
    from scripts.check_release import check_release

    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3rc1"\n')
    check_release("refs/tags/v1.2.3rc1", root=tmp_path)
    with pytest.raises(SystemExit, match="matching pyproject.toml"):
        check_release("refs/tags/v1.2.3", root=tmp_path)


def test_run_all_continues_after_constructor_failure(tmp_path):
    file_job(tmp_path)
    config = tmp_path / "jobs.toml"
    config.write_text(
        '[bad]\ntype="file"\nbackup_dst="bad"\ntemplate_filename="missing"\n'
        '[good]\ntype="file"\nbackup_dst="good"\ntemplate_filename="paths"\nchdir="source"\n'
    )
    result = CliRunner().invoke(main, ["run", "--all", "--config", str(config)])
    assert result.exit_code == 1
    assert (tmp_path / "good.tar").exists()
    assert not (tmp_path / "bad.tar").exists()


def test_retention_defers_deletion_when_incremental_is_still_writing(tmp_path):
    from bdbackup.retention import run_job
    from tests.test_retention import mkfile

    fulls = tmp_path / "fulls"
    incrs = tmp_path / "incrs"
    fulls.mkdir()
    incrs.mkdir()
    now = datetime(2026, 7, 29)
    for day in (1, 2, 3):
        mkfile(fulls, f"full_202607{day:02}_000000.mbi", datetime(2026, 7, day))
    mkfile(incrs, "incr_20260729_000000.mbi", now - timedelta(minutes=5))
    rc = run_job(full_dir=fulls, incr_dir=incrs, now=now, apply_changes=True)
    assert rc == 3
    assert len(list(fulls.iterdir())) == 3


def test_prepare_rejects_success_without_prepared_checkpoints(tmp_path, physical_runner):
    xb = XtraBackup(tmp_path / "backups")
    with patch("subprocess.run", side_effect=physical_runner):
        full = xb.full_backup()
    with patch.object(xb, "_run", return_value=None):
        with pytest.raises(BackupError, match="recovery point"):
            xb.prepare(full.path, tmp_path / "recovery")
    assert not (tmp_path / "recovery").exists()


def test_modern_mariadb_checkpoint_filename(tmp_path):
    write_checkpoints(tmp_path)
    (tmp_path / "xtrabackup_checkpoints").rename(tmp_path / "mariadb_backup_checkpoints")
    xb = XtraBackup(tmp_path.parent, binary="mariadb-backup")
    assert xb.verify(BackupResult(tmp_path)).success
    assert xb._valid_base(tmp_path)


def test_file_backup_does_not_follow_alias_to_its_previous_archive(tmp_path):
    fb = file_job(tmp_path, ".\n", follow_symlinks=True)
    result = fb.backup()
    (fb.chdir / "previous").symlink_to(result.path)
    with tarfile.open(fb.backup().path) as tar:
        assert "previous" not in tar.getnames()


def test_physical_process_failure_preserves_diagnostics(tmp_path):
    xb = XtraBackup(tmp_path)
    with pytest.raises(BackupError, match="synthetic process failure"):
        xb._run(
            [
                sys.executable,
                "-c",
                "import sys; print('synthetic process failure', file=sys.stderr); sys.exit(7)",
            ]
        )


def test_relative_template_names_without_chdir_remain_relative(tmp_path, monkeypatch):
    fb = file_job(tmp_path)
    monkeypatch.chdir(fb.chdir)
    default_job = FileBackup(tmp_path / "relative", fb.template_filename)
    with tarfile.open(default_job.backup().path) as archive:
        assert archive.getnames() == ["hello"]
