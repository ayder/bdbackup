"""Command-line interface for bdbackup."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import click
from click.core import ParameterSource

from bdbackup import __version__, copy_runner
from bdbackup.backends import BackupError, setup_logging
from bdbackup.config import Config, ConfigError, Job, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.gfs import runner as gfs_runner
from bdbackup.gfs import store as gfs_store
from bdbackup.gfs import structure as gfs_structure
from bdbackup.gfs.stages import fed_first_stages
from bdbackup.history import History, HistoryError
from bdbackup.metrics_command import run_metrics
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.postgres.pgbackrest import ExportProgress
from bdbackup.postgres.restore import describe_unit, restore_lines
from bdbackup.recovery import backup_restore_info, restore_record, suggested_destination
from bdbackup.ssh_copy import CopySettings
from bdbackup.templates import TemplateError
from bdbackup.utils import process_lock
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


def _echo(message: str = "") -> None:
    """Print one line; a reader that went away discards the rest of the output, not the run.

    On the first ``BrokenPipeError`` (``bdbackup history | head``) stdout is pointed at
    ``/dev/null``. That line, every later one and Python's flush at exit then succeed, so the
    command finishes its work and exits with the code it would have had.
    """
    try:
        click.echo(message)
    except BrokenPipeError:
        _discard_stdout()


def _discard_stdout() -> None:
    """Point stdout's file descriptor at ``/dev/null``; a stream without one is left alone."""
    try:
        descriptor = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):  # a captured stream, or no stdout
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, descriptor)
    finally:
        os.close(devnull)


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


def _tracked_backup(
    config, name, backup_type, action, restore_info=None, *,
    encrypted=False, copy: CopySettings | None = None,
):
    if config.history:
        return History(config.history).run(
            name, backup_type, action, restore_info, encrypted=encrypted, copy=copy,
        )
    return action()


def _run_copy(config: Config, job: Job) -> int:
    if job.copy is None:
        return EXIT_OK
    return copy_runner.run_job(History(config.history), job.name, job.copy, _echo)


def _protect_history(backend, config):
    if config.history and isinstance(backend, FileBackup):
        database = config.history.database
        excluded = {Path(str(database) + suffix) for suffix in ("", "-journal", "-wal", "-shm")}
        if backend.tar_filename.resolve() in excluded:
            raise BackupError("Archive destination conflicts with the history database")
        backend.exclude.update(excluded)


def _now() -> datetime:
    """The current time, UTC-aware; a function so tests can set the clock."""
    return datetime.now(UTC)


def _handle_errors(func, *args, **kwargs) -> int:
    """Wrap a command body in the exit-code contract.

    Returns the body's own exit code when it returns one, else ``EXIT_OK``.
    """
    try:
        code = func(*args, **kwargs)
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
    return code if isinstance(code, int) else EXIT_OK


def _dry_copy(job: Job) -> None:
    if job.copy is not None:
        _echo(f"Dry run; configured copy: {job.copy.destination} ({job.copy.method})")


def _dry_run(config: Config, job: Job, incremental: bool) -> None:
    """Report what a run of ``job`` would do; run no tool and write nothing."""
    backend = build_backend(job)
    if isinstance(backend, FileBackup):
        _protect_history(backend, config)
        backend.backup(dry_run=True)
        _echo(f"Dry run; would create: {backend.tar_filename}")
    elif isinstance(backend, MySQLBackup):
        _echo(f"Dry run; would dump {backend.selection()} into {backend.out_dir}")
    elif isinstance(backend, XtraBackup) and incremental:
        _echo(f"Dry run; would take an incremental of {backend.incremental_parent()} "
                   f"under {backend.backup_root}")
    elif isinstance(backend, XtraBackup):
        _echo(f"Dry run; would take a full backup under {backend.backup_root}")
    else:
        raise click.UsageError(f"--dry-run is not supported for job type {job.type!r}")
    _dry_copy(job)


@dataclass
class _Outcome:
    """What a pgbackrest or gfs run decided; ``_last_run`` turns it into a ``last_runs`` row.

    A run sets ``result`` and ``facts`` where it decides them. What it leaves unset is worked
    out from the exit code and from how far an export got (``progress``), or, for a gfs run,
    from ``gfs`` and ``raised``.
    """

    result: str | None = None
    facts: dict | None = None
    progress: ExportProgress = field(default_factory=ExportProgress)
    gfs: gfs_runner.RunOutcome = field(default_factory=gfs_runner.RunOutcome)
    raised: bool = False


# The result of a failed pgbackrest run, by the step its export reached.
_FAILED_AT = {
    "destination": "destination_not_ready",
    "write": "export_failed",
    "publish": "export_failed",
    "verify": "verify_failed",
}


def _result_of(job: Job, outcome: _Outcome, code: int) -> str:
    """The ``last_runs`` result word of a run that ended with exit code ``code``."""
    if job.type == "gfs":
        if outcome.raised:
            return "error"
        if code == EXIT_OK:
            return "completed"
        if code == EXIT_LOCKED:
            return "locked" if outcome.gfs.locked else "deferred"
        return "failed"
    if outcome.result is not None:
        return outcome.result
    if code == EXIT_LOCKED:
        return "locked"
    return _FAILED_AT.get(outcome.progress.step, "failed")


@contextmanager
def _last_run(config: Config, job: Job, outcome: _Outcome):
    """Record the run in the block in ``last_runs`` when it ends, however it ends.

    The exit code is the one the block exits with (click's ``Exit``, or a ``ClickException``
    such as a usage error); an exception that escapes counts as 1. Whatever ended the block is
    raised again. A failure to record is a warning: it never changes the exit code or the output.
    """
    code = EXIT_OK
    # A pgbackrest run is refused for root so that nothing is left owned by root: no database,
    # no directory for it, and no row in one that exists.
    record = not (job.type == "pgbackrest" and os.geteuid() == 0)
    try:
        yield
    except click.exceptions.Exit as exc:
        code = exc.exit_code
        raise
    except click.ClickException as exc:
        code = exc.exit_code
        raise
    except BaseException:
        code = EXIT_BACKUP_FAILED
        outcome.raised = True
        raise
    finally:
        if record:
            try:
                History(config.history).record_last_run(
                    job.name, job.type, code, _result_of(job, outcome, code), outcome.facts
                )
            except (HistoryError, OSError) as exc:
                logging.getLogger("bdbackup.cli").warning(
                    "Could not record the last run of job %r: %s", job.name, exc
                )


def _export_facts(full, result, progress: ExportProgress, started: datetime) -> dict:
    """The facts an export leaves in ``last_runs``: its phases, label, size and the full's age."""
    phases = {"write": progress.write_seconds}
    if progress.verify_seconds is not None:
        phases["verify"] = progress.verify_seconds
    return {
        "phases": phases,
        "label": full.label,
        "bytes": result.size_bytes,
        "full_age_seconds": max(0, int((started - full.stop).total_seconds())),
    }


def _run_pgbackrest(
    config: Config, job: Job, *, verify: bool, dry_run: bool, outcome: _Outcome
) -> int:
    """Export the newest pgBackRest full of the job's stanza as one tar; return the exit code.

    A real run holds the job lock from before the full is chosen until the export is published.
    A dry run takes no lock and writes nothing. When there is no new full nothing is recorded in
    the history of backups. The result a run decides is set on ``outcome``.
    """
    engine = build_backend(job)
    engine.refuse_root()
    with nullcontext() if dry_run else process_lock(engine.lock_file):
        choice = engine.choose(History(config.history), job.name)
        full = choice.full
        if full.page_errors:
            _echo(
                f"WARNING: full {full.label} has page checksum errors; it is exported, "
                "and restoring it needs checksum-page-error=n"
            )
        if not choice.new:
            hours = max(0, int((_now() - full.stop).total_seconds() // 3600))
            message = f"no new full: newest {full.label}, {hours} hours old"
            limit = engine.max_full_age_hours
            if limit is not None and hours > limit:
                _echo(f"{message} (older than max_full_age_hours = {limit})")
                outcome.result = "full_too_old"
                return EXIT_BACKUP_FAILED
            _echo(message)
            outcome.result = "no_new_full"
            return EXIT_OK
        if dry_run:
            engine.require_marker()
            _echo(
                f"Dry run; would export {full.label} ({full.repo_bytes} bytes) "
                f"of stanza {engine.stanza} to {engine.unit_path(full.label)}"
            )
            return EXIT_OK

        info = engine.restore_info(full, engine.version())
        began = []

        def export():
            began.append(_now())
            return engine.export(full, verify=verify, progress=outcome.progress)

        result = _tracked_backup(config, job.name, "pgbackrest-full", export, info, copy=job.copy)
    outcome.result = "exported"
    outcome.facts = _export_facts(full, result, outcome.progress, began[0])
    _echo(f"Job {job.name}: {result.path}")
    return EXIT_OK


def _run_pgbackrest_job(
    config: Config, job: Job, *, verify: bool, dry_run: bool, outcome: _Outcome
) -> int:
    code = _run_pgbackrest(config, job, verify=verify, dry_run=dry_run, outcome=outcome)
    if dry_run:
        _dry_copy(job)
        return code
    if code != EXIT_OK or job.copy is None:
        return code

    # The export lock is released. Keep its facts and report any copy-phase failure.
    export_result = outcome.result
    outcome.result = "copy_failed"
    code = _run_copy(config, job)
    if code == EXIT_OK:
        outcome.result = export_result
    elif code == EXIT_LOCKED:
        outcome.result = "copy_deferred"
    return code


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
              help="Report what the job would do; run no backup tool and change nothing.")
@click.option("--validate", "validate_only", is_flag=True,
              help="Check settings, filesystem access and MySQL grants of every job, or of "
                   "--job; run nothing.")
@click.option("--initialize-structure", "initialize", is_flag=True,
              help="Create the stage directories and destination markers of a gfs job.")
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
    initialize: bool,
) -> int:
    """Run one configured job, or validate the configuration."""
    verify_given = ctx.get_parameter_source("verify") is not ParameterSource.DEFAULT
    if initialize:
        for flag, given in (("--full", full), ("--incremental", incremental),
                            ("--verify" if verify else "--no-verify", verify_given),
                            ("--dry-run", dry_run), ("--validate", validate_only)):
            if given:
                raise click.UsageError(f"--initialize-structure cannot be combined with {flag}")
        if job_name is None:
            raise click.UsageError("--initialize-structure requires --job NAME")
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
    if initialize:
        if job.type != "gfs":
            raise click.UsageError(f"--initialize-structure applies only to gfs jobs: {job.name}")
        fed = job.params["stage"][0].paths[0] in fed_first_stages(config.jobs)
        ctx.exit(gfs_structure.initialize(job, _echo, fed))
    if validate_only:
        jobs = [job] if job else list(config.jobs.values())
        if not jobs:
            raise click.UsageError("No configured jobs selected")
        lines, ok = validate_config(config, jobs)
        _echo("\n".join(lines))
        ctx.exit(EXIT_OK if ok else EXIT_BACKUP_FAILED)
    if job is None:
        raise click.UsageError("Provide --job NAME or --validate")
    if incremental and job.type != "xtrabackup":
        raise click.UsageError(f"--incremental applies only to xtrabackup jobs: {job.name}")
    logger = logging.getLogger("bdbackup.cli")
    logger.info("Running job %r (%s)%s", job.name, job.type, " as a dry run" if dry_run else "")
    if job.type == "gfs":
        outcome = _Outcome()
        with nullcontext() if dry_run else _last_run(config, job, outcome):
            code = gfs_runner.run_job(
                config, job, out=_echo, dry_run=dry_run, outcome=outcome.gfs
            )
            outcome.facts = outcome.gfs.facts  # none for a busy lock, and a raise never gets here
            ctx.exit(code if code in (EXIT_OK, EXIT_LOCKED) else EXIT_BACKUP_FAILED)
    if job.type == "pgbackrest":
        outcome = _Outcome()
        with nullcontext() if dry_run else _last_run(config, job, outcome):
            ctx.exit(_handle_errors(
                _run_pgbackrest_job, config, job, verify=verify, dry_run=dry_run, outcome=outcome
            ))
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
            encrypted=job.params.get("encrypt") is True, copy=job.copy,
        )
    except BlockingIOError as exc:
        logger.error("Job %r locked: %s", job.name, exc)
        ctx.exit(EXIT_LOCKED)
    except Exception as exc:
        logger.error("Job %r failed: %s", job.name, exc)
        ctx.exit(EXIT_BACKUP_FAILED)
    _echo(f"Job {job.name}: {result.path}")
    ctx.exit(_handle_errors(_run_copy, config, job))


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


def _echo_restore_command(directory: Path) -> None:
    """Print the pgbackrest restore command if ``directory`` holds an extracted pgBackRest unit."""
    facts = describe_unit(directory)
    if facts is not None:
        for line in restore_lines(facts, directory.expanduser().absolute()):
            _echo(line)


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
            restored = FileBackup.restore_archive(archive, dst)
            _echo(f"Restored to: {restored}")
            _echo_restore_command(restored)
            return
        if mode == "backup":
            xb = XtraBackup(backup_root=root, binary=binary or "xtrabackup",
                            encrypt_key_file=encrypt_key_file)
            _echo(f"Prepared recovery directory: {xb.prepare(backup, dst)}")
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
            _echo("Successful backups (newest first):")
            for record in records:
                _echo(
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
            _echo("Locations: " + ", ".join(
                str(p.member) for p in known[record.id] if gfs_store.place_available(p)))
        else:
            _echo(f"Backup: {record.path}")
        if dst is None and not yes:
            destination = Path(click.prompt("Restore destination", default=str(destination)))
        _echo(f"Recovery destination: {destination}")
        if not yes:
            click.confirm("Restore this backup?", abort=True)
        if encrypt_key_file is not None and not record.backup_type.startswith("xtrabackup"):
            raise click.UsageError("--encrypt-key-file requires a physical backup")
        restored = restore_record(record, destination, encrypt_key_file=encrypt_key_file,
                                  history=history,
                                  on_source=(lambda path: _echo(f"Backup: {path}"))
                                  if managed else None)
        if record.backup_type == "mysqldump":
            _echo(f"SQL recovered to: {restored / 'backup.sql'} (not imported into a server)")
        elif record.backup_type.startswith("xtrabackup"):
            _echo(f"Prepared recovery directory: {restored}")
        else:
            _echo(f"Restored to: {restored}")
        if record.backup_type == "pgbackrest-full":
            _echo_restore_command(restored)

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
            _echo(f"{record_id}: {outcome}")
            if outcome == "skipped: unexpected unit":
                unexpected.append(record_id)

    def _listing():
        config = _history_config(config_path)
        history = History(config.history)
        records = history.records(job=job, successful=successful)
        suffixes = gfs_store.listing_suffixes(history)
        known = gfs_store.places(history)
        if not records:
            _echo("No backup history found.")
        for record in records:
            available = "available" if gfs_store.available(record, known) else "unavailable"
            _echo(
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


@main.command(name="metrics")
@config_option(required=True)
def metrics_cmd(config_path: Path) -> None:
    """Write the Prometheus textfile of the jobs, the history and the pgBackRest repository.

    Read-only: it never backs up, prunes or writes the history. Run it from cron.
    """
    click.get_current_context().exit(run_metrics(config_path))


if __name__ == "__main__":
    sys.exit(main())
