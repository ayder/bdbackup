"""Command-line interface for bdbackup."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import click

from bdbackup import __version__
from bdbackup.backends import BackupError, setup_logging
from bdbackup.config import Config, ConfigError, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.mysqlbackup import MySQLBackup
from bdbackup.xtrabackup import XtraBackup

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
        return EXIT_LOCKED
    except BackupError as exc:
        logging.getLogger("bdbackup.cli").error("Backup/verify failed: %s", exc)
        return EXIT_BACKUP_FAILED
    except subprocess.CalledProcessError as exc:
        logging.getLogger("bdbackup.cli").error("External command failed: %s", exc)
        return EXIT_BACKUP_FAILED
    except Exception as exc:  # pragma: no cover - safety net
        logging.getLogger("bdbackup.cli").exception("Unexpected error: %s", exc)
        return EXIT_BACKUP_FAILED
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
    follow_symlinks: bool,
    dry_run: bool,
    verify: bool,
) -> int:
    """Create a tar archive from a template file listing paths."""
    log_level = click.get_current_context().obj.get("log_level", "INFO")
    debug = log_level == "DEBUG"

    def _run() -> None:
        fb = FileBackup(
            backup_dst=dst,
            template_filename=template,
            chdir=chdir,
            compression=compress,
            format=archive_format,
            exclude=exclude,
            exclude_pattern=exclude_pattern,
            follow_symlinks=follow_symlinks,
        )
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
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option("-h", "--host", default="localhost", show_default=True, help="MySQL host.")
@click.option("-P", "--port", default=3306, show_default=True, type=int, help="MySQL port.")
@click.option(
    "--options",
    default="--single-transaction,--databases",
    show_default=True,
    help="Comma-separated mysqldump options.",
)
@click.option("--full/--no-full", default=False, help="Add --all-databases and dump everything.")
@click.option(
    "-j",
    "--jobs",
    default=1,
    show_default=True,
    type=int,
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
    if full:
        opts = ["--all-databases"]
    else:
        opts = options.split(",")

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
        result = mb.backup(database if not full else None)
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
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option(
    "-b",
    "--binary",
    default="xtrabackup",
    show_default=True,
    help="Backup binary (xtrabackup or mariabackup).",
)
@click.option("--compress", default="zstd", show_default=True, help="Compression algorithm.")
@click.option(
    "--compress-threads",
    default=4,
    show_default=True,
    type=int,
    help="Number of compression threads.",
)
@click.option(
    "--parallel",
    default=1,
    show_default=True,
    type=int,
    help="Number of copy threads.",
)
@click.option(
    "--throttle",
    default=None,
    type=int,
    help="Limit I/O to this many IOPS.",
)
@click.option("--retention", default=5, show_default=True, type=int, help="Retention in days.")
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

    def _run() -> None:
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
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option(
    "-b",
    "--binary",
    default="xtrabackup",
    show_default=True,
    help="Backup binary (xtrabackup or mariabackup).",
)
@click.option("--compress", default="zstd", show_default=True, help="Compression algorithm.")
@click.option(
    "--compress-threads",
    default=4,
    show_default=True,
    type=int,
    help="Number of compression threads.",
)
@click.option(
    "--parallel",
    default=1,
    show_default=True,
    type=int,
    help="Number of copy threads.",
)
@click.option(
    "--throttle",
    default=None,
    type=int,
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

    def _run() -> None:
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
@click.option("--retention", default=5, show_default=True, type=int, help="Retention in days.")
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
    removed = xb.prune()
    click.echo(f"Pruned {len(removed)} backup directories")
    return EXIT_OK


@xtrabackup.command(name="prepare")
@click.argument("target", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-u", "--user", default="xtrabackup", show_default=True, help="MySQL user.")
@click.option(
    "-p",
    "--password",
    default=None,
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
    user: str,
    password: str | None,
    binary: str,
) -> int:
    """Run --prepare on a backup directory to make it restorable."""
    xb = XtraBackup(
        backup_root=target.parent,
        user=user,
        password=password,
        binary=binary,
    )
    return _handle_errors(lambda: (xb.prepare(target), click.echo(f"Prepared: {target}")))


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
    help="Resolve template paths against this directory (legacy).",
)
def restore(
    archive: Path,
    dst: Path,
    chdir: Path | None,
) -> int:
    """Extract a file backup archive to a destination directory."""
    fb = FileBackup(backup_dst=dst, chdir=chdir)
    return _handle_errors(lambda: (fb.restore(dst, archive), click.echo(f"Restored to: {dst}")))


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

    logger = logging.getLogger("bdbackup.cli")
    failed = 0
    for name in names:
        job = config.get(name)
        backend = build_backend(job)
        logger.info("Running job %r (%s)", name, job.type)
        try:
            result = backend.backup()
            if verify:
                backend.verify(result)
            click.echo(f"Job {name}: {result.path}")
        except (BackupError, subprocess.CalledProcessError) as exc:
            logger.error("Job %r failed: %s", name, exc)
            failed += 1

    return EXIT_BACKUP_FAILED if failed else EXIT_OK


# Backwards-compatible single command alias.
@main.command()
@click.argument("mode", type=click.Choice(["full", "incremental"], case_sensitive=False))
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
    help="MySQL password. If given without a value, you are prompted securely.",
)
@click.option(
    "-b",
    "--binary",
    default="xtrabackup",
    show_default=True,
    help="Backup binary (xtrabackup or mariabackup).",
)
@click.option("--compress", default="zstd", show_default=True, help="Compression algorithm.")
@click.option(
    "--compress-threads",
    default=4,
    show_default=True,
    type=int,
    help="Number of compression threads.",
)
@click.option(
    "--parallel",
    default=1,
    show_default=True,
    type=int,
    help="Number of copy threads.",
)
@click.option(
    "--throttle",
    default=None,
    type=int,
    help="Limit I/O to this many IOPS.",
)
@click.option("--retention", default=5, show_default=True, type=int, help="Retention in days.")
@click.option(
    "--verify/--no-verify",
    default=True,
    show_default=True,
    help="Verify the backup after creation.",
)
def xtrabackup_legacy(
    mode: str,
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
    """Legacy command: same as 'bdbackup xtrabackup full|incremental'."""
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

    def _run() -> None:
        if mode == "full":
            result = xb.full_backup()
        else:
            result = xb.incremental_backup()
        if verify:
            xb.verify(result)
        click.echo(f"Backup written to: {result.path}")

    return _handle_errors(_run)


if __name__ == "__main__":
    sys.exit(main())
