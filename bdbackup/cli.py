"""Command-line interface for bdbackup."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from bdbackup import __version__
from bdbackup.filebackup import FileBackup
from bdbackup.mysqlbackup import MySQLBackup
from bdbackup.xtrabackup import XtraBackup


@click.group(name="bdbackup")
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
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["log_level"] = level


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
    help="Change to this directory before archiving.",
)
@click.option("-z", "--compress/--no-compress", default=False, help="Gzip-compress the archive.")
@click.option("-x", "--exclude", multiple=True, help="Path/pattern to exclude; can be repeated.")
def file(
    template: Path,
    dst: Path,
    chdir: Path | None,
    compress: bool,
    exclude: tuple[str, ...],
) -> None:
    """Create a tar archive from a template file listing paths."""
    fb = FileBackup(
        backup_dst=dst,
        template_filename=template,
        chdir=chdir,
        compression=compress,
        exclude=exclude,
    )
    try:
        fb.backup(debug=False)
    finally:
        fb.close_archive()
    click.echo(f"Archive created: {fb.tar_filename}")


@main.command()
@click.option("--database", required=True, help="Database name to dump.")
@click.option(
    "-o",
    "--out-dir",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for the dump file.",
)
@click.option("-u", "--user", default="root", show_default=True, help="MySQL user.")
@click.option("-p", "--password", default=None, help="MySQL password.")
@click.option("-h", "--host", default="localhost", show_default=True, help="MySQL host.")
@click.option("-P", "--port", default=3306, show_default=True, type=int, help="MySQL port.")
@click.option(
    "--options",
    default="--single-transaction,--databases",
    show_default=True,
    help="Comma-separated mysqldump options.",
)
@click.option("--full/--no-full", default=False, help="Add --all-databases and dump everything.")
def mysqldump(
    database: str,
    out_dir: Path,
    user: str,
    password: str | None,
    host: str,
    port: int,
    options: str,
    full: bool,
) -> None:
    """Dump a MySQL database with mysqldump."""
    opts = options.split(",")
    if full:
        opts = ["--all-databases"]
    mb = MySQLBackup(
        out_dir=out_dir,
        user=user,
        password=password,
        host=host,
        port=port,
        options=opts,
    )
    out = mb.backup(database if not full else "all-databases")
    click.echo(f"Dump written to: {out}")


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
@click.option("-p", "--password", default=None, help="MySQL password.")
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
@click.option("--retention", default=5, show_default=True, type=int, help="Retention in days.")
def xtrabackup(
    mode: str,
    database: str,
    backup_root: Path,
    user: str,
    password: str | None,
    binary: str,
    compress: str,
    compress_threads: int,
    retention: int,
) -> None:
    """Run a full or incremental xtrabackup physical backup."""
    xb = XtraBackup(
        backup_root=backup_root / database,
        user=user,
        password=password,
        binary=binary,
        compress=compress,
        compress_threads=compress_threads,
        retention_days=retention,
    )
    if mode == "full":
        target = xb.full_backup()
    else:
        target = xb.incremental_backup()
    click.echo(f"Backup written to: {target}")


if __name__ == "__main__":
    main()
