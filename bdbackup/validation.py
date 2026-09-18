"""Read-only preflight checks and actionable permission/grant guidance."""

from __future__ import annotations

import inspect
import os
import re
import shlex
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from bdbackup import retention
from bdbackup.config import Config, Job, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.mysql.helpers import mysql_cnf_file
from bdbackup.scheduling import os_user


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.ok = True

    def add(self, status: str, message: str) -> None:
        self.lines.append(f"[{status}] {message}")
        if status == "FAIL":
            self.ok = False


def _safe_error(exc: Exception, job: Job) -> str:
    message = str(exc)
    password = job.params.get("password")
    if isinstance(password, str) and password:
        message = message.replace(password, "<redacted>")
    return " ".join(message.splitlines())


def _access(path: Path, mode: int) -> bool:
    return os.access(path, mode, effective_ids=True)


def _directory(report: Report, path: Path, label: str, *, write=False, create=False) -> None:
    path = path.expanduser().absolute()
    required = "read/write/search" if write else "read/search"
    if not path.exists():
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        creatable = create and parent.is_dir() and _access(parent, os.W_OK | os.X_OK)
        report.add(
            "OK" if creatable else "FAIL",
            f"{label}: {path} does not exist; "
            + ("can be created automatically." if creatable else "directory is required."),
        )
        report.lines.append(f"  Required directory: mkdir -p -- {shlex.quote(str(path))}")
        report.lines.append(f"  OS user {os_user()} needs {required} permissions on {path}.")
        return
    mode = os.R_OK | os.X_OK | (os.W_OK if write else 0)
    if not path.is_dir() or not _access(path, mode):
        report.add("FAIL", f"{label}: OS user {os_user()} needs {required} on directory {path}.")
    else:
        report.add("OK", f"{label}: {path} ({required} as OS user {os_user()}).")


def _file(report: Report, path: Path, label: str, *, write=False) -> None:
    mode = os.R_OK | (os.W_OK if write else 0)
    if not path.is_file() or not _access(path, mode):
        report.add(
            "FAIL",
            f"{label}: OS user {os_user()} needs "
            f"{'read/write' if write else 'read'} access to file {path}.",
        )
    else:
        report.add("OK", f"{label}: {path} is accessible.")


def _binary(report: Report, name: str) -> None:
    found = shutil.which(name)
    if found:
        report.add("OK", f"Executable: {found}")
    else:
        report.add("FAIL", f"Required executable {name!r} is missing or not executable in PATH.")


def _settings(job: Job) -> None:
    integer_options = {"parallel", "compress_threads", "retention_days", "throttle", "jobs", "port"}
    boolean_options = {"encrypt", "follow_symlinks"}
    string_options = {"user", "password", "host", "binary", "compress", "database", "format"}
    list_options = {"options", "exclude", "exclude_pattern", "exclude_templates"}
    for name, value in job.params.items():
        if name in integer_options and (type(value) is not int or value < 1):
            raise ValueError(f"{name} must be an integer >= 1")
        if name == "port" and value > 65535:
            raise ValueError("port must be <= 65535")
        if name in boolean_options and not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        if name in string_options and not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if name in list_options and (
            not isinstance(value, list) or any(not isinstance(v, str) for v in value)
        ):
            raise ValueError(f"{name} must be an array of strings")


def _identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def _account(user: str, host: str) -> str:
    def quote(value):
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

    return f"{quote(user)}@{quote(host)}"


def _scope(database: str, table: str) -> str:
    return ".".join("*" if value == "*" else _identifier(value) for value in (database, table))


def _has_grant(grants: list[str], privilege: str, database="*", table="*") -> bool:
    # ALL PRIVILEGES covers static privileges, not dynamic BACKUP_ADMIN/SHOW_ROUTINE.
    scopes = {_scope(database, table), _scope(database, "*"), "*.*"}
    for grant in grants:
        match = re.match(r"^GRANT (.+?) ON (.+?) TO ", grant, flags=re.IGNORECASE)
        if not match:
            continue
        names, scope = match.groups()
        if scope not in scopes:
            continue
        privileges = {name.strip().upper() for name in names.split(",")}
        if privilege in privileges or (
            "ALL PRIVILEGES" in privileges and privilege not in {"BACKUP_ADMIN", "SHOW_ROUTINE"}
        ):
            return True
    return False


def _requirements(backend, version: str) -> list[tuple[str, str, str]]:
    if isinstance(backend, XtraBackup):
        names = ["RELOAD", "REPLICATION CLIENT", "PROCESS", "LOCK TABLES"]
        if backend.is_mariadb:
            names[1] = "BINLOG MONITOR"
        elif not version or not version.startswith("5."):
            names.insert(1, "BACKUP_ADMIN")
        requirements = [(name, "*", "*") for name in names]
        if "BACKUP_ADMIN" in names:
            requirements.extend(
                ("SELECT", "performance_schema", table)
                for table in (
                    "log_status",
                    "keyring_component_status",
                    "replication_group_members",
                )
            )
        return requirements
    database = backend.database or "*"
    names = ["SELECT", "SHOW VIEW"]
    options = backend.options
    for name, flag in (("TRIGGER", "triggers"), ("EVENT", "events")):
        if f"--skip-{flag}" not in options:
            names.append(name)
    if "--skip-single-transaction" in options or "--lock-tables" in options:
        names.append("LOCK TABLES")
    requirements = [(name, database, "*") for name in names]
    if "--no-tablespaces" not in options:
        requirements.append(("PROCESS", "*", "*"))
    return requirements


def _grant_help(report: Report, backend, account: str, requirements) -> None:
    report.lines.append(f"  SQL for a database administrator (not executed), account {account}:")
    grouped = defaultdict(list)
    for privilege, database, table in requirements:
        grouped[(database, table)].append(privilege)
    for (database, table), privileges in grouped.items():
        report.lines.append(
            f"  GRANT {', '.join(privileges)} ON {_scope(database, table)} TO {account};"
        )
    if isinstance(backend, XtraBackup) and not backend.is_mariadb:
        report.lines.append("  Optional, for importing individual tables:")
        report.lines.append(f"  GRANT CREATE TABLESPACE ON *.* TO {account};")
    report.lines.append(
        "  If the account does not exist, create it with your chosen authentication "
        "method first; no password is printed here."
    )


def _mysql(report: Report, backend) -> None:
    physical = isinstance(backend, XtraBackup)
    host = "localhost" if physical else backend.host
    account = _account(backend.user, "localhost" if host == "localhost" else "<client-host>")
    client = shutil.which("mariadb" if physical and backend.is_mariadb else "mysql")
    if not client:
        client = shutil.which("mysql" if physical and backend.is_mariadb else "mariadb")
    requirements = _requirements(backend, "")
    if not client:
        report.add("FAIL", "Install mysql/mariadb client to check login and SHOW GRANTS.")
        _grant_help(report, backend, account, requirements)
        return
    connection = {} if physical else {"host": backend.host, "port": backend.port}
    try:
        with mysql_cnf_file(user=backend.user, password=backend.password, **connection) as defaults:
            result = subprocess.run(  # noqa: S603
                [
                    client,
                    f"--defaults-extra-file={defaults}",
                    "--batch",
                    "--raw",
                    "--skip-column-names",
                    "--connect-timeout=5",
                    "--execute=SELECT CURRENT_USER(), VERSION(), @@datadir; SHOW GRANTS;",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        if result.returncode:
            report.add(
                "FAIL",
                f"MySQL login/grant query failed for user {backend.user!r} "
                f"(client exit {result.returncode}); check credentials and connection defaults.",
            )
            _grant_help(report, backend, account, requirements)
            return
        lines = result.stdout.splitlines()
        identity, version, datadir = lines[0].split("\t", 2)
        user, matched_host = identity.rsplit("@", 1)
        account = _account(user, matched_host)
        report.add("OK", f"MySQL authenticated as {account}; server {version}.")
        grants = lines[1:]
        requirements = _requirements(backend, version)
        missing = [entry for entry in requirements if not _has_grant(grants, *entry)]
        if missing:
            report.add(
                "FAIL",
                "Required grants not confirmed: "
                + ", ".join(
                    f"{privilege} ON {_scope(db, table)}" for privilege, db, table in missing
                ),
            )
            _grant_help(report, backend, account, missing)
        else:
            report.add("OK", "Required direct MySQL grants are present.")
        if any(g.upper().startswith("REVOKE ") for g in grants):
            report.add(
                "FAIL",
                "Partial revokes require manual privilege review; "
                "effective access cannot be confirmed.",
            )
        if any(g.upper().startswith("GRANT ") and " ON " not in g.upper() for g in grants):
            report.add(
                "INFO",
                "Role grants are not expanded; review active roles if grants "
                "above could not be confirmed.",
            )
        if physical:
            if backend.is_mariadb != ("mariadb" in version.lower()):
                report.add("FAIL", "Backup binary family does not match the MySQL/MariaDB server.")
            _directory(report, Path(datadir), "MySQL datadir")
            report.add(
                "INFO",
                "Datadir check covers directory access; external tablespaces, "
                "individual data files and server/binary version compatibility need review.",
            )
        else:
            report.add(
                "INFO",
                "mysqldump options, routine visibility and GTID settings may require "
                "additional grants (SHOW_ROUTINE or global SELECT, RELOAD/FLUSH_TABLES).",
            )
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        report.add("FAIL", f"Cannot validate MySQL login/grants ({type(exc).__name__}).")
        _grant_help(report, backend, account, requirements)


def _retention(report: Report, job: Job) -> None:
    params = dict(job.params)
    if "apply" in params:
        params["apply_changes"] = params.pop("apply")
    if "pick" in params:
        params["pick_mode"] = params.pop("pick")
    bound = inspect.signature(retention.run_job).bind(**params)
    bound.apply_defaults()
    values = bound.arguments
    for key, minimum in (
        ("daily", 1),
        ("weekly", 0),
        ("monthly", 0),
        ("incr_days", 0),
        ("log_days", 0),
        ("min_keep_fulls", 1),
    ):
        if type(values[key]) is not int or values[key] < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    if values["pick_mode"] not in {"first", "last"}:
        raise ValueError("pick must be first or last")
    if not isinstance(values["apply_changes"], bool):
        raise ValueError("apply must be true or false")
    for key in ("full_dir", "incr_dir", "log_dir"):
        if values[key] is not None:
            _directory(report, Path(values[key]), key, write=values["apply_changes"])
    report.add("INFO", "Retention mode: " + ("APPLY" if values["apply_changes"] else "DRY RUN"))
    report.add("INFO", "Retention settings/access checked; no deletion scan or deletion performed.")


def validate_config(config: Config, jobs: list[Job]) -> tuple[list[str], bool]:
    report = Report()
    report.lines.append(
        f"Validation as OS user {os_user()}; no backup, prune or GRANT is executed."
    )
    if config.history:
        db = config.history.database
        _directory(report, db.parent, "SQLite history directory", write=True, create=True)
        if db.exists():
            _file(report, db, "SQLite history", write=True)
        _directory(report, config.history.restore_root, "Recovery root", write=True, create=True)
    for job in jobs:
        report.lines.append(f"\nJob {job.name!r} ({job.type})")
        try:
            if job.type == "retention":
                _retention(report, job)
                continue
            if job.type not in {"file", "xtrabackup", "mysqldump"}:
                report.add("FAIL", f"Preflight checks are not implemented for engine {job.type!r}.")
                continue
            # Report destinations even if another constructor setting is invalid.
            destination = job.params.get("backup_root", job.params.get("out_dir"))
            if destination is not None:
                _directory(report, Path(destination), "Backup destination", write=True, create=True)
            _settings(job)
            backend = build_backend(job)
            if isinstance(backend, FileBackup):
                _directory(
                    report,
                    backend.tar_filename.parent,
                    "Archive destination",
                    write=True,
                    create=True,
                )
                if config.history and backend.tar_filename.resolve() in {
                    Path(str(config.history.database) + suffix)
                    for suffix in ("", "-wal", "-shm", "-journal")
                }:
                    report.add(
                        "FAIL", "Archive destination conflicts with the SQLite history file."
                    )
                if not backend.backup_paths:
                    report.add("FAIL", "File job needs a nonempty template_filename path list.")
                count = 0
                for source, _ in backend._walk():
                    count += 1
                    if source.is_symlink() and not backend.follow_symlinks:
                        continue
                    mode = os.R_OK | (os.X_OK if source.is_dir() else 0)
                    if not _access(source, mode):
                        report.add(
                            "FAIL", f"OS user {os_user()} cannot read/search source {source}."
                        )
                if count:
                    report.add("OK", f"Checked access to {count} selected source entries.")
                elif backend.backup_paths:
                    report.add("FAIL", "No source entries selected after exclusions.")
            elif isinstance(backend, (MySQLBackup, XtraBackup)):
                if not isinstance(backend.user, str) or not backend.user.strip():
                    raise ValueError("MySQL user must be a nonempty string")
                if isinstance(backend, MySQLBackup):
                    _binary(report, "mysqldump")
                    if not backend.database and "--all-databases" not in backend.options:
                        report.add("FAIL", "Set database or include --all-databases in options.")
                    if destination is None:
                        _directory(
                            report, backend.out_dir, "Backup destination", write=True, create=True
                        )
                else:
                    _binary(report, backend.binary)
                    if config.history and config.history.restore_root.is_relative_to(
                        backend.backup_root
                    ):
                        report.add("FAIL", "Recovery root must be outside this job's backup root.")
                    if backend.compress not in {"", "zstd", "lz4", "quicklz"}:
                        report.add("FAIL", "compress must be empty, zstd, lz4 or quicklz.")
                    if backend.compress:
                        _binary(
                            report, {"quicklz": "qpress"}.get(backend.compress, backend.compress)
                        )
                    if backend.encrypt:
                        report.add(
                            "OK",
                            f"AES256 key file is readable and 32 bytes: {backend.encrypt_key_file}",
                        )
                _mysql(report, backend)
        except Exception as exc:
            report.add("FAIL", f"Invalid settings or inaccessible path: {_safe_error(exc, job)}")
    report.lines.append(
        "\nValidation passed." if report.ok else "\nValidation failed; see requirements above."
    )
    return report.lines, report.ok
