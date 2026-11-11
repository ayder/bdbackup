"""Opt-in disposable test of the pgbackrest engine against real pgBackRest and PostgreSQL.

Variants: `pg16-2.58` (PostgreSQL 16, pgBackRest 2.58.0) and `pg18-2.60` (PostgreSQL 18,
pgBackRest 2.60.0). Requires Docker and the variant's image, built from
scripts/docker/pgbackrest.Dockerfile with the command in tests/AGENTS.md. Building needs the
network; this run does not. Its containers sit on an internal network, and the container that
restores has no network at all. Only task-created containers, the network and the temporary
directory are touched or removed.

A primary, a streaming standby and a repository host run the pgBackRest TLS server with a CA
made for this run. Fulls are taken from the standby (backup-standby=y) into a zstd, bundled
repository: a host directory mounted at the same path in the repository container. bdbackup
runs in this process, and the pgbackrest commands it runs go to the repository container.

The restore runs the pgbackrest command bdbackup printed, then its pre-start step, its
`postgres -C` check and its pg_ctl start, each with only the placeholders filled in, in a
fresh container that holds decoy pgBackRest configs.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from click.testing import CliRunner

from bdbackup.cli import main as cli
from bdbackup.history import History, HistorySettings
from bdbackup.postgres.restore import NO_CONFIG, PGDATA

VARIANTS = {
    "pg16-2.58": ("bdbackup-pgbackrest:16-2.58.0", "16", "2.58.0"),
    "pg18-2.60": ("bdbackup-pgbackrest:18-2.60.0", "18", "2.60.0"),
}
STANZA = "demo"
TLS = "/etc/pgbackrest/tls"
RESTORED = "/var/lib/postgresql/restored"  # <PGDATA> in the restore container
CONTROL = "/var/lib/postgresql/control"  # <PGDATA> of the control runs, which must fail
SERVER_LOG = "/var/lib/postgresql/restored.log"
FREE_PORT = "5433"  # <free port> in the printed pg_ctl line; nothing listens on it in there
READY = "/etc/pgbackrest/ready"  # the restore container has installed its decoys
SUCCESS = "Real pgBackRest export and restore passed"

# Taken before bdbackup's subprocess.run is patched: the patch replaces the attribute of the
# subprocess module itself, and the docker calls of this script must not be routed.
_run = subprocess.run


class _Captured(logging.Handler):
    """Keeps bdbackup's log lines; the CLI reports a refusal there, not in its output."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        line = self.format(record)
        self.lines.append(line)
        print(f"  log: {line}", flush=True)


def _tls_client(prefix, cert):
    return [
        f"{prefix}-type=tls",
        f"{prefix}-ca-file={TLS}/ca.crt",
        f"{prefix}-cert-file={TLS}/{cert}.crt",
        f"{prefix}-key-file={TLS}/{cert}.key",
    ]


def _tls_server(host, clients):
    return [
        "tls-server-address=*",
        f"tls-server-ca-file={TLS}/ca.crt",
        f"tls-server-cert-file={TLS}/{host}.crt",
        f"tls-server-key-file={TLS}/{host}.key",
        *(f"tls-server-auth={client}={STANZA}" for client in clients),
    ]


def repository_config(repo, pgdata):
    lines = [
        "[global]",
        f"repo1-path={repo}",
        "repo1-retention-full=9",
        "compress-type=zst",
        "repo1-bundle=y",
        "backup-standby=y",
        "start-fast=y",
        "log-level-console=info",
        "log-level-file=off",
        *_tls_server("pg-repo", ["pg-primary", "pg-standby"]),
        "",
        f"[{STANZA}]",
    ]
    for index, host in enumerate(("pg-primary", "pg-standby"), 1):
        lines += [f"pg{index}-host={host}", *_tls_client(f"pg{index}-host", "pg-repo"),
                  f"pg{index}-path={pgdata}"]
    return "\n".join(lines) + "\n"


def database_config(host, pgdata):
    return "\n".join([
        "[global]",
        "repo1-host=pg-repo",
        *_tls_client("repo1-host", host),
        "compress-type=zst",
        "log-level-file=off",
        *_tls_server(host, ["pg-repo"]),
        "",
        f"[{STANZA}]",
        f"pg1-path={pgdata}",
    ]) + "\n"


def decoy_config(name):
    """A stanza of the same name that points elsewhere, as a restoring host's own config might."""
    return f"[{STANZA}]\nrepo1-path=/decoy/{name}-repo\npg1-host=decoy-{name}-host\n"


INITDB = f"""\
cat >> "$PGDATA/pg_hba.conf" <<'EOF'
host replication all all trust
EOF
cat >> "$PGDATA/postgresql.conf" <<'EOF'
wal_level = replica
archive_mode = on
archive_command = 'pgbackrest --stanza={STANZA} archive-push %p'
max_wal_senders = 10
EOF
"""

CERTIFICATES = """\
set -e
cd /tls
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=bdbackup-test-ca \
    -keyout ca.key -out ca.crt 2>/dev/null
for host in pg-primary pg-standby pg-repo; do
    openssl req -new -newkey rsa:2048 -nodes -subj "/CN=$host" \
        -keyout "$host.key" -out "$host.csr" 2>/dev/null
    printf 'subjectAltName=DNS:%s\\n' "$host" > "$host.ext"
    openssl x509 -req -in "$host.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 2 \
        -extfile "$host.ext" -out "$host.crt" 2>/dev/null
done
rm -f ./*.csr ./*.ext ca.key ca.srl
"""


def install_config(host):
    """Shell that installs this run's TLS files and the host's config, owned by postgres."""
    return (
        f"install -d -o postgres -g postgres -m 700 {TLS}"
        f" && install -o postgres -g postgres -m 600 /setup/tls/* {TLS}/"
        f" && install -o postgres -g postgres -m 600 /setup/{host}.conf"
        " /etc/pgbackrest/pgbackrest.conf"
    )


def info_entries(text):
    """Parse an info file's `[section]` / `key=JSON` lines without bdbackup's parser."""
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif "=" in line and current is not None:
            key, _, value = line.partition("=")
            current[key] = json.loads(value)
    return sections


def main(variant):
    if variant not in VARIANTS:
        raise SystemExit(f"usage: {sys.argv[0]} {{{' | '.join(VARIANTS)}}}")
    image, major, version = VARIANTS[variant]
    docker_binary = shutil.which("docker")
    if docker_binary is None:
        raise SystemExit("Docker is required")
    prefix = "bdbackup-pgbackrest-" + uuid4().hex[:12]
    network = prefix + "-net"
    primary, standby, repository = (prefix + "-primary", prefix + "-standby", prefix + "-repo")
    restorer = prefix + "-restore"
    containers = [restorer, standby, primary, repository]
    uid = os.getuid()

    def docker(*args, check=True):
        result = _run([docker_binary, *args], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(
                f"docker {' '.join(args[:4])} exited {result.returncode}:\n"
                f"{result.stdout[-6000:]}{result.stderr[-6000:]}"
            )
        return result

    def sql(container, query, port="5432"):
        return docker("exec", "-u", "postgres", container, "psql", "-X", "-At", "-p", port,
                      "-v", "ON_ERROR_STOP=1", "-c", query).stdout.strip()

    def pgbackrest(*args, check=True):
        return docker("exec", "-u", "postgres", repository, "pgbackrest", *args, check=check)

    def wait_for(what, check, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if check():
                    return
            except RuntimeError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"timed out waiting for {what}")
            time.sleep(1)

    def ready(container):
        # TCP only: the image's temporary init server listens on no address.
        return docker("exec", container, "pg_isready", "-q", "-U", "postgres", "-h", "127.0.0.1",
                      check=False).returncode == 0

    def stanza_info():
        document = json.loads(
            pgbackrest(f"--stanza={STANZA}", "info", "--output=json").stdout
        )
        return document[0]

    def labels():
        return [backup["label"] for backup in stanza_info()["backup"]]

    def backup(*options):
        before = labels()
        pgbackrest(f"--stanza={STANZA}", *options, "backup")
        new = [label for label in labels() if label not in before]
        if len(new) != 1:
            raise RuntimeError(f"expected one new backup, found {new}")
        return new[0]

    def routed(cmd, **kwargs):
        """bdbackup's pgbackrest commands, run as postgres in the repository container."""
        if cmd[0] != "pgbackrest":
            raise RuntimeError(f"unexpected command from bdbackup: {cmd}")
        return _run([docker_binary, "exec", "-u", "postgres", repository, *cmd], **kwargs)

    captured = _Captured()
    captured.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    # bdbackup adds its stderr handler only when it has none; a handler made inside one
    # CliRunner call would keep writing to that call's closed stream.
    logging.getLogger("bdbackup").addHandler(captured)

    real_which = shutil.which

    def which(name, *args, **kwargs):
        """--validate looks for pgbackrest on PATH: it is on the repository container's."""
        if name == "pgbackrest":
            return f"{repository}:/usr/bin/pgbackrest"
        return real_which(name, *args, **kwargs)

    def bdbackup(*args):
        captured.lines.clear()
        with (
            patch("bdbackup.postgres.pgbackrest.subprocess.run", side_effect=routed),
            patch("bdbackup.postgres.pgbackrest.shutil.which", side_effect=which),
        ):
            result = CliRunner().invoke(cli, [str(arg) for arg in args])
        print(f"$ bdbackup {' '.join(map(str, args))}  -> exit {result.exit_code}", flush=True)
        for line in result.output.splitlines():
            print(f"  {line}", flush=True)
        if result.exception is not None and not isinstance(result.exception, SystemExit):
            raise RuntimeError(f"bdbackup raised {result.exception!r}")
        return result.exit_code, result.output + "\n".join(captured.lines)

    with tempfile.TemporaryDirectory(prefix="bdbackup-pgbackrest-") as raw:
        work = Path(raw).resolve()
        setup, repo = work / "setup", work / "repo"
        (setup / "tls").mkdir(parents=True)
        repo.mkdir()
        try:
            # --- Containers: primary, streaming standby, repository host; TLS everywhere.
            pgdata = docker("run", "--rm", "--entrypoint", "printenv", image,
                            "PGDATA").stdout.strip()
            docker("run", "--rm", "--network", "none", "--user", f"{uid}:{os.getgid()}",
                   "-e", "HOME=/tmp", "--mount", f"type=bind,src={setup / 'tls'},dst=/tls",
                   "--entrypoint", "sh", image, "-c", CERTIFICATES)
            (setup / "pg-repo.conf").write_text(repository_config(repo, pgdata))
            for host in ("pg-primary", "pg-standby"):
                (setup / f"{host}.conf").write_text(database_config(host, pgdata))
            (setup / "initdb.sh").write_text(INITDB)
            (setup / "decoy-main.conf").write_text(decoy_config("main"))
            (setup / "decoy-confd.conf").write_text(decoy_config("confd"))
            docker("network", "create", "--internal", network)
            mount_setup = ["--mount", f"type=bind,src={setup},dst=/setup,readonly"]

            def start(name, alias, script, *extra):
                docker("run", "-d", "--name", name, "--hostname", alias, "--network", network,
                       "--network-alias", alias, *mount_setup, *extra,
                       "--entrypoint", "sh", image, "-c", script)

            # The repository's OS user takes the host user's uid, so that bdbackup, running
            # here as that user, reads the repository as its owner does on a real host.
            start(repository, "pg-repo",
                  f"usermod -o -u {uid} postgres && {install_config('pg-repo')}"
                  " && exec gosu postgres pgbackrest server",
                  "--mount", f"type=bind,src={repo},dst={repo}")
            start(primary, "pg-primary",
                  f"{install_config('pg-primary')}"
                  " && cp /setup/initdb.sh /docker-entrypoint-initdb.d/"
                  " && exec docker-entrypoint.sh postgres",
                  "-e", "POSTGRES_HOST_AUTH_METHOD=trust")
            wait_for("the primary", lambda: ready(primary))
            docker("exec", "-d", "-u", "postgres", primary, "pgbackrest", "server")
            start(standby, "pg-standby",
                  f"{install_config('pg-standby')}"
                  " && until pg_isready -q -h pg-primary; do sleep 1; done"
                  ' && mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"'
                  ' && chmod 700 "$PGDATA"'
                  ' && gosu postgres pg_basebackup -h pg-primary -U postgres -D "$PGDATA"'
                  " -R -X stream -c fast"
                  " && exec gosu postgres postgres")
            wait_for("the standby", lambda: ready(standby)
                     and sql(standby, "SELECT pg_is_in_recovery()") == "t")
            docker("exec", "-d", "-u", "postgres", standby, "pgbackrest", "server")
            print(f"PostgreSQL {sql(primary, 'SHOW server_version')} primary and streaming "
                  f"standby with {pgbackrest('version').stdout.strip()} over TLS are ready",
                  flush=True)

            # --- Step 1: the stanza, and synthetic rows on the primary.
            wait_for("stanza-create", lambda: pgbackrest(
                f"--stanza={STANZA}", "stanza-create").returncode == 0, timeout=60)
            pgbackrest(f"--stanza={STANZA}", "check")
            fresh = stanza_info()["status"]
            print(f"  status before the first backup: {fresh}", flush=True)
            if fresh["code"] != 2:
                raise RuntimeError("a stanza without backups is not status 2 (no valid backups)")
            sql(primary, "CREATE TABLE audit(id int PRIMARY KEY, phase text NOT NULL); "
                         "INSERT INTO audit SELECT g, 'before-f1' "
                         "FROM generate_series(1, 1000) g")
            print("Step 1: stanza created and checked; 1000 rows before F1", flush=True)

            # --- Step 2: F1 with archive-copy, a diff, more rows, F2 with archive-copy.
            f1 = backup("--type=full", "--archive-copy")
            diff = backup("--type=diff")
            sql(primary, "INSERT INTO audit SELECT g, 'before-f2' "
                         "FROM generate_series(1001, 2000) g")
            f2 = backup("--type=full", "--archive-copy")
            sql(primary, "INSERT INTO audit SELECT g, 'after-f2' "
                         "FROM generate_series(2001, 3000) g")
            print(f"Step 2: F1 {f1}, diff {diff}, F2 {f2} taken from the standby", flush=True)
            document = json.loads(
                pgbackrest(f"--stanza={STANZA}", "info", "--output=json").stdout
            )
            entry = document[0]
            f2_json = next(b for b in entry["backup"] if b["label"] == f2)
            print(f"  info --output=json stanza keys: {sorted(entry)}", flush=True)
            print(f"  repo entries: {entry['repo']}", flush=True)
            print(f"  F2 backup keys: {sorted(f2_json)}; error={f2_json.get('error')!r}",
                  flush=True)
            print(f"  F2 database {f2_json['database']}, db {entry['db']}", flush=True)
            if "error" not in f2_json or any("cipher" not in r for r in entry["repo"]):
                raise RuntimeError("info JSON lacks backup[].error or repo[].cipher")
            unknown = pgbackrest("--repo=1", f"--repo1-path={repo}", "--stanza=nosuch",
                                 "info", "--output=json", check=False)
            print(f"  unknown stanza: exit {unknown.returncode}, {unknown.stdout.strip()}",
                  flush=True)
            if unknown.returncode or json.loads(unknown.stdout)[0]["status"]["code"] != 1:
                raise RuntimeError("pgbackrest info no longer lists an unknown stanza as status 1")

            # --- Step 3: export F2 through bdbackup run.
            history_db, restores = work / "history.sqlite3", work / "restores"
            stage, out_dir = work / "stage" / "pg", work / "stage" / "pg" / STANZA
            stage.parent.mkdir()  # the mounted share; the stage itself is initialized
            config = work / "config.toml"
            config.write_text(
                f"[history]\ndatabase = {json.dumps(str(history_db))}\n"
                f"restore_root = {json.dumps(str(restores))}\n\n"
                f'[pg-main]\ntype = "pgbackrest"\nstanza = "{STANZA}"\n'
                f"repo_path = {json.dumps(str(repo))}\nout_dir = {json.dumps(str(out_dir))}\n\n"
                '[pg-nosuch]\ntype = "pgbackrest"\nstanza = "nosuch"\n'
                f"repo_path = {json.dumps(str(repo))}\n"
                f"out_dir = {json.dumps(str(stage / 'nosuch'))}\n\n"
                '[pg-gfs]\ntype = "gfs"\n\n'
                f"[[pg-gfs.stage]]\npaths = [{json.dumps(str(stage))}]\n"
                'period = "daily"\nkeep = "14d"\n'
            )
            code, _ = bdbackup("run", "-c", config, "-j", "pg-gfs", "--initialize-structure")
            if code:
                raise RuntimeError(f"--initialize-structure exited {code}")
            code, output = bdbackup("run", "-c", config, "--validate", "-j", "pg-main")
            if code or "[FAIL]" in output or f"[OK] pgBackRest {version}\n" not in output:
                raise RuntimeError(f"--validate of pg-main exited {code}")
            code, output = bdbackup("run", "-c", config, "--validate", "-j", "pg-nosuch")
            missing = "stanza 'nosuch': pgbackrest info reports status 1: missing stanza path"
            if code != 1 or f"[FAIL] {missing}" not in output:
                raise RuntimeError(f"--validate of the unknown stanza exited {code}")
            print("  --validate: every check OK for the stanza; the unknown stanza FAILs on its "
                  "status", flush=True)
            code, output = bdbackup("run", "-c", config, "-j", "pg-main")
            if code:
                raise RuntimeError(f"bdbackup run exited {code}")
            history = History(HistorySettings(history_db, restores))
            records = history.records(job="pg-main", successful=True)
            info = json.loads(records[0].restore_info) if len(records) == 1 else {}
            if (info.get("label"), info.get("pg_version"), info.get("pgbackrest_version")) != (
                f2, major, version
            ):
                raise RuntimeError(f"expected one record of {f2}, found {records}")
            unit = out_dir / f"{STANZA}-{f2}.tar"
            if records[0].path != str(unit) or records[0].backup_type != "pgbackrest-full":
                raise RuntimeError(f"record {records[0]} is not the unit {unit}")
            if unit.stat().st_mode & 0o777 != 0o600:
                raise RuntimeError(f"{unit} has mode {unit.stat().st_mode:o}")
            label_dir = repo / "backup" / STANZA / f2
            expected = {label_dir.relative_to(repo).as_posix()}
            expected |= {p.relative_to(repo).as_posix() for p in label_dir.rglob("*")}
            for name in (f"backup/{STANZA}/backup.info", f"archive/{STANZA}/archive.info"):
                expected |= {name, f"{name}.copy"}
            with tarfile.open(unit) as tar:
                members = tar.getmembers()
                names = [member.name for member in members]
                copied = info_entries(
                    tar.extractfile(f"backup/{STANZA}/backup.info").read().decode()
                )
            if len(names) != len(set(names)) or set(names) != expected:
                raise RuntimeError(f"tar members differ from spec 4.4: extra "
                                   f"{set(names) - expected}, missing {expected - set(names)}")
            if not all(member.isfile() or member.isdir() for member in members):
                raise RuntimeError("tar holds a member that is not a file or directory")
            if not any(f"/{f2}/bundle/" in name for name in names):
                raise RuntimeError("repo-bundle=y produced no bundle directory")
            if not {f1, diff} <= set(copied["backup:current"]):
                raise RuntimeError("the copied backup.info does not list F1 and the diff")
            print(f"Step 3: exported {unit.name} ({len(names)} members, spec 4.4 layout; "
                  f"its backup.info also lists {f1} and {diff})", flush=True)

            # --- Step 4: nothing new.
            code, output = bdbackup("run", "-c", config, "-j", "pg-main")
            if code or f"no new full: newest {f2}" not in output:
                raise RuntimeError(f"second run exited {code}: {output}")
            print("Step 4: the second run printed no new full", flush=True)

            # --- Step 5: a full without archive-copy is refused, by option-archive-copy.
            # Taken in the background, to see what info reports while a backup holds its lock.
            before = labels()
            docker("exec", "-d", "-u", "postgres", repository, "pgbackrest",
                   f"--stanza={STANZA}", "--type=full", "--no-archive-copy", "backup")
            while_held = set()
            deadline = time.monotonic() + 120
            while len(labels()) == len(before) or stanza_info()["status"]["lock"]["backup"][
                "held"
            ]:
                status = stanza_info()["status"]
                if status["lock"]["backup"]["held"]:
                    while_held.add((status["code"], status["message"]))
                if time.monotonic() > deadline:
                    raise RuntimeError("F3 did not complete")
            (f3,) = [label for label in labels() if label not in before]
            print(f"  status while F3 held the backup lock: {sorted(while_held) or 'not seen'}",
                  flush=True)
            if any(code != 0 for code, _ in while_held):
                raise RuntimeError("a running backup gives the stanza a non-zero status")
            code, output = bdbackup("run", "-c", config, "-j", "pg-main")
            if code != 1 or f"full {f3} was taken without archive-copy" not in output:
                raise RuntimeError(f"F3 run exited {code}: {output}")
            current = info_entries(
                (repo / "backup" / STANZA / "backup.info").read_text()
            )["backup:current"]
            print(f"  backup.info [backup:current] F2 keys: {sorted(current[f2])}", flush=True)
            if current[f2].get("option-archive-copy") is not True or current[f3].get(
                "option-archive-copy"
            ) is not False:
                raise RuntimeError("option-archive-copy is not true for F2 and false for F3")
            if current[f2].get("option-backup-standby") is not True:
                raise RuntimeError("F2 was not taken from the standby")
            print(f"Step 5: F3 {f3} refused; option-archive-copy is true for F2, false for F3",
                  flush=True)

            # --- Step 6: history restore and restore --archive print the same command.
            def printed(output, start):
                lines = [line for line in output.splitlines() if line.startswith(start)]
                if len(lines) != 1:
                    raise RuntimeError(f"expected one line starting {start!r} in {output}")
                return lines[0]

            def printed_command(output):
                return shlex.split(printed(output, "pgbackrest "))

            from_history, from_archive = work / "restore-history", work / "restore-archive"
            code, output = bdbackup("restore", "--config", config, "--backup-id",
                                    records[0].id, "--yes", "--dst", from_history)
            if code:
                raise RuntimeError(f"history restore exited {code}")
            command = printed_command(output)
            if any(argument.startswith("--archive-mode") for argument in command):
                raise RuntimeError("the printed command still has --archive-mode")
            steps = [printed(output, start) for start in ("printf ", "postgres -D ", "pg_ctl -D ")]
            code, output = bdbackup("restore", "--archive", unit, "--dst", from_archive)
            if code:
                raise RuntimeError(f"restore --archive exited {code}")
            other = printed_command(output)
            if [printed(output, start) for start in ("printf ", "postgres -D ", "pg_ctl -D ")
                ] != steps:
                raise RuntimeError("the two restores print different pre-start steps")

            def without_repo(arguments, directory):
                if f"--repo1-path={directory}" not in arguments:
                    raise RuntimeError(f"{arguments} does not name {directory}")
                return [a for a in arguments if a != f"--repo1-path={directory}"]

            if without_repo(command, from_history) != without_repo(other, from_archive):
                raise RuntimeError(f"the two restores print different commands: {other}")
            print("Step 6: restore --archive printed the history restore's command "
                  "but for --repo1-path", flush=True)

            # --- Step 7: the printed command in a fresh container with decoy configs.
            docker("run", "-d", "--name", restorer, "--network", "none", *mount_setup,
                   "--mount", f"type=bind,src={from_history},dst={from_history},readonly",
                   "--entrypoint", "sh", image, "-c",
                   f"usermod -o -u {uid} postgres"
                   " && install -d -m 755 /etc/pgbackrest/conf.d"
                   " && install -m 644 /setup/decoy-main.conf /etc/pgbackrest/pgbackrest.conf"
                   " && install -m 644 /setup/decoy-confd.conf"
                   " /etc/pgbackrest/conf.d/decoy.conf"
                   f" && install -d -o postgres -g postgres -m 700 {RESTORED} {CONTROL}"
                   f" && touch {READY} && exec sleep infinity")
            wait_for("the restore container",
                     lambda: docker("exec", restorer, "test", "-f", READY,
                                    check=False).returncode == 0, timeout=60)
            if command.count(f"--pg1-path={PGDATA}") != 1:
                raise RuntimeError(f"the command has no {PGDATA} placeholder: {command}")

            def pgbackrest_restore(arguments, pg1_path):
                filled = [f"--pg1-path={pg1_path}" if a == f"--pg1-path={PGDATA}" else a
                          for a in arguments]
                unfilled = [a for a in arguments if a != f"--pg1-path={PGDATA}"]
                if [a for a in filled if a != f"--pg1-path={pg1_path}"] != unfilled:
                    raise RuntimeError("the command run differs from the printed one")
                # Console info logs the options in effect; the command itself is unchanged.
                return docker("exec", "-u", "postgres", "-e", "PGBACKREST_LOG_LEVEL_CONSOLE=info",
                              restorer, *filled, check=False)

            # Controls: without the no-config form, each decoy alone is read and redirects
            # the restore. Then the printed command, with both decoys in place.
            default_loading = [a for a in command if a not in NO_CONFIG]
            for decoy, aside in (("main", "/etc/pgbackrest/conf.d/decoy.conf"),
                                 ("confd", "/etc/pgbackrest/pgbackrest.conf")):
                docker("exec", restorer, "mv", aside, f"{aside}.aside")
                control = pgbackrest_restore(default_loading, CONTROL)
                docker("exec", restorer, "mv", f"{aside}.aside", aside)
                text = control.stdout + control.stderr
                print(f"  control without {' '.join(NO_CONFIG)}, {decoy} decoy only: "
                      f"exit {control.returncode}", flush=True)
                for line in text.strip().splitlines():
                    print(f"    {line}", flush=True)
                if control.returncode == 0 or f"decoy-{decoy}-host" not in text:
                    raise RuntimeError(f"the {decoy} decoy was not read without "
                                       f"{NO_CONFIG}; the control proves nothing")
            restored = pgbackrest_restore(command, RESTORED)
            text = restored.stdout + restored.stderr
            print(f"Step 7: {' '.join(command)}", flush=True)
            for line in text.strip().splitlines():
                print(f"    {line}", flush=True)
            if restored.returncode:
                raise RuntimeError(f"pgbackrest restore exited {restored.returncode}")

            # --- Step 9 (checked before the start): the decoys were not read.
            begin = [line for line in text.splitlines() if "restore command begin" in line]
            if len(begin) != 1 or f"--repo1-path={from_history}" not in begin[0].split():
                raise RuntimeError("pgbackrest did not log the repository path it used")
            if "decoy" in text:
                raise RuntimeError("the decoy config reached the restore")

            # --- Step 8: the printed pre-start step and check, then the printed pg_ctl start.
            for name in ("standby.signal", "recovery.signal"):
                if docker("exec", restorer, "test", "-e", f"{RESTORED}/{name}",
                          check=False).returncode == 0:
                    raise RuntimeError(f"the restore left {name} in {PGDATA}")
            pre_start, check, start = steps
            if pre_start.count(PGDATA) != 1 or check.count(PGDATA) != 1 or (
                start.count(PGDATA) != 1 or start.count("<free port>") != 1
            ):
                raise RuntimeError(f"unexpected placeholders in the printed steps: {steps}")
            pre_start = pre_start.replace(PGDATA, RESTORED)
            check = check.replace(PGDATA, RESTORED)
            start = start.replace(PGDATA, RESTORED).replace("<free port>", FREE_PORT)
            print(f"  $ {pre_start}", flush=True)
            docker("exec", "-u", "postgres", restorer, "sh", "-c", pre_start)
            auto = docker("exec", restorer, "cat", f"{RESTORED}/postgresql.auto.conf").stdout
            print("  restored postgresql.auto.conf after the step:", flush=True)
            for line in auto.splitlines():
                print(f"    {line}", flush=True)
            before_start = docker("exec", "-u", "postgres", restorer,
                                  *shlex.split(check)).stdout.strip()
            print(f"  $ {check}\n    {before_start}", flush=True)
            if before_start != "off":
                raise RuntimeError(f"STOP: {check} printed {before_start!r} before the start")
            command_before = docker("exec", "-u", "postgres", restorer, "postgres", "-D",
                                    RESTORED, "-C", "archive_command").stdout.strip()
            if command_before != "":
                raise RuntimeError(f"archive_command is {command_before!r} before the start")
            print(f"  $ {start}", flush=True)
            # The server keeps pg_ctl's output streams open; send them to a file, or docker exec
            # would wait for the server to exit.
            started = docker("exec", "-u", "postgres", restorer, "sh", "-c",
                             f"{start} > {SERVER_LOG} 2>&1", check=False)
            if started.returncode:
                log = docker("exec", restorer, "cat", SERVER_LOG, check=False).stdout
                raise RuntimeError(f"PostgreSQL did not start on the restore:\n{log[-6000:]}")

            def restored_sql(query):
                return sql(restorer, query, port=FREE_PORT)

            rows = restored_sql("SELECT phase || '=' || count(*) FROM audit "
                                "GROUP BY phase ORDER BY phase").splitlines()
            source_mode = sql(standby, "SHOW archive_mode")
            archive_mode = restored_sql("SHOW archive_mode")
            # SHOW prints "(disabled)" for archive_command whenever archive_mode is off;
            # pg_file_settings has the value the server applied, and where it came from.
            archive_command = restored_sql("SHOW archive_command")
            applied = restored_sql(
                "SELECT '[' || setting || '] ' || sourcefile FROM pg_file_settings "
                "WHERE name = 'archive_command' AND applied"
            )
            in_recovery = restored_sql("SELECT pg_is_in_recovery()")
            print(f"Step 8: restored rows {rows}; archive_mode {archive_mode}; archive_command "
                  f"shows {archive_command!r}, applied {applied!r}; in recovery {in_recovery} "
                  f"(the standby that was backed up has archive_mode {source_mode})", flush=True)
            if rows != ["before-f1=1000", "before-f2=1000"]:
                raise RuntimeError(f"restored rows {rows}: expected F2's rows only")
            if source_mode != "on":
                raise RuntimeError("the backed-up cluster did not have archive_mode on")
            if archive_mode != "off":
                raise RuntimeError(f"STOP: SHOW archive_mode returned {archive_mode!r} after the "
                                   "printed step, not off")
            if archive_command != "(disabled)" or applied != (
                f"[] {RESTORED}/postgresql.auto.conf"
            ):
                raise RuntimeError(f"archive_command shows {archive_command!r}, applied "
                                   f"{applied!r}: not the empty value from postgresql.auto.conf")
            if in_recovery != "f":
                raise RuntimeError("the restored cluster is still in recovery")
            print(f"Step 9: the restore used --repo1-path={from_history}; neither "
                  "decoy config was read", flush=True)
            print(SUCCESS, flush=True)
        except BaseException:
            for container in containers:
                logs = docker("logs", "--tail", "40", container, check=False)
                if logs.returncode == 0:
                    print(f"--- last log lines of {container}\n{logs.stdout}{logs.stderr}",
                          flush=True)
            raise
        finally:
            for container in containers:
                docker("rm", "-f", "-v", container, check=False)
            docker("network", "rm", network, check=False)
            logging.getLogger("bdbackup").removeHandler(captured)
            print("Removed disposable pgBackRest-test containers and network", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
