"""Command-line interface for bdbackup."""

from __future__ import annotations

from pathlib import Path

import click

from bdbackup import __version__
from bdbackup.filebackup import FileBackup
from bdbackup.mysqlbackup import MySQLBackup
from bdbackup.xtrabackup import XtraBackup


@click.group(name="bdbackup")
@click.version_option(version=__version__, prog_name="bdbackup")
def main() -> None:
    """Brain-dead backup: file archives, mysqldump and xtrabackup helpers."""


@main.command()
@click.argument("template", type=click.Path(exists=True, dir_okay=False, path_type=Path))
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
@click.option("--debug/--no-debug", default=False, help="Print every backed-up path.")
def file(
    template: Path,
    dst: Path,
    chdir: Path | None,
    compress: bool,
    exclude: tuple[str, ...],
    debug: bool,
) -> None:
    """Create a tar archive from a TEMPLATE file listing paths."""
    fb = FileBackup(
        backup_dst=dst,
        template_filename=template,
        chdir=chdir,
        compression=compress,
        exclude=exclude,
    )
    try:
        fb.backup(debug=debug)
    finally:
        fb.close_archive()
    click.echo(f"Archive created: {fb.tar_filename}")


@main.group()
def db() -> None:
    """Database backup commands."""


@db.command(name="mysqldump")
@click.argument("database")
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
def mysqldump(
    database: str,
    out_dir: Path,
    user: str,
    password: str | None,
    host: str,
    port: int,
    options: str,
) -> None:
    """Dump a MySQL database with mysqldump."""
    mb = MySQLBackup(
        out_dir=out_dir,
        user=user,
        password=password,
        host=host,
        port=port,
        options=options.split(","),
    )
    out = mb.backup(database)
    click.echo(f"Dump written to: {out}")


@db.command(name="xtrabackup")
@click.argument("mode", type=click.Choice(["full", "incremental"], case_sensitive=False))
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
def xtrabackup_cmd(
    mode: str,
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
        backup_root=backup_root,
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
