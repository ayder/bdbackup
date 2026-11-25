"""Read-only preflight checks and actionable permission/grant guidance."""

from __future__ import annotations

import errno
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bdbackup.config import Config, Job, build_backend
from bdbackup.filebackup import FileBackup
from bdbackup.gfs.stages import MARKER, fed_first_stages
from bdbackup.mongodb.pbm import PBM, URI_ENV, PBMError
from bdbackup.mysql import MySQLBackup, XtraBackup
from bdbackup.mysql.helpers import mysql_cnf_file
from bdbackup.ssh_copy import CopyCommandError, CopySettings, SSHConnection


def os_user() -> str:
    """The effective user's name; ``uid <n>`` when the uid has no passwd entry."""
    uid = os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


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


# Python 3.12 raises PermissionError from Path.exists, is_dir and is_file on EACCES, and 3.14
# returns False. Every filesystem check below goes through _stat, so 3.12 to 3.14 report
# alike: ENOENT and ENOTDIR (and ELOOP, as pathlib ignored it) mean missing, EACCES unreachable,
# and any other error (on NFS ESTALE, EIO, ETIMEDOUT) is kept in ``_Entry.error`` for a FAIL line.
_MISSING = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


@dataclass(frozen=True)
class _Entry:
    """What one stat of a path found: an entry, nothing there, or no way to look."""

    info: os.stat_result | None = None
    unreachable: bool = False
    error: OSError | None = None

    @property
    def exists(self) -> bool:
        return self.info is not None

    @property
    def is_dir(self) -> bool:
        return self.info is not None and stat.S_ISDIR(self.info.st_mode)

    @property
    def is_file(self) -> bool:
        return self.info is not None and stat.S_ISREG(self.info.st_mode)

    @property
    def is_symlink(self) -> bool:
        return self.info is not None and stat.S_ISLNK(self.info.st_mode)


def _stat(path: Path, *, follow: bool = True) -> _Entry:
    """Stat ``path`` (``lstat`` when ``follow`` is false); an unexpected error is in ``error``."""
    try:
        return _Entry(os.stat(path) if follow else os.lstat(path))
    except OSError as exc:
        if exc.errno in _MISSING:
            return _Entry()
        if exc.errno == errno.EACCES:
            return _Entry(unreachable=True)
        return _Entry(error=exc)


def _unsearchable(path: Path) -> Path:
    """The first ancestor of ``path``, from the root down, that the OS user cannot search."""
    for ancestor in reversed(path.parents):
        if not _access(ancestor, os.X_OK):
            return ancestor
    return path.parent


def _unreachable(report: Report, path: Path, label: str) -> None:
    path = path.absolute()
    report.add(
        "FAIL",
        f"{label}: {path} is not reachable by OS user {os_user()}: {_unsearchable(path)} lacks "
        f"search (x) permission; see namei -l {shlex.quote(str(path))}",
    )


def _cannot_check(report: Report, path: Path, label: str, error: OSError) -> None:
    name = errno.errorcode.get(error.errno, str(error.errno))
    report.add("FAIL", f"{label}: cannot check {path}: {error.strerror or error} ({name})")


def _unreadable(report: Report, path: Path, label: str, found: _Entry) -> bool:
    """Report a path that could not be looked at as a FAIL line; true when it did."""
    if found.unreachable:
        _unreachable(report, path, label)
    elif found.error is not None:
        _cannot_check(report, path, label, found.error)
    else:
        return False
    return True


def _directory(report: Report, path: Path, label: str, *, write=False, create=False) -> None:
    path = path.expanduser().absolute()
    required = "read/write/search" if write else "read/search"
    found = _stat(path)
    if _unreadable(report, path, label, found):
        return
    if not found.exists:
        parent = path.parent
        above = _stat(parent)
        while not above.exists and above.error is None and parent != parent.parent:
            parent = parent.parent
            above = _stat(parent)
        if above.error is not None:
            _cannot_check(report, parent, label, above.error)
            return
        creatable = create and above.is_dir and _access(parent, os.W_OK | os.X_OK)
        report.add(
            "OK" if creatable else "FAIL",
            f"{label}: {path} does not exist; "
            + ("can be created automatically." if creatable else "directory is required."),
        )
        report.lines.append(f"  Required directory: mkdir -p -- {shlex.quote(str(path))}")
        report.lines.append(f"  OS user {os_user()} needs {required} permissions on {path}.")
        return
    mode = os.R_OK | os.X_OK | (os.W_OK if write else 0)
    if not found.is_dir or not _access(path, mode):
        report.add("FAIL", f"{label}: OS user {os_user()} needs {required} on directory {path}.")
    else:
        report.add("OK", f"{label}: {path} ({required} as OS user {os_user()}).")


def _file(report: Report, path: Path, label: str, *, write=False) -> None:
    found = _stat(path)
    if _unreadable(report, path, label, found):
        return
    mode = os.R_OK | (os.W_OK if write else 0)
    if not found.is_file or not _access(path, mode):
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


def _copy_executable(report: Report, tool: str) -> bool:
    found = shutil.which(tool)
    if found:
        report.add("OK", f"SSH copy local {tool} is available at {found}.")
        return True
    package = "rsync" if tool == "rsync" else "OpenSSH"
    report.add(
        "FAIL", f"SSH copy local {tool} is missing. Install {package} and put {tool} in PATH.",
    )
    return False


def _ssh_copy(report: Report, settings: CopySettings) -> None:
    ssh_available = _copy_executable(report, "ssh")
    _copy_executable(report, settings.method)
    config_readable = True
    if settings.ssh_config is not None:
        path = settings.ssh_config
        config_readable = _stat(path).is_file and _access(path, os.R_OK)
        if config_readable:
            report.add("OK", f"SSH copy ssh_config {path} is readable.")
        else:
            report.add(
                "FAIL", f"SSH copy ssh_config {path} is unreadable. "
                f"Give OS user {os_user()} read access.",
            )

    directory = settings.path
    probes = [
        (
            "destination", f"destination {directory}",
            f"test -d {directory} && test -w {directory} && test -x {directory}",
            "is a writable directory.",
            f"Create the directory {directory} and give the SSH account write and search access.",
        ),
        (
            "sha256sum", "remote sha256sum", "command -v sha256sum >/dev/null", "is available.",
            "Install GNU coreutils or BusyBox on the SSH server.",
        ),
        (
            "stat", "remote stat -c %a",
            f"test -d {directory} && stat -c %a -- {directory} >/dev/null",
            f"can read the mode of {directory}.",
            "Install GNU coreutils or BusyBox and check access to the directory.",
        ),
    ]
    if settings.method == "rsync":
        probes.append((
            "rsync", "remote rsync", "command -v rsync >/dev/null", "is available.",
            "Install rsync on the SSH server and put it in the account's PATH.",
        ))

    detected = None
    if not ssh_available or not config_readable:
        remedy = (
            "Install ssh and retry validation." if not ssh_available
            else "Make the configured SSH file readable and retry validation."
        )
        report.add("FAIL", f"SSH copy connection could not be checked. {remedy}")
    else:
        command = "; ".join(
            f"if {check}; then printf '%s\\n' {token}; fi" for token, _, check, _, _ in probes
        )
        try:
            detected = set(SSHConnection(settings).run(command).splitlines())
        except CopyCommandError as exc:
            report.add(
                "FAIL",
                f"SSH copy connection to {settings.destination} failed ({exc.failure_class}). "
                "Check network access, the known host key and key-based login for this account.",
            )
        else:
            report.add("OK", f"SSH copy connection to {settings.destination} is non-interactive.")

    for token, label, _, message, remedy in probes:
        if detected is not None and token in detected:
            report.add("OK", f"SSH copy {label} {message}")
        else:
            if detected is None:
                remedy = "Restore the SSH connection and retry validation."
            report.add("FAIL", f"SSH copy {label} could not be confirmed. {remedy}")


def _settings(job: Job) -> None:
    integer_options = {"parallel", "compress_threads", "retention_days", "throttle", "port"}
    boolean_options = {"encrypt", "follow_symlinks", "timestamp"}
    string_options = {"user", "password", "host", "binary", "compress", "database", "format"}
    list_options = {"options", "exclude", "exclude_pattern", "exclude_templates"}
    for name, value in job.params.items():
        if name == "retention_days" and (type(value) is not int or value < 0):
            raise ValueError("retention_days must be an integer >= 0")
        if name in integer_options - {"retention_days"} and (
            type(value) is not int or value < 1
        ):
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


def _metrics(report: Report, path: Path) -> None:
    """The metrics file is replaced whole, so its directory is what needs write access."""
    first = len(report.lines)
    _directory(report, path.parent, "Metrics directory", write=True)
    if not report.lines[first].startswith("[OK]"):
        return
    found = _stat(path)
    if _unreadable(report, path, "Metrics file", found):
        return
    if found.is_dir:
        report.add("FAIL", f"Metrics file: {path} is a directory; the file cannot replace it.")
    elif found.exists:
        report.add(
            "OK",
            f"Metrics file: {path} exists; it is replaced whole, so only its directory "
            "needs write access.",
        )


def _gfs(report: Report, job: Job, config_path: Path, fed: bool) -> None:
    """Check the later stages, and the first one when a pgbackrest job writes into it."""
    for stage in job.params["stage"] if fed else job.params["stage"][1:]:
        for path in stage.paths:
            found = _stat(path)
            if _unreadable(report, path, "Stage path", found):
                continue
            if not found.is_dir:
                report.add("FAIL", f"Stage path {path}: does not exist")
            elif not _access(path, os.W_OK | os.X_OK):
                report.add("FAIL", f"Stage path {path}: not writable by OS user {os_user()}")
            else:
                marker = _stat(path / MARKER)
                if _unreadable(report, path / MARKER, "Stage path", marker):
                    continue
                if not marker.is_file:
                    report.add("FAIL", f"Stage path {path}: {MARKER} missing; run bdbackup run -c "
                                       f"{config_path} -j {job.name} --initialize-structure")
                elif not _access(path / MARKER, os.W_OK):
                    report.add("FAIL", f"Stage path {path}: {MARKER} not writable by OS user "
                                       f"{os_user()}")
                else:
                    report.add("OK", f"Stage path {path}: writable, marker present and writable")


_TESTED_PBM_VERSIONS = ("2.10.0", "2.17.0")


def _pbm_version_token(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]+", value):
        return None
    return re.sub(r"^v(?=[0-9])", "", value)


def _pbm_version(report: Report, backend: PBM, on_warning: Callable[[str], None]) -> str | None:
    try:
        version = _pbm_version_token(backend.version(on_warning=on_warning).get("Version"))
        if version is None:
            raise PBMError("pbm version did not report a version")
    except PBMError as exc:
        report.add("FAIL", f"PBM version: {exc}. Install a PBM executable that matches the agents.")
        return None
    note = ""
    if version not in _TESTED_PBM_VERSIONS:
        note = f" (not a tested version: {', '.join(_TESTED_PBM_VERSIONS)})"
    report.add("OK", f"PBM {version}{note}")
    return version


def _pbm_agent_checks(
    report: Report, backend: PBM, version: str | None, on_warning: Callable[[str], None],
) -> None:
    try:
        # PBM 2.10 can open the storage when the backups section is requested.
        cluster = backend.status(cluster_only=True, on_warning=on_warning).get("cluster")
        if not isinstance(cluster, list) or any(
            not isinstance(replica, dict) or not isinstance(replica.get("nodes"), list)
            for replica in cluster
        ):
            raise PBMError("pbm status did not report the replica set nodes")
        nodes = [node for replica in cluster for node in replica["nodes"]]
        if any(not isinstance(node, dict) for node in nodes):
            raise PBMError("pbm status returned invalid agent data")
    except PBMError as exc:
        report.add("FAIL", f"PBM status: {exc}. Check that the URI reaches the replica set.")
        return
    report.add("OK", "PBM status answered.")

    failed = []
    versions = set()
    healthy_without_version = False
    for node in nodes:
        agent_version = _pbm_version_token(node.get("agent"))
        if agent_version is not None:
            versions.add(agent_version)
        if node.get("ok") is True:
            healthy_without_version |= agent_version is None
        else:
            host = node.get("host")
            failed.append(
                host if isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", host)
                else "unknown node"
            )
    healthy = len(nodes) - len(failed)
    health = f"{healthy} of {len(nodes)} OK."
    if failed:
        health += f" Not OK: {', '.join(failed)}."
    if healthy:
        report.add("OK", f"PBM agents: {health}")
    else:
        report.add(
            "FAIL", f"PBM agents: {health} Start or repair a pbm-agent and check its storage.",
        )

    if version is not None and versions == {version} and not healthy_without_version:
        report.add("OK", f"PBM command and agents use version {version}.")
    else:
        agent_versions = ", ".join(sorted(versions)) or "unavailable"
        report.add(
            "FAIL", f"PBM version agreement: CLI {version or 'unavailable'}, agents "
            f"{agent_versions}. Install matching PBM and agent versions and check agent status.",
        )


def _pbm(report: Report, backend: PBM) -> None:
    found = shutil.which(backend.binary)
    if found:
        report.add("OK", f"Executable: {found}")
    else:
        report.add(
            "FAIL", f"PBM executable {backend.binary!r} is missing or not executable. "
            "Install Percona Backup for MongoDB and put pbm in PATH or set binary.",
        )
    connection = (backend.mongodb_uri or os.environ.get(URI_ENV, "")).strip()
    if connection:
        report.add("OK", "PBM connection string is set.")
    else:
        report.add("FAIL", f"PBM connection string: set mongodb_uri or {URI_ENV}.")

    warnings: list[str] = []
    if found:
        version = _pbm_version(report, backend, warnings.append)
        if connection:
            _pbm_agent_checks(report, backend, version, warnings.append)
            try:
                storage_type = backend.storage_type(on_warning=warnings.append)
            except PBMError as exc:
                report.add("FAIL", f"PBM storage type: {exc}. Check PBM's storage configuration.")
            else:
                if storage_type == "filesystem":
                    report.add("OK", "PBM storage type: filesystem.")
                else:
                    report.add("FAIL", "PBM storage type: set PBM storage.type to filesystem.")
    for warning in dict.fromkeys(warnings):
        report.add("NOTE", warning)

    _directory(report, backend.storage_path, "PBM storage")
    marker = backend.storage_path / ".pbm.init"
    found_marker = _stat(marker)
    if not found_marker.exists and not found_marker.unreachable and found_marker.error is None:
        report.add(
            "FAIL", f"PBM storage marker: {marker} is missing. "
            "Point storage_path at PBM's initialized filesystem storage.",
        )
    else:
        _file(report, marker, "PBM storage marker")
    _directory(report, backend.out_dir, "Backup destination", write=True, create=True)


def validate_config(config: Config, jobs: list[Job]) -> tuple[list[str], bool]:
    report = Report()
    report.lines.append(
        f"Validation as OS user {os_user()}; no backup, prune or GRANT is executed."
    )
    if config.history:
        db = config.history.database
        _directory(report, db.parent, "SQLite history directory", write=True, create=True)
        found = _stat(db)
        if found.exists or found.error is not None:
            _file(report, db, "SQLite history", write=True)
        _directory(report, config.history.restore_root, "Recovery root", write=True, create=True)
    if config.metrics is not None:
        _metrics(report, config.metrics.path)
    for job in jobs:
        if not job.active:
            report.lines.append(
                f"\nJob {job.name!r} ({job.type}): inactive (active = false), not checked"
            )
            continue
        report.lines.append(f"\nJob {job.name!r} ({job.type})")
        try:
            if job.restore_root is not None:
                _directory(report, job.restore_root, "Recovery root", write=True, create=True)
            if job.copy is not None:
                _ssh_copy(report, job.copy)
            if job.type == "gfs":
                first = job.params["stage"][0].paths[0]
                _gfs(report, job, config.path, first in fed_first_stages(config.jobs))
                continue
            if job.type == "pgbackrest":
                for status, message in build_backend(job).preflight():
                    report.add(status, message)
                continue
            if job.type == "pbm":
                _pbm(report, build_backend(job))
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
                # The walk stats each root first, and stops at one it cannot look at. Those roots
                # are named here and left out: the walk below covers the others, so each still
                # gets its own line. The backend is only for this check; nothing else reads it.
                reachable = []
                for raw in backend.backup_paths:
                    root = backend._resolve_path(raw)
                    found = _stat(root, follow=backend.follow_symlinks)
                    if not _unreadable(report, root, "Backup source", found):
                        reachable.append(raw)
                backend.backup_paths = reachable
                count = 0
                for source, _ in backend._walk():
                    count += 1
                    link = _stat(source, follow=False)
                    if _unreadable(report, source, "Backup source", link):
                        continue
                    if link.is_symlink and not backend.follow_symlinks:
                        continue
                    target = _stat(source)
                    if _unreadable(report, source, "Backup source", target):
                        continue
                    mode = os.R_OK | (os.X_OK if target.is_dir else 0)
                    if not _access(source, mode):
                        report.add(
                            "FAIL", f"OS user {os_user()} cannot read/search source {source}."
                        )
                if count:
                    report.add("OK", f"Checked access to {count} selected source entries.")
                elif reachable:
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
