"""Command-line interface for bdbackup."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import click

from bdbackup import __version__, retention
from bdbackup.backends import BackupError, setup_logging
from bdbackup.config import Config, ConfigError, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.templates import TemplateError

# Exit-code contract: 0 ok, 1 backup/verify failure, 2 usage, 3 locked.
EXIT_OK = 0
EXIT_BACKUP_FAILED = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3


@click.group(name="bdbackup", invoke_without_command=False)
@click.version_option(version=__version__, prog_name="bdbackup")
@click.option(
    "--logging",
    "log_level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Log level: DEBUG, INFO, WARNING, ERROR.",
)
@click.pass_context
def main(ctx: click.Context, log_level: str) -> None:
    """Brain-dead backup: file archives, mysqldump and xtrabackup helpers."""
    setup_logging(getattr(logging, log_level.upper(), logging.INFO))
    ctx.ensure_object(dict)
    ctx.obj["log_level"] = log_level


def _handle_errors(func, *args, **kwargs) -> int:
    """Wrap a command body in the exit-code contract."""
    try:
        func(*args, **kwargs)
    except click.ClickException:
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

    def _run() -> None:
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

    return _handle_errors(_run)


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

    def _run() -> None:
        mb = MySQLBackup(
            out_dir=out_dir,
            user=user,
            password=password,
            host=host,
            port=port,
            options=opts,
            jobs=jobs,
        )
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
    compress: str,
    compress_threads: int,
    parallel: int,
    throttle: int | None,
    retention: int,
    verify: bool,
) -> int:
    """Run a full physical backup."""

    def _run() -> None:
        xb = XtraBackup(
            backup_root=backup_root / database,
            user=user,
            password=password,
            binary=binary,
            compress=compress,
            compress_threads=compress_threads,
            parallel=parallel,
            throttle=throttle,
            retention_days=retention,
        )
        result = xb.full_backup()
        if verify:
            xb.verify(result)
        click.echo(f"Backup written to: {result.path}")

    return _handle_errors(_run)


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
    compress: str,
    compress_threads: int,
    parallel: int,
    throttle: int | None,
    verify: bool,
) -> int:
    """Run an incremental physical backup based on the latest full."""

    def _run() -> None:
        xb = XtraBackup(
            backup_root=backup_root / database,
            user=user,
            password=password,
            binary=binary,
            compress=compress,
            compress_threads=compress_threads,
            parallel=parallel,
            throttle=throttle,
        )
        result = xb.incremental_backup()
        if verify:
            xb.verify(result)
        click.echo(f"Backup written to: {result.path}")

    return _handle_errors(_run)


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
def xtrabackup_prepare(
    target: Path,
    backup_root: Path,
    destination: Path,
    user: str,
    password: str | None,
    binary: str,
) -> int:
    """Run --prepare on a backup directory to make it restorable."""
    xb = XtraBackup(
        backup_root=backup_root,
        user=user,
        password=password,
        binary=binary,
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
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-d",
    "--dst",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to extract the archive into.",
)
@click.option(
    "-c",
    "--chdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Resolve template paths against this directory.",
)
def restore(
    archive: Path,
    dst: Path,
    chdir: Path | None,
) -> int:
    """Extract a file backup archive to a destination directory."""
    return _handle_errors(
        lambda: (
            FileBackup.restore_archive(archive, dst),
            click.echo(f"Restored to: {dst}"),
        )
    )


@main.command(name="run")
@click.option(
    "-c",
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="TOML configuration file with job definitions.",
)
@click.argument("job_name", required=False)
@click.option("--all", "run_all", is_flag=True, help="Run every job in the config.")
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify backups after creation.",
)
def run(
    config_path: Path,
    job_name: str | None,
    run_all: bool,
    verify: bool,
) -> int:
    """Run one or more configured backup jobs."""
    try:
        config = Config(config_path)
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc

    names = list(config.jobs.keys()) if run_all else [job_name]
    if not names or any(n is None for n in names):
        raise click.UsageError("Provide a JOB_NAME or use --all.")

    try:
        selected = [config.get(name) for name in names]
    except ConfigError as exc:
        raise click.UsageError(str(exc)) from exc
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
            backend = build_backend(job)
            result = backend.backup()
            if not result.success:
                raise BackupError("Backend reported an unsuccessful backup")
            if verify:
                backend.verify(result)
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
