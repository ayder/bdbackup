"""Command-line interface for bdbackup."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import click
from click.core import ParameterSource

from bdbackup import __version__
from bdbackup.backends import BackupError, setup_logging
from bdbackup.config import Config, ConfigError, Job, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.gfs import runner as gfs_runner
from bdbackup.gfs import store as gfs_store
from bdbackup.history import History
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.recovery import backup_restore_info, restore_record, suggested_destination
from bdbackup.templates import TemplateError
from bdbackup.validation import validate_config

# Exit-code contract: 0 ok, 1 backup/verify failure, 2 usage (Click's usage error), 3 locked.
EXIT_OK = 0
EXIT_BACKUP_FAILED = 1
EXIT_LOCKED = 3


def config_option(**kwargs):
    return click.option(
        "-c", "--config", "config_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        help="TOML configuration file with job definitions.", **kwargs,
    )


@click.group(name="bdbackup", no_args_is_help=True)
@click.version_option(version=__version__, prog_name="bdbackup")
@click.option(
    "--logging",
    "log_level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Log level: DEBUG, INFO, WARNING, ERROR.",
)
def main(log_level: str) -> None:
    """Backups by design.

    File and MySQL/MariaDB backups with verification, retention, and guided recovery.
    """
    setup_logging(getattr(logging, log_level.upper(), logging.INFO))


def _load_config(path: Path) -> Config:
    try:
        return Config(path)
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc


def _history_config(path: Path | None) -> Config:
    if path is None:
        raise click.UsageError("Provide --config with a TOML configuration file")
    config = _load_config(path)
    if not config.history:
        raise click.UsageError("Configure [history] database in the TOML file first")
    return config


def _tracked_backup(config, name, backup_type, action, restore_info=None, *, encrypted=False):
    if config.history:
        return History(config.history).run(
            name, backup_type, action, restore_info, encrypted=encrypted,
        )
    return action()


def _protect_history(backend, config):
    if config.history and isinstance(backend, FileBackup):
        database = config.history.database
        excluded = {Path(str(database) + suffix) for suffix in ("", "-journal", "-wal", "-shm")}
        if backend.tar_filename.resolve() in excluded:
            raise BackupError("Archive destination conflicts with the history database")
        backend.exclude.update(excluded)


def _handle_errors(func, *args, **kwargs) -> int:
    """Wrap a command body in the exit-code contract."""
    try:
        func(*args, **kwargs)
    except (click.ClickException, click.Abort):
        raise
    except BlockingIOError as exc:
        logging.getLogger("bdbackup.cli").error("Another backup is already running: %s", exc)
        click.get_current_context().exit(EXIT_LOCKED)
    except (ConfigError, TemplateError, ValueError, TypeError) as exc:
        raise click.UsageError(str(exc)) from exc
    except BackupError as exc:
        logging.getLogger("bdbackup.cli").error("Backup/verify failed: %s", exc)
        click.get_current_context().exit(EXIT_BACKUP_FAILED)
    except subprocess.CalledProcessError as exc:
        logging.getLogger("bdbackup.cli").error("External command failed: %s", exc)
        click.get_current_context().exit(EXIT_BACKUP_FAILED)
    except Exception as exc:  # pragma: no cover - safety net
        logging.getLogger("bdbackup.cli").exception("Unexpected error: %s", exc)
        click.get_current_context().exit(EXIT_BACKUP_FAILED)
    return EXIT_OK


def _dry_run(config: Config, job: Job, incremental: bool) -> None:
    """Report what a run of ``job`` would do; run no tool and write nothing."""
    backend = build_backend(job)
    if isinstance(backend, FileBackup):
        _protect_history(backend, config)
        backend.backup(dry_run=True)
        click.echo(f"Dry run; would create: {backend.tar_filename}")
    elif isinstance(backend, MySQLBackup):
        click.echo(f"Dry run; would dump {backend.selection()} into {backend.out_dir}")
    elif isinstance(backend, XtraBackup) and incremental:
        click.echo(f"Dry run; would take an incremental of {backend.incremental_parent()} "
                   f"under {backend.backup_root}")
    elif isinstance(backend, XtraBackup):
        click.echo(f"Dry run; would take a full backup under {backend.backup_root}")
    else:
        raise click.UsageError(f"--dry-run is not supported for job type {job.type!r}")


@main.command(name="run")
@config_option(required=True)
@click.option("-j", "--job", "job_name", help="Run this configured job.")
@click.option("--full", is_flag=True, help="Take a full backup (the default).")
@click.option("--incremental", is_flag=True,
              help="Take an incremental backup of an xtrabackup job.")
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify the backup after creation.",
)
@click.option("--dry-run", is_flag=True,
              help="Report what the job would do; run no tool and change nothing.")
@click.option("--validate", "validate_only", is_flag=True,
              help="Check settings, filesystem access and MySQL grants of every job, or of "
                   "--job; run nothing.")
@click.pass_context
def run(
    ctx: click.Context,
    config_path: Path,
    job_name: str | None,
    full: bool,
    incremental: bool,
    verify: bool,
    dry_run: bool,
    validate_only: bool,
) -> int:
    """Run one configured job, or validate the configuration."""
    verify_given = ctx.get_parameter_source("verify") is not ParameterSource.DEFAULT
    if full and incremental:
        raise click.UsageError("Choose --full or --incremental, not both")
    if validate_only:
        for flag, given in (("--full", full), ("--incremental", incremental),
                            ("--verify" if verify else "--no-verify", verify_given),
                            ("--dry-run", dry_run)):
            if given:
                raise click.UsageError(f"--validate cannot be combined with {flag}")
    if dry_run and verify_given:
        raise click.UsageError("--dry-run cannot be combined with --verify or --no-verify")
    config = _load_config(config_path)
    job = None
    if job_name is not None:
        try:
            job = config.get(job_name)
        except ConfigError as exc:
            raise click.UsageError(str(exc)) from exc
        if not job.active:
            raise click.UsageError(f"Job {job.name!r} is inactive (active = false)")
    if validate_only:
        jobs = [job] if job else list(config.jobs.values())
        if not jobs:
            raise click.UsageError("No configured jobs selected")
        lines, ok = validate_config(config, jobs)
        click.echo("\n".join(lines))
        ctx.exit(EXIT_OK if ok else EXIT_BACKUP_FAILED)
    if job is None:
        raise click.UsageError("Provide --job NAME or --validate")
    if incremental and job.type != "xtrabackup":
        raise click.UsageError(f"--incremental applies only to xtrabackup jobs: {job.name}")
    logger = logging.getLogger("bdbackup.cli")
    logger.info("Running job %r (%s)%s", job.name, job.type, " as a dry run" if dry_run else "")
    if job.type == "gfs":
        code = gfs_runner.run_job(config, job, out=click.echo, dry_run=dry_run)
        ctx.exit(code if code in (EXIT_OK, EXIT_LOCKED) else EXIT_BACKUP_FAILED)
    if dry_run:
        return _handle_errors(_dry_run, config, job, incremental)
    info = {}

    def _create():
        backend = build_backend(job)
        _protect_history(backend, config)
        info.update(backup_restore_info(backend))
        result = (
            backend.incremental_backup()
            if job.type == "xtrabackup" and incremental else backend.backup()
        )
        if not result.success:
            raise BackupError("Backend reported an unsuccessful backup")
        if verify:
            backend.verify(result)
        return result

    backup_type = job.type
    if job.type == "xtrabackup":
        backup_type = "xtrabackup-incremental" if incremental else "xtrabackup-full"
    try:
        result = _tracked_backup(
            config, job.name, backup_type, _create, info,
            encrypted=job.params.get("encrypt") is True,
        )
    except BlockingIOError as exc:
        logger.error("Job %r locked: %s", job.name, exc)
        ctx.exit(EXIT_LOCKED)
    except Exception as exc:
        logger.error("Job %r failed: %s", job.name, exc)
        ctx.exit(EXIT_BACKUP_FAILED)
    click.echo(f"Job {job.name}: {result.path}")
    return EXIT_OK


def _restore_mode(config_path, backup_id, job, yes, dst, archive, backup, root, binary,
                  encrypt_key_file) -> str:
    """Name the restore mode, or refuse a combination that mixes modes (spec 10 §2)."""
    if archive is not None and (backup is not None or root is not None):
        raise click.UsageError("--archive and --backup cannot be combined")
    if backup is not None and root is None:
        raise click.UsageError("--backup requires --root")
    if root is not None and backup is None:
        raise click.UsageError("--root requires --backup")
    if archive is None and backup is None:
        if binary is not None:
            raise click.UsageError("--binary applies only to --backup")
        return "history"
    if config_path is not None:
        raise click.UsageError("--config applies only to history restores")
    if backup_id is not None or job is not None or yes:
        raise click.UsageError("--backup-id, --job and --yes apply only to history restores")
    if archive is not None and binary is not None:
        raise click.UsageError("--binary applies only to --backup")
    if archive is not None and encrypt_key_file is not None:
        raise click.UsageError("--encrypt-key-file does not apply to --archive")
    if dst is None:
        raise click.UsageError("Provide --dst for --archive and --backup")
    return "archive" if archive is not None else "backup"


@main.command()
@config_option()
@click.option("--backup-id", type=click.IntRange(min=1), help="Successful backup ID from history.")
@click.option("--job", help="Limit history choices to this job.")
@click.option("-y", "--yes", is_flag=True, help="Confirm restore of an explicit --backup-id.")
@click.option(
    "-d",
    "--dst",
    type=click.Path(file_okay=False, path_type=Path),
    help="Recovery directory; history mode suggests a new path when omitted.",
)
@click.option("--archive", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Restore this tar archive (no history needed).")
@click.option("--backup", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Prepare this physical backup directory (needs --root).")
@click.option("--root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="The physical backup's root, holding its dated directories.")
@click.option("--binary", help="Backup binary for --backup (xtrabackup or mariadb-backup); "
                               "default: xtrabackup.")
@click.option("--encrypt-key-file", type=click.Path(dir_okay=False, path_type=Path),
              help="Key file for an encrypted physical backup; overrides the one in history.")
def restore(
    config_path: Path | None,
    backup_id: int | None,
    job: str | None,
    yes: bool,
    dst: Path | None,
    archive: Path | None,
    backup: Path | None,
    root: Path | None,
    binary: str | None,
    encrypt_key_file: Path | None,
) -> int:
    """Restore a backup from history, a tar archive, or a physical backup directory."""
    mode = _restore_mode(config_path, backup_id, job, yes, dst, archive, backup, root, binary,
                         encrypt_key_file)

    def _run():
        if mode == "archive":
            FileBackup.restore_archive(archive, dst)
            click.echo(f"Restored to: {dst}")
            return
        if mode == "backup":
            xb = XtraBackup(backup_root=root, binary=binary or "xtrabackup",
                            encrypt_key_file=encrypt_key_file)
            click.echo(f"Prepared recovery directory: {xb.prepare(backup, dst)}")
            return
        config = _history_config(config_path)
        history = History(config.history)
        known = gfs_store.places(history)

        def shown(record):
            """A unit GFS manages is shown at its first available location."""
            available = [p.member for p in known.get(record.id, ())
                         if gfs_store.place_available(p)]
            return available[0] if available else record.path

        if backup_id is None:
            if yes:
                raise click.UsageError("--yes requires an explicit --backup-id")
            records = [r for r in history.records(job=job, successful=True)
                       if gfs_store.available(r, known)]
            if not records:
                raise BackupError("No successful, available backups found")
            click.echo("Successful backups (newest first):")
            for record in records:
                click.echo(
                    f"{record.id}: {record.job_name} | {record.backup_type} | "
                    f"{record.started_at} | "
                    f"{'encrypted' if record.encrypted else 'unencrypted'} | {shown(record)}"
                )
            selected_id = click.prompt("Backup ID", type=int, default=records[0].id)
            choices = {r.id: r for r in records}
            if selected_id not in choices:
                raise click.UsageError("Choose a backup ID from the displayed list")
            record = choices[selected_id]
        else:
            record = history.get(backup_id)
            if job is not None and record.job_name != job:
                raise click.UsageError("The selected backup does not belong to --job")
        if not gfs_store.available(record, known):
            raise BackupError("Backup is unsuccessful, missing, or has been replaced/modified")
        managed = record.id in known
        current_job = config.jobs.get(record.job_name)
        restore_root = (
            current_job.restore_root if current_job and current_job.restore_root
            else config.history.restore_root
        )
        destination = dst or suggested_destination(record, restore_root)
        if managed:
            # `Backup:` follows once restore has chosen the copy it restores from.
            click.echo("Locations: " + ", ".join(
                str(p.member) for p in known[record.id] if gfs_store.place_available(p)))
        else:
            click.echo(f"Backup: {record.path}")
        if dst is None and not yes:
            destination = Path(click.prompt("Restore destination", default=str(destination)))
        click.echo(f"Recovery destination: {destination}")
        if not yes:
            click.confirm("Restore this backup?", abort=True)
        if encrypt_key_file is not None and not record.backup_type.startswith("xtrabackup"):
            raise click.UsageError("--encrypt-key-file requires a physical backup")
        restored = restore_record(record, destination, encrypt_key_file=encrypt_key_file,
                                  history=history,
                                  on_source=(lambda path: click.echo(f"Backup: {path}"))
                                  if managed else None)
        if record.backup_type == "mysqldump":
            click.echo(f"SQL recovered to: {restored / 'backup.sql'} (not imported into a server)")
        elif record.backup_type.startswith("xtrabackup"):
            click.echo(f"Prepared recovery directory: {restored}")
        else:
            click.echo(f"Restored to: {restored}")

    return _handle_errors(_run)


@main.command(name="history")
@config_option(required=True)
@click.option("--job", help="Only this job.")
@click.option("--successful", is_flag=True, help="Show only successful runs.")
@click.option("--create-checksum", is_flag=True,
              help="Record unit and checksum for older records that have none.")
def history_cmd(config_path: Path, job: str | None, successful: bool,
                create_checksum: bool) -> int:
    """List recorded backup attempts and whether their artifacts are still available."""
    if create_checksum and successful:
        raise click.UsageError("--create-checksum cannot be combined with --successful")
    unexpected = []

    def _checksums():
        config = _history_config(config_path)
        for record_id, outcome in History(config.history).record_checksums(job):
            click.echo(f"{record_id}: {outcome}")
            if outcome == "skipped: unexpected unit":
                unexpected.append(record_id)

    def _listing():
        config = _history_config(config_path)
        history = History(config.history)
        records = history.records(job=job, successful=successful)
        suffixes = gfs_store.listing_suffixes(history)
        known = gfs_store.places(history)
        if not records:
            click.echo("No backup history found.")
        for record in records:
            available = "available" if gfs_store.available(record, known) else "unavailable"
            click.echo(
                f"{record.id}: {record.job_name} | {record.backup_type} | {record.started_at} | "
                f"{record.status} | {available} | "
                f"{'encrypted' if record.encrypted else 'unencrypted'} | {record.path or '-'}"
                f" | unit {record.unit or '-'}"
                f"{suffixes.get(record.unit, '') if record.unit else ''}"
            )

    _handle_errors(_checksums if create_checksum else _listing)
    if unexpected:
        click.get_current_context().exit(EXIT_BACKUP_FAILED)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
