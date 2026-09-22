"""Command-line interface for bdbackup."""

from __future__ import annotations

import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from bdbackup import __version__, retention
from bdbackup.backends import BackupError, BackupResult, setup_logging
from bdbackup.config import Config, ConfigError, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.history import History
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.recovery import backup_restore_info, restore_record, suggested_destination
from bdbackup.scheduling import recommend_cron
from bdbackup.templates import TemplateError
from bdbackup.validation import validate_config

# Exit-code contract: 0 ok, 1 backup/verify failure, 2 usage, 3 locked.
EXIT_OK = 0
EXIT_BACKUP_FAILED = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


@click.group(name="bdbackup", invoke_without_command=True, no_args_is_help=True)
@click.version_option(version=__version__, prog_name="bdbackup")
@click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="TOML configuration; enables configured history for direct backup commands.",
)
@click.option(
    "--logging",
    "log_level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Log level: DEBUG, INFO, WARNING, ERROR.",
)
@click.pass_context
@click.option("--validate", "validate_only", is_flag=True,
              help="Check configured settings, filesystem access and MySQL grants; do not back up.")
@click.option("--cron", "cron_only", is_flag=True,
              help="Read crontab -l and recommend entries for configured jobs; do not install.")
def main(
    ctx: click.Context, log_level: str, config_path: Path | None,
    validate_only: bool, cron_only: bool,
) -> None:
    """Backups by design.

    File and MySQL/MariaDB backups with verification, retention, and guided recovery.
    """
    setup_logging(getattr(logging, log_level.upper(), logging.INFO))
    ctx.ensure_object(dict)
    ctx.obj["log_level"] = log_level
    try:
        ctx.obj["config"] = Config(config_path) if config_path else None
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc
    if validate_only or cron_only:
        if ctx.invoked_subcommand:
            raise click.UsageError("Use global --validate/--cron without a subcommand, "
                                   "or put these options after run")
        config = _get_config(required=True)
        ctx.exit(_configuration_helpers(config, list(config.jobs.values()),
                                        validate_only, cron_only))
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(EXIT_USAGE)


def _configuration_helpers(config, jobs, validate_only, cron_only) -> int:
    ok = True
    if not jobs:
        raise click.UsageError("No configured jobs selected")
    for enabled, helper in ((validate_only, validate_config), (cron_only, recommend_cron)):
        if enabled:
            lines, success = helper(config, jobs)
            click.echo("\n".join(lines))
            ok = ok and success
    return EXIT_OK if ok else EXIT_BACKUP_FAILED


def _get_config(path: Path | None = None, *, required: bool = False) -> Config | None:
    config = Config(path) if path else click.get_current_context().obj.get("config")
    if required and config is None:
        raise click.UsageError("Provide --config with a TOML configuration file")
    return config


def _tracked_backup(config, name, backup_type, action, restore_info=None, *, encrypted=False):
    if config and config.history:
        return History(config.history).run(
            name, backup_type, action, restore_info, encrypted=encrypted,
        )
    return action()


def _protect_history(backend, config):
    if config and config.history and isinstance(backend, FileBackup):
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


@main.command()
@click.option(
    "--template",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Template file listing paths to archive.",
)
@click.option(
    "-d",
    "--dst",
    required=True,
    type=click.Path(path_type=Path),
    help="Destination archive path (without extension).",
)
@click.option(
    "-c",
    "--chdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Resolve template paths against this directory.",
)
@click.option("-z", "--compress/--no-compress", default=False, help="Gzip-compress the archive.")
@click.option(
    "-f",
    "--format",
    "archive_format",
    default="tar",
    show_default=True,
    type=click.Choice(["tar", "tar.gz", "tar.zst"], case_sensitive=False),
    help="Archive format.",
)
@click.option("-x", "--exclude", multiple=True, help="Path/pattern to exclude; can be repeated.")
@click.option(
    "--exclude-pattern",
    multiple=True,
    help="Glob pattern to exclude; can be repeated.",
)
@click.option(
    "--exclude-template",
    "exclude_templates",
    multiple=True,
    metavar="NAME",
    help=(
        "Named exclusion template (e.g. python-dev); can be repeated or "
        "comma-separated. User templates live in ~/.config/bdbackup/templates/."
    ),
)
@click.option(
    "--follow-symlinks/--no-follow-symlinks",
    default=False,
    show_default=True,
    help="Follow symbolic links when archiving directories.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List what would be archived without writing anything.",
)
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify the archive after creation.",
)
def file(
    template: Path,
    dst: Path,
    chdir: Path | None,
    compress: bool,
    archive_format: str,
    exclude: tuple[str, ...],
    exclude_pattern: tuple[str, ...],
    exclude_templates: tuple[str, ...],
    follow_symlinks: bool,
    dry_run: bool,
    verify: bool,
) -> int:
    """Create a tar archive from a template file listing paths."""
    log_level = click.get_current_context().obj.get("log_level", "INFO")
    debug = log_level == "DEBUG"
    template_names = [
        n.strip() for group in exclude_templates for n in group.split(",") if n.strip()
    ]
    config = _get_config()

    def _create() -> BackupResult:
        try:
            fb = FileBackup(
                backup_dst=dst,
                template_filename=template,
                chdir=chdir,
                format="tar.gz" if compress else archive_format,
                exclude=exclude,
                exclude_pattern=exclude_pattern,
                exclude_templates=template_names,
                follow_symlinks=follow_symlinks,
            )
        except TemplateError as exc:
            raise click.UsageError(str(exc)) from exc
        _protect_history(fb, config)
        try:
            result = fb.backup(debug=debug, dry_run=dry_run)
            if verify and not dry_run:
                fb.verify(result)
        finally:
            fb.close_archive()
        if dry_run:
            click.echo(f"Dry run; would create: {fb.tar_filename}")
        else:
            click.echo(f"Archive created: {fb.tar_filename}")
        return result

    return _handle_errors(
        lambda: _tracked_backup(
            None if dry_run else config, dst.name, "file", _create, {"kind": "file"},
        )
    )


@main.command()
@click.option("--database", required=False, help="Database name to dump (ignored when --full).")
@click.option(
    "-o",
    "--out-dir",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for the dump file.",
)
@click.option("-u", "--user", default="root", show_default=True, help="MySQL user.")
@click.option(
    "-p",
    "--password",
    default=None,
    prompt="MySQL password",
    prompt_required=False,
    hide_input=True,
    is_flag=False,
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option("-h", "--host", default="localhost", show_default=True, help="MySQL host.")
@click.option("-P", "--port", default=3306, show_default=True, type=int, help="MySQL port.")
@click.option(
    "--options",
    default="",
    show_default=True,
    help="Additional comma-separated mysqldump options (safe defaults are retained).",
)
@click.option("--full/--no-full", default=False, help="Add --all-databases and dump everything.")
@click.option(
    "-j",
    "--jobs",
    default=1,
    show_default=True,
    type=click.IntRange(min=1),
    help="Parallel dumps when multiple databases are specified.",
)
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify the dump after creation.",
)
def mysqldump(
    database: str | None,
    out_dir: Path,
    user: str,
    password: str | None,
    host: str,
    port: int,
    options: str,
    full: bool,
    jobs: int,
    verify: bool,
) -> int:
    """Dump a MySQL database with mysqldump."""
    if not full and not database:
        raise click.UsageError("--database is required unless --full is used.")
    opts = [option.strip() for option in options.split(",") if option.strip()]
    if full:
        opts.append("--all-databases")
    databases = [db.strip() for db in (database or "").split(",") if db.strip()]
    if not full and not databases:
        raise click.UsageError("Provide at least one database name")
    config = _get_config()

    def _backend():
        return MySQLBackup(
            out_dir=out_dir,
            user=user,
            password=password,
            host=host,
            port=port,
            options=opts,
            jobs=jobs,
        )

    def _run() -> None:
        if config and config.history:
            # Each worker owns its history transactions, so partial batch failures
            # cannot hide the successful dumps from the other workers.
            def _one(db):
                def _create():
                    mb = _backend()
                    result = mb.backup(db)
                    if verify:
                        mb.verify(result)
                    return result

                return _tracked_backup(
                    config, db or "all-databases", "mysqldump", _create, {"kind": "mysqldump"},
                )

            failures = []
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = [pool.submit(_one, db) for db in ([None] if full else databases)]
                for future in as_completed(futures):
                    try:
                        click.echo(f"Dump written to: {future.result().path}")
                    except Exception as exc:
                        failures.append(exc)
            if failures:
                raise failures[0]
            return
        mb = _backend()
        results = [mb.backup(None)] if full else mb.backup_all(databases)
        for result in results:
            if verify:
                mb.verify(result)
            click.echo(f"Dump written to: {result.path}")

    return _handle_errors(_run)


@main.group(invoke_without_command=False)
def xtrabackup() -> None:
    """Run or manage xtrabackup/mariabackup physical backups."""


@xtrabackup.command(name="full")
@click.option("--database", required=True, help="Database name (used for directory naming).")
@click.option(
    "-r",
    "--root",
    "backup_root",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Backup root directory.",
)
@click.option("-u", "--user", default="xtrabackup", show_default=True, help="MySQL user.")
@click.option(
    "-p",
    "--password",
    default=None,
    prompt="MySQL password",
    prompt_required=False,
    hide_input=True,
    is_flag=False,
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option(
    "-b",
    "--binary",
    default="xtrabackup",
    show_default=True,
    help="Backup binary (xtrabackup or mariabackup).",
)
@click.option("--encrypt/--no-encrypt", default=False,
              help="Encrypt with AES256; requires a key file.")
@click.option("--encrypt-key-file", type=click.Path(dir_okay=False, path_type=Path),
              help="32-byte AES256 key file (e.g. /etc/mysql/xtrabackup.key).")
@click.option("--compress", default="", help="Compression algorithm; default: uncompressed.")
@click.option(
    "--compress-threads",
    default=4,
    show_default=True,
    type=click.IntRange(min=1),
    help="Number of compression threads.",
)
@click.option(
    "--parallel",
    default=1,
    show_default=True,
    type=click.IntRange(min=1),
    help="Number of copy threads.",
)
@click.option(
    "--throttle",
    default=None,
    type=click.IntRange(min=1),
    help="Limit I/O to this many IOPS.",
)
@click.option(
    "--retention",
    default=5,
    show_default=True,
    type=click.IntRange(min=1),
    help="Retention in days.",
)
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify the backup after creation.",
)
def xtrabackup_full(
    database: str,
    backup_root: Path,
    user: str,
    password: str | None,
    binary: str,
    encrypt: bool,
    encrypt_key_file: Path | None,
    compress: str,
    compress_threads: int,
    parallel: int,
    throttle: int | None,
    retention: int,
    verify: bool,
) -> int:
    """Run a full physical backup."""
    config = _get_config()
    info = {"kind": "xtrabackup", "backup_root": str((backup_root / database).resolve()),
            "binary": binary}

    def _create() -> BackupResult:
        xb = XtraBackup(
            backup_root=backup_root / database,
            user=user,
            password=password,
            binary=binary,
            encrypt=encrypt,
            encrypt_key_file=encrypt_key_file,
            compress=compress,
            compress_threads=compress_threads,
            parallel=parallel,
            throttle=throttle,
            retention_days=retention,
        )
        info.update(backup_restore_info(xb))
        result = xb.full_backup()
        if verify:
            xb.verify(result)
        click.echo(f"Backup written to: {result.path}")
        return result

    return _handle_errors(
        lambda: _tracked_backup(
            config, database, "xtrabackup-full", _create, info, encrypted=encrypt,
        )
    )


@xtrabackup.command(name="incremental")
@click.option("--database", required=True, help="Database name (used for directory naming).")
@click.option(
    "-r",
    "--root",
    "backup_root",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Backup root directory.",
)
@click.option("-u", "--user", default="xtrabackup", show_default=True, help="MySQL user.")
@click.option(
    "-p",
    "--password",
    default=None,
    prompt="MySQL password",
    prompt_required=False,
    hide_input=True,
    is_flag=False,
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option(
    "-b",
    "--binary",
    default="xtrabackup",
    show_default=True,
    help="Backup binary (xtrabackup or mariabackup).",
)
@click.option("--encrypt/--no-encrypt", default=False,
              help="Encrypt with AES256; requires a key file.")
@click.option("--encrypt-key-file", type=click.Path(dir_okay=False, path_type=Path),
              help="32-byte AES256 key file (e.g. /etc/mysql/xtrabackup.key).")
@click.option("--compress", default="", help="Compression algorithm; default: uncompressed.")
@click.option(
    "--compress-threads",
    default=4,
    show_default=True,
    type=click.IntRange(min=1),
    help="Number of compression threads.",
)
@click.option(
    "--parallel",
    default=1,
    show_default=True,
    type=click.IntRange(min=1),
    help="Number of copy threads.",
)
@click.option(
    "--throttle",
    default=None,
    type=click.IntRange(min=1),
    help="Limit I/O to this many IOPS.",
)
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify the backup after creation.",
)
def xtrabackup_incremental(
    database: str,
    backup_root: Path,
    user: str,
    password: str | None,
    binary: str,
    encrypt: bool,
    encrypt_key_file: Path | None,
    compress: str,
    compress_threads: int,
    parallel: int,
    throttle: int | None,
    verify: bool,
) -> int:
    """Run an incremental physical backup based on the latest full."""
    config = _get_config()
    info = {"kind": "xtrabackup", "backup_root": str((backup_root / database).resolve()),
            "binary": binary}

    def _create() -> BackupResult:
        xb = XtraBackup(
            backup_root=backup_root / database,
            user=user,
            password=password,
            binary=binary,
            encrypt=encrypt,
            encrypt_key_file=encrypt_key_file,
            compress=compress,
            compress_threads=compress_threads,
            parallel=parallel,
            throttle=throttle,
        )
        info.update(backup_restore_info(xb))
        result = xb.incremental_backup()
        if verify:
            xb.verify(result)
        click.echo(f"Backup written to: {result.path}")
        return result

    return _handle_errors(
        lambda: _tracked_backup(
            config, database, "xtrabackup-incremental", _create, info, encrypted=encrypt,
        )
    )


@xtrabackup.command(name="prune")
@click.option("--database", required=True, help="Database name (used for directory naming).")
@click.option(
    "-r",
    "--root",
    "backup_root",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Backup root directory.",
)
@click.option(
    "--retention",
    default=5,
    show_default=True,
    type=click.IntRange(min=1),
    help="Retention in days.",
)
def xtrabackup_prune(
    database: str,
    backup_root: Path,
    retention: int,
) -> int:
    """Remove backup directories older than retention days."""
    xb = XtraBackup(
        backup_root=backup_root / database,
        retention_days=retention,
    )
    return _handle_errors(lambda: click.echo(f"Pruned {len(xb.prune())} backup directories"))


@xtrabackup.command(name="prepare")
@click.argument("target", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "-r",
    "--root",
    "backup_root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="The database's backup root (containing dated directories).",
)
@click.option(
    "-d",
    "--dst",
    "destination",
    required=True,
    type=click.Path(path_type=Path),
    help="New recovery directory outside the backup root.",
)
@click.option("-u", "--user", default="xtrabackup", show_default=True, help="MySQL user.")
@click.option(
    "-p",
    "--password",
    default=None,
    prompt="MySQL password",
    prompt_required=False,
    hide_input=True,
    is_flag=False,
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option(
    "-b",
    "--binary",
    default="xtrabackup",
    show_default=True,
    help="Backup binary (xtrabackup or mariabackup).",
)
@click.option("--encrypt-key-file", type=click.Path(dir_okay=False, path_type=Path),
              help="32-byte AES256 key file for encrypted backups.")
def xtrabackup_prepare(
    target: Path,
    backup_root: Path,
    destination: Path,
    user: str,
    password: str | None,
    binary: str,
    encrypt_key_file: Path | None,
) -> int:
    """Run --prepare on a backup directory to make it restorable."""
    xb = XtraBackup(
        backup_root=backup_root,
        user=user,
        password=password,
        binary=binary,
        encrypt_key_file=encrypt_key_file,
    )
    return _handle_errors(lambda: click.echo(f"Prepared: {xb.prepare(target, destination)}"))


@main.command()
@click.option(
    "--full-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing full backups.",
)
@click.option(
    "--incr-dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing incremental backups (optional).",
)
@click.option(
    "--log-dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing backup logs to expire by age (optional).",
)
@click.option(
    "--full-glob",
    default=retention.DEFAULTS["full_glob"],
    show_default=True,
    help="Glob matching full backup files.",
)
@click.option(
    "--incr-glob",
    default=retention.DEFAULTS["incr_glob"],
    show_default=True,
    help="Glob matching incremental backup files.",
)
@click.option(
    "--log-glob",
    default=retention.DEFAULTS["log_glob"],
    show_default=True,
    help="Glob matching log files.",
)
@click.option(
    "--daily",
    default=retention.DEFAULTS["daily"],
    show_default=True,
    type=int,
    help="Keep EVERY backup of the last N calendar days.",
)
@click.option(
    "--weekly",
    default=retention.DEFAULTS["weekly"],
    show_default=True,
    type=int,
    help="Then one backup per ISO week, for N weeks.",
)
@click.option(
    "--monthly",
    default=retention.DEFAULTS["monthly"],
    show_default=True,
    type=int,
    help="Then one backup per calendar month, for N months.",
)
@click.option(
    "--incr-days",
    default=retention.DEFAULTS["incr_days"],
    show_default=True,
    type=int,
    help="Keep incrementals for N days, never past their full.",
)
@click.option(
    "--log-days",
    default=retention.DEFAULTS["log_days"],
    show_default=True,
    type=int,
    help="Keep logs for N days.",
)
@click.option(
    "--pick",
    "pick_mode",
    default=retention.DEFAULTS["pick"],
    show_default=True,
    type=click.Choice(["first", "last"]),
    help="Which backup survives inside a week/month bucket.",
)
@click.option(
    "--min-keep-fulls",
    default=retention.DEFAULTS["min_keep_fulls"],
    show_default=True,
    type=int,
    help="Safety floor: never leave fewer than N fulls on disk.",
)
@click.option(
    "--now", default=None, metavar="YYYY-MM-DD", help="Pretend it is this date (for testing)."
)
@click.option("--apply", is_flag=True, help="Actually delete; default is a dry run.")
@click.option("-q", "--quiet", is_flag=True, help="Only print the summary line.")
def retention_cmd(
    full_dir: Path,
    incr_dir: Path | None,
    log_dir: Path | None,
    full_glob: str,
    incr_glob: str,
    log_glob: str,
    daily: int,
    weekly: int,
    monthly: int,
    incr_days: int,
    log_days: int,
    pick_mode: str,
    min_keep_fulls: int,
    now: str | None,
    apply: bool,
    quiet: bool,
) -> int:
    """Apply a GFS (daily/weekly/monthly) retention policy to a backup tree.

    Default is a DRY RUN: nothing is deleted until --apply is passed.
    Incremental backups are chain-safe: they never outlive their parent full,
    and a full that still anchors a live incremental is kept.
    """
    import datetime as _dt

    now_dt = None
    if now:
        try:
            now_dt = _dt.datetime.strptime(now, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        except ValueError:
            raise click.UsageError("--now must be YYYY-MM-DD") from None

    log_level = click.get_current_context().obj.get("log_level", "INFO")
    rc = retention.run_job(
        full_dir=full_dir,
        incr_dir=incr_dir,
        log_dir=log_dir,
        full_glob=full_glob,
        incr_glob=incr_glob,
        log_glob=log_glob,
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        incr_days=incr_days,
        log_days=log_days,
        pick_mode=pick_mode,
        min_keep_fulls=min_keep_fulls,
        now=now_dt,
        apply_changes=apply,
        verbose=(log_level == "DEBUG"),
        quiet=quiet,
    )
    if rc:
        click.get_current_context().exit(rc)
    return rc


@main.command()
@click.argument("archive", required=False,
                type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--config", "config_path",
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--backup-id", type=click.IntRange(min=1), help="Successful backup ID from history.")
@click.option("--job", help="Limit history choices to this job.")
@click.option("-y", "--yes", is_flag=True, help="Confirm restore of an explicit --backup-id.")
@click.option(
    "-d",
    "--dst",
    type=click.Path(file_okay=False, path_type=Path),
    help="Recovery directory; history mode suggests a new path when omitted.",
)
@click.option(
    "-c",
    "--chdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Resolve template paths against this directory.",
)
@click.option("--encrypt-key-file", type=click.Path(dir_okay=False, path_type=Path),
              help="Override the encryption key file recorded in physical backup history.")
def restore(
    archive: Path | None,
    dst: Path | None,
    chdir: Path | None,
    config_path: Path | None,
    backup_id: int | None,
    encrypt_key_file: Path | None,
    job: str | None,
    yes: bool,
) -> int:
    """Restore an archive, or select a successful backup from configured history."""
    def _run():
        if archive is not None:
            if encrypt_key_file is not None:
                raise click.UsageError("--encrypt-key-file requires physical backup history")
            if backup_id is not None or job is not None:
                raise click.UsageError("ARCHIVE cannot be combined with --backup-id or --job")
            if dst is None:
                raise click.UsageError("Provide --dst when restoring an explicit ARCHIVE")
            FileBackup.restore_archive(archive, dst)
            click.echo(f"Restored to: {dst}")
            return
        config = _get_config(config_path, required=True)
        if not config.history:
            raise click.UsageError("Configure [history] database in the TOML file first")
        history = History(config.history)
        if backup_id is None:
            if yes:
                raise click.UsageError("--yes requires an explicit --backup-id")
            records = [r for r in history.records(job=job, successful=True) if r.available]
            if not records:
                raise BackupError("No successful, available backups found")
            click.echo("Successful backups (newest first):")
            for record in records:
                click.echo(
                    f"{record.id}: {record.job_name} | {record.backup_type} | "
                    f"{record.started_at} | "
                    f"{'encrypted' if record.encrypted else 'unencrypted'} | {record.path}"
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
        if not record.available:
            raise BackupError("Backup is unsuccessful, missing, or has been replaced/modified")
        destination = dst or suggested_destination(record, config.history.restore_root)
        click.echo(f"Backup: {record.path}")
        if dst is None and not yes:
            destination = Path(click.prompt("Restore destination", default=str(destination)))
        click.echo(f"Recovery destination: {destination}")
        if not yes:
            click.confirm("Restore this backup?", abort=True)
        if encrypt_key_file is not None:
            if not record.backup_type.startswith("xtrabackup"):
                raise click.UsageError("--encrypt-key-file requires a physical backup")
            restored = restore_record(record, destination, encrypt_key_file=encrypt_key_file)
        else:
            restored = restore_record(record, destination)
        if record.backup_type == "mysqldump":
            click.echo(f"SQL recovered to: {restored / 'backup.sql'} (not imported into a server)")
        elif record.backup_type.startswith("xtrabackup"):
            click.echo(f"Prepared recovery directory: {restored}")
        else:
            click.echo(f"Restored to: {restored}")

    return _handle_errors(_run)


@main.command(name="history")
@click.option("--config", "config_path",
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--job", help="Show only this job.")
@click.option("--successful", is_flag=True, help="Show only successful runs.")
def history_cmd(config_path: Path | None, job: str | None, successful: bool) -> int:
    """List recorded backup attempts and whether their artifacts are still available."""
    def _run():
        config = _get_config(config_path, required=True)
        if not config.history:
            raise click.UsageError("Configure [history] database in the TOML file first")
        records = History(config.history).records(job=job, successful=successful)
        if not records:
            click.echo("No backup history found.")
        for record in records:
            available = "available" if record.available else "unavailable"
            click.echo(
                f"{record.id}: {record.job_name} | {record.backup_type} | {record.started_at} | "
                f"{record.status} | {available} | "
                f"{'encrypted' if record.encrypted else 'unencrypted'} | {record.path or '-'}"
            )

    return _handle_errors(_run)


@main.command(name="run")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="TOML configuration file with job definitions.",
)
@click.argument("job_name", required=False)
@click.option("--all", "run_all", is_flag=True, help="Run every job in the config.")
@click.option("--validate", "validate_only", is_flag=True,
              help="Validate selected jobs without running backups or retention.")
@click.option("--cron", "cron_only", is_flag=True,
              help="Recommend cron entries for selected jobs without installing them.")
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify backups after creation.",
)
def run(
    config_path: Path | None,
    job_name: str | None,
    run_all: bool,
    verify: bool,
    validate_only: bool,
    cron_only: bool,
) -> int:
    """Run one or more configured backup jobs."""
    try:
        config = _get_config(config_path, required=True)
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc

    if run_all and job_name:
        raise click.UsageError("Choose JOB_NAME or --all, not both")
    select_all = run_all or ((validate_only or cron_only) and job_name is None)
    names = list(config.jobs.keys()) if select_all else [job_name]
    if not names or any(n is None for n in names):
        raise click.UsageError("Provide a JOB_NAME or use --all.")

    try:
        selected = [config.get(name) for name in names]
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc
    if validate_only or cron_only:
        click.get_current_context().exit(
            _configuration_helpers(config, selected, validate_only, cron_only)
        )
    logger = logging.getLogger("bdbackup.cli")
    failures = []
    for job in selected:
        logger.info("Running job %r (%s)", job.name, job.type)
        try:
            if job.type == "retention":
                params = dict(job.params)
                if "apply" in params:
                    params["apply_changes"] = params.pop("apply")
                if "pick" in params:
                    params["pick_mode"] = params.pop("pick")
                if retention.run_job(**params):
                    failures.append(EXIT_BACKUP_FAILED)
                continue
            info = {}

            def _create(job=job, info=info):
                backend = build_backend(job)
                _protect_history(backend, config)
                info.update(backup_restore_info(backend))
                result = backend.backup()
                if not result.success:
                    raise BackupError("Backend reported an unsuccessful backup")
                if verify:
                    backend.verify(result)
                return result

            result = _tracked_backup(
                config, job.name, job.type, _create, info,
                encrypted=job.params.get("encrypt") is True,
            )
            click.echo(f"Job {job.name}: {result.path}")
        except BlockingIOError as exc:
            logger.error("Job %r locked: %s", job.name, exc)
            failures.append(EXIT_LOCKED)
        except Exception as exc:
            logger.error("Job %r failed: %s", job.name, exc)
            failures.append(EXIT_BACKUP_FAILED)
    if failures:
        click.get_current_context().exit(
            EXIT_BACKUP_FAILED if EXIT_BACKUP_FAILED in failures else EXIT_LOCKED,
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
