"""Test an installed bdbackup wheel against a loopback sshd in isolated containers.

Build both candidate images, then run the gate:

    .venv/bin/python scripts/integration_ssh_copy.py --build-probe-images
    .venv/bin/python scripts/integration_ssh_copy.py

Rerun one case with:

    .venv/bin/python scripts/integration_ssh_copy.py --ubuntu 24.04 --case checksum

Use --help to list cases, --source-hash to print the wheel source fingerprint,
and --probe all to repeat the prerequisite matrix independently of the gate.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager, nullcontext, suppress
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = {"22.04": "legacy SCP", "24.04": "SFTP"}
CASES = (
    "rsync", "scp", "existing-name", "publish-race", "checksum", "unreachable",
    "unknown-host", "refused-key", "session-stop", "session-stall", "killed-transfer",
)
COLLISIONS = ("existing file", "existing directory", "dangling symlink", "after precheck")
# The Ubuntu sshd login sessions can retain these masks.
UMASKS = (0o022, 0o077)
SOURCE_MODES = (0o600, 0o640)
PORT = 22222
USER = "bdprobe"
BD_BACKUP = "/opt/bdbackup/bin/bdbackup"
COPY_SUCCESS = "Real SSH backup copies passed"
JOB_DEADLINE = 90


def source_hash(root=ROOT):
    digest = hashlib.sha256()
    package = root / "bdbackup"
    sources = sorted((*package.rglob("*.py"), *package.rglob("*.sh")))
    for path in [root / "pyproject.toml", *sources]:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def expect(check, condition, detail=""):
    if not condition:
        raise AssertionError(f"{check} failed" + (f"\n{detail}" if detail else ""))
    print(f"PASS: {check}", flush=True)


def verify(ubuntu, report):
    versions = report.get("versions", {})
    for program in ("client ssh", "server ssh", "client rsync", "server rsync"):
        expect(f"Ubuntu {ubuntu}: {program} version observed", bool(versions.get(program)))

    names = report["publish"]
    expect("free name: link created and temporary name removed", names["free name"] == (0, True))
    for case in COLLISIONS:
        expect(f"{case}: rejected with source and destination intact", names[case] == (1, True))

    for method, protocol in (("rsync", "rsync"), ("scp", FIXTURES[ubuntu])):
        for umask in UMASKS:
            for source in SOURCE_MODES:
                observed, before, after = report["modes"][method, umask, source]
                label = f"{protocol} with umask {umask:03o}, source {source:04o}"
                expect(f"{label}: observed protocol", observed == protocol)
                expect(f"{label}: staged permissions capped", after == before & source)


def capture(*argv, check=True):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)  # noqa: S603
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()}")
    return result


def wait_for_server(process, log):
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"fixture sshd did not start: {log.read_text()}")


@contextmanager
def server(work, umask):
    config = work / "sshd_config"
    config.write_text(
        f"ListenAddress 127.0.0.1\nPort {PORT}\nHostKey {work}/host\n"
        f"PidFile {work}/pid\nAuthorizedKeysFile {work}/authorized_keys\n"
        f"AllowUsers {USER}\nAuthenticationMethods publickey\n"
        "UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "PermitRootLogin no\nPermitUserRC no\n"
        f"Subsystem sftp internal-sftp -u {umask:03o}\n"
    )
    log = work / "sshd.log"
    with log.open("w") as errors:
        with subprocess.Popen(  # noqa: S603
            ("/usr/sbin/sshd", "-D", "-e", "-f", config), stderr=errors, umask=umask,
        ) as process:
            try:
                wait_for_server(process, log)
                yield process
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def prepare(work):
    work.chmod(0o755)
    for name in ("host", "client"):
        capture("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / name)
    shutil.copyfile(work / "client.pub", work / "authorized_keys")
    (work / "authorized_keys").chmod(0o644)
    (work / "known_hosts").write_text(f"[127.0.0.1]:{PORT} {(work / 'host.pub').read_text()}")
    config = work / "client_config"
    config.write_text(
        f"Host probe\n  HostName 127.0.0.1\n  Port {PORT}\n  User {USER}\n"
        f"  IdentityFile {work}/client\n  UserKnownHostsFile {work}/known_hosts\n"
        "  GlobalKnownHostsFile /dev/null\n  IdentityAgent none\n  IdentitiesOnly yes\n"
        "  StrictHostKeyChecking yes\n  BatchMode yes\n  ConnectTimeout 5\n"
    )
    dest = work / "destination"
    dest.mkdir()
    shutil.chown(dest, user=USER)
    return config, dest


def versions(config):
    handshake = capture("ssh", "-v", "-F", config, "probe", "true").stderr
    server_version = next(
        (line.split("remote software version ", 1)[1] for line in handshake.splitlines()
         if "remote software version " in line),
        "",
    )
    return {
        "client ssh": capture("ssh", "-V").stderr.strip(),
        "server ssh": server_version,
        "client rsync": capture("rsync", "--version").stdout.splitlines()[0],
        "server rsync": capture("ssh", "-F", config, "probe", "rsync --version")
        .stdout.splitlines()[0],
    }


def snapshot(path):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if path.is_symlink():
        payload = path.readlink()
    elif path.is_dir():
        payload = {item.name: snapshot(item) for item in path.iterdir()}
    else:
        payload = path.read_bytes()
    return info.st_ino, info.st_mode, payload


def publish_cases(config, dest):
    results = {}
    for index, case in enumerate(("free name", *COLLISIONS)):
        folder = dest / str(index)
        temp, final = folder / ".bdbackup-tmp-fixture", folder / "final"
        commands = {
            "free name": "true",
            "existing file": f"printf existing > {final}",
            "existing directory": f"mkdir {final} && printf sentinel > {final}/sentinel",
            "dangling symlink": f"ln -s -- {folder}/missing {final}",
            "after precheck": f"test ! -e {final} && test ! -L {final} && printf raced > {final}",
        }
        capture("ssh", "-F", config, "probe", f"mkdir {folder} && printf staged > {temp}")
        capture("ssh", "-F", config, "probe", commands[case])
        staged = snapshot(temp)
        original = None if case == "free name" else snapshot(final)
        link = capture("ssh", "-F", config, "probe", f"ln -T -- {temp} {final}", check=False)
        if case == "free name" and link.returncode == 0:
            capture("ssh", "-F", config, "probe", f"rm -- {temp}")
            correct = snapshot(final) == staged and not temp.exists()
        else:
            correct = snapshot(temp) == staged and snapshot(final) == original
        results[case] = link.returncode, correct
        print(f"ln -T -- {case}: exit={link.returncode} verified={correct}", flush=True)
    return results


def transfer_modes(config, dest, umask):
    result = {}
    for method in ("rsync", "scp"):
        for source in SOURCE_MODES:
            local = config.parent / f"source-{source:04o}"
            local.write_bytes(b"probe archive\n")
            local.chmod(source)
            temp = dest / f".bdbackup-tmp-{method}-{umask:03o}-{source:04o}"
            if method == "rsync":
                capture("rsync", "-e", f"ssh -F {config}", "--", local, f"probe:{temp}")
                observed = "rsync"
            else:
                output = capture("scp", "-v", "-F", config, "--", local, f"probe:{temp}").stderr
                if "Sending subsystem: sftp" in output:
                    observed = "SFTP"
                elif "Sending command: scp " in output:
                    observed = "legacy SCP"
                else:
                    raise RuntimeError(f"could not identify SCP protocol: {output}")
            before = stat.S_IMODE(temp.stat().st_mode)
            if before & ~source:
                capture("ssh", "-F", config, "probe", f"chmod {before & source:04o} {temp}")
            after = stat.S_IMODE(temp.stat().st_mode)
            result[method, umask, source] = observed, before, after
            print(
                f"{observed} umask={umask:03o} source={source:04o} "
                f"remote={before:04o} capped={after:04o}", flush=True,
            )
    return result


def probe(ubuntu):
    with tempfile.TemporaryDirectory(prefix="bdbackup-ssh-", dir="/run") as directory:
        work = Path(directory)
        config, dest = prepare(work)
        report = {"modes": {}}
        for umask in UMASKS:
            with server(work, umask):
                actual = capture("ssh", "-F", config, "probe", "umask").stdout.strip()
                expect(f"remote umask {umask:03o}", int(actual, 8) == umask)
                if umask == UMASKS[0]:
                    report["versions"] = versions(config)
                    print(f"Ubuntu {ubuntu}: {report['versions']}", flush=True)
                    report["publish"] = publish_cases(config, dest)
                report["modes"].update(transfer_modes(config, dest, umask))
        return report


# Observe client argv and break at specific remote operations. The installed CLI
# chooses the tools and flags. The shims call the actual OpenSSH and rsync binaries.
CLIENT_SHIM = """#!/opt/bdbackup/bin/python
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
argv = sys.argv[1:]
trace = Path(os.environ["SSH_COPY_TEST_TRACE"])
root = Path(os.environ["SSH_COPY_TEST_DEST"])
hook = os.environ.get("SSH_COPY_TEST_HOOK")
real = f"/usr/bin/{tool}"

def log(event):
    with trace.open("a") as stream:
        stream.write(json.dumps([tool, event, argv]) + "\\n")

log("start")
if tool == "ssh":
    command = argv[-1]
    if hook == "checksum" and command.startswith("test -f "):
        temp = Path(shlex.split(command)[2])
        if temp.parent != root or not temp.name.startswith(".bdbackup-tmp-"):
            raise RuntimeError("checksum hook path outside the fixture")
        with temp.open("r+b") as artifact:
            first = artifact.read(1)
            artifact.seek(0)
            artifact.write(bytes([first[0] ^ 0xff]))
        log("checksum-hook")
    elif hook == "publish-race" and command.startswith("ln -T -- "):
        final = Path(shlex.split(command)[4])
        if final.parent != root:
            raise RuntimeError("publish hook path outside the fixture")
        with final.open("xb") as artifact:
            artifact.write(b"racing writer")
        log("publish-hook")
    os.execv(real, [real, *argv])

result = subprocess.run([real, *argv], check=False)
log("finish")
sys.exit(result.returncode)
"""


class FileJob:
    def __init__(self, work):
        self.work = work
        self.ssh_config, self.dest = prepare(work)
        self.destination = f"ssh://{USER}@probe:{PORT}{self.dest}"
        self.database = work / "history.db"
        self.config = work / "backup.toml"
        self.bin = work / "client-bin"
        self.bin.mkdir()
        for name in ("ssh", "scp", "rsync"):
            tool = self.bin / name
            tool.write_text(CLIENT_SHIM)
            tool.chmod(0o755)
        self.runs = 0
        (work / "input").mkdir()
        self.payload = work / "input" / "payload"
        self.payload.write_bytes(b"synthetic backup contents\n")
        (work / "paths").write_text("payload\n")

    def configure(self, method, *, slow=False):
        args = "bwlimit=128\n" if slow else ""
        self.config.write_text(
            '[history]\ndatabase="history.db"\n'
            '[archive]\ntype="file"\nchdir="input"\ntemplate_filename="paths"\n'
            'backup_dst="archives/archive"\nformat="tar"\ntimestamp=true\n'
            "[archive.copy]\n"
            f'destination="{self.destination}"\n'
            f'method="{method}"\nssh_config="{self.ssh_config}"\n{args}'
        )
        if slow:
            with self.payload.open("r+b") as payload:
                payload.truncate(4 * 1024 * 1024)

    @contextmanager
    def launch(self, hook=""):
        if self.runs:
            # File job names have one-second resolution. A new run must get a new name.
            time.sleep(1.02 - time.time() % 1)
        self.runs += 1
        stem = self.work / f"run-{self.runs}"
        stdout, stderr, trace = (stem.with_suffix(f".{ext}") for ext in ("out", "err", "trace"))
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "SSH_COPY_TEST_TRACE": str(trace),
            "SSH_COPY_TEST_DEST": str(self.dest),
            "SSH_COPY_TEST_HOOK": hook,
        }
        argv = (BD_BACKUP, "run", "-c", str(self.config), "-j", "archive")
        with stdout.open("w") as out, stderr.open("w") as err:
            with subprocess.Popen(  # noqa: S603
                argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                env=env, cwd=self.work, start_new_session=True,
            ) as process:
                run = ActiveJob(process, stdout, stderr, trace)
                try:
                    yield run
                finally:
                    if process.poll() is None:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)

    def run(self, hook=""):
        with self.launch(hook) as run:
            run.wait()
            return run

    def backup(self, record_id=None):
        with closing(sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """SELECT b.id, b.path, b.checksum, b.size_bytes, b.status AS backup_status,
                          d.status, d.failure_class, d.attempt_count, d.bytes_sent,
                          d.started_at, d.ended_at
                   FROM backup_runs b JOIN deliveries d ON d.record_id = b.id
                   WHERE (? IS NULL OR b.id = ?) ORDER BY b.id DESC LIMIT 1""",
                (record_id, record_id),
            ).fetchone()
        expect("history contains the local backup and its delivery", row is not None)
        return row

    def final(self, row):
        return self.dest / Path(row["path"]).name

    def staged(self):
        return tuple(self.dest.glob(".bdbackup-tmp-*"))

    @contextmanager
    def copy_lock(self):
        lock = self.work / "history.db.copy-archive.lock"
        with lock.open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield


class ActiveJob:
    def __init__(self, process, stdout, stderr, trace):
        self.process = process
        self.stdout = stdout
        self.stderr = stderr
        self.trace = trace
        self.started = time.monotonic()

    def output(self):
        return self.stdout.read_text() + self.stderr.read_text()

    def wait(self):
        remaining = max(0, JOB_DEADLINE - (time.monotonic() - self.started))
        try:
            return self.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "bdbackup exits within 90 seconds failed\n" + self.output()
            ) from None

    def events(self):
        if not self.trace.exists():
            return []
        # A client process might still be writing the last line when we reach the barrier.
        lines = self.trace.read_text().splitlines(keepends=True)
        return [json.loads(line) for line in lines if line.endswith("\n")]

    def expect_exit(self, code):
        expect(f"bdbackup exits {code}", self.wait() == code, self.output())


def published(job, row, *, bytes_sent=None):
    final = job.final(row)
    expect("local backup remains success", row["backup_status"] == "success")
    expect("delivery is copied", row["status"] == "copied")
    expect(
        "history records bytes sent",
        row["bytes_sent"] == (row["size_bytes"] if bytes_sent is None else bytes_sent),
    )
    expect(
        "history records ordered attempt timestamps",
        bool(row["started_at"] and row["ended_at"])
        and datetime.fromisoformat(row["started_at"]) <= datetime.fromisoformat(row["ended_at"]),
    )
    expect("archive exists under its remote name", final.is_file())
    digest = capture("ssh", "-F", job.ssh_config, "probe", f"sha256sum -- {final}", check=False)
    expect("server SHA-256 equals the ledger checksum",
           digest.returncode == 0 and digest.stdout.split()[:1] == [row["checksum"]],
           digest.stderr)


def local_intact(row):
    source = Path(row["path"])
    expect(
        "local archive remains intact and recorded successful",
        row["backup_status"] == "success" and source.is_file()
        and hashlib.sha256(source.read_bytes()).hexdigest() == row["checksum"],
    )


def checked_failure(job, run, reason, *, record_id=None):
    run.expect_exit(1)
    row = job.backup(record_id)
    expect(f"history records failed ({reason})", (row["status"], row["failure_class"]) == (
        "failed", reason,
    ))
    expect(
        f"run reports failed ({reason})",
        f"Copy archive: {job.final(row).name} failed: {reason}" in run.stdout.read_text(),
        run.output(),
    )
    local_intact(row)
    return row


def check_arguments(run, method, protocol):
    calls = [args for tool, event, args in run.events() if tool == method and event == "start"]
    expect(f"installed bdbackup invokes real {method}", bool(calls), run.output())
    if method == "rsync":
        expect("product rsync transfers use --inplace", all("--inplace" in args for args in calls))
    else:
        ssh = [args for tool, event, args in run.events() if tool == "ssh" and event == "start"]
        sftp = any(args[-1] == "sftp" and "-s" in args for args in ssh)
        legacy = any(args[-1].startswith("scp ") and " -t " in args[-1] for args in ssh)
        expect(f"product scp uses {protocol}", (sftp, legacy) == (protocol == "SFTP",
                                                               protocol == "legacy SCP"))


def copy_case(job, method, protocol, umask):
    job.configure(method)
    with server(job.work, umask):
        with job.copy_lock():
            job.run().expect_exit(3)
        old = job.backup()
        expect("deferred archive starts pending", old["status"] == "pending")
        # FileBackup creates 0600 archives. Give the queued archive a group-read bit
        # so both modes are tested through the production CLI.
        Path(old["path"]).chmod(0o640)
        run = job.run()
        run.expect_exit(0)
        check_arguments(run, method, protocol)
        for row in (job.backup(), job.backup(old["id"])):
            published(job, row)
            source_mode = stat.S_IMODE(Path(row["path"]).stat().st_mode)
            remote_mode = stat.S_IMODE(job.final(row).stat().st_mode)
            expect(f"remote mode {remote_mode:04o} is no wider than local {source_mode:04o}",
                   remote_mode & ~source_mode == 0)
            prefix = (
                f"Copy archive: {job.final(row).name} -> {job.destination} "
                f"({method}, {row['size_bytes']} bytes, "
            )
            expect(f"run reports {method} copy of {job.final(row).name}",
                   any(line.startswith(prefix) for line in run.stdout.read_text().splitlines()))
        expect("successful copies leave no temporary names", not job.staged())


def existing_name_case(job):
    job.configure("scp")
    with server(job.work, 0o022):
        with job.copy_lock():
            job.run().expect_exit(3)
        old = job.backup()
        final = job.final(old)
        final.write_bytes(b"pre-existing destination")
        final.chmod(0o644)
        before = snapshot(final)
        checked_failure(job, job.run(), "name_taken", record_id=old["id"])
        expect("the conflicting remote file is unchanged byte for byte", snapshot(final) == before)

        final.write_bytes(Path(old["path"]).read_bytes())
        before = snapshot(final)
        run = job.run()
        run.expect_exit(0)
        expect("matching file reports already there", "(already there)" in run.stdout.read_text())
        published(job, job.backup(old["id"]), bytes_sent=0)
        expect("matching file never goes through scp", not any(
            tool == "scp" and event == "start" and args[-2] == old["path"]
            for tool, event, args in run.events()
        ))
        expect("matching file and its wider mode are untouched", snapshot(final) == before)
        expect("existing-name checks leave no temporary names", not job.staged())


def hooked_case(job, case):
    job.configure("scp")
    with server(job.work, 0o022):
        run = job.run(hook=case)
        reason = "checksum_mismatch" if case == "checksum" else "name_taken"
        row = checked_failure(job, run, reason)
        final = job.final(row)
        if case == "checksum":
            expect("fixture changed the staged bytes before verification",
                   any(event == "checksum-hook" for _, event, _ in run.events()))
            expect("a mismatched checksum never publishes the final name", not final.exists())
        else:
            expect("fixture created the final name after precheck",
                   any(event == "publish-hook" for _, event, _ in run.events()))
            expect("a racing file survives publication unchanged",
                   final.is_file() and final.read_bytes() == b"racing writer")
        expect("the failed copy removes its temporary name", not job.staged())


def connection_case(job, case):
    job.configure("rsync")
    if case == "unknown-host":
        (job.work / "known_hosts").write_text("")
        job.ssh_config.write_text(job.ssh_config.read_text().replace(
            "StrictHostKeyChecking yes", "StrictHostKeyChecking accept-new",
        ))
    elif case == "refused-key":
        (job.work / "authorized_keys").write_text("")
    with nullcontext() if case == "unreachable" else server(job.work, 0o022):
        run = job.run()
        checked_failure(job, run, "connect")
        expect("connection failure ends within 45 seconds", time.monotonic() - run.started < 45)
        commands = [args for tool, event, args in run.events()
                    if tool == "ssh" and event == "start"]
        expect("connection uses BatchMode and strict host key checking",
               bool(commands) and all(
                   "BatchMode=yes" in args and "StrictHostKeyChecking=yes" in args
                   for args in commands
               ))
        expect("connection failure leaves no remote files", not any(job.dest.iterdir()))
    if case == "unknown-host":
        expect("the previously unknown key was not accepted",
               not (job.work / "known_hosts").read_text())


def descendants(parent):
    """List descendants of this fixture's sshd within the container PID namespace."""
    result = []
    todo = [parent]
    while todo:
        pid = todo.pop()
        with suppress(FileNotFoundError, ProcessLookupError):
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            todo.extend(int(child) for child in children)
            result.extend(int(child) for child in children)
    return result


def signal_processes(pids, sig):
    for pid in pids:
        with suppress(ProcessLookupError):
            os.kill(pid, sig)


def wait_for_partial(job, run):
    until = run.started + 20
    while time.monotonic() < until and run.process.poll() is None:
        for path in job.staged():
            with suppress(FileNotFoundError):
                if 0 < path.stat().st_size < 4 * 1024 * 1024:
                    expect("real transfer reached a partially written staging file", True)
                    return path
        time.sleep(0.025)
    expect("real transfer reached a partially written staging file", False, run.output())


def recover(job, method, first):
    job.configure(method)
    result = job.run()
    result.expect_exit(0)
    retried = job.backup(first["id"])
    expect("the next run retries the same backup", retried["attempt_count"] == 2)
    published(job, retried)
    expect("the current run also copies its new backup", job.backup()["status"] == "copied")


def disconnected_transfer(job, case):
    stalled = case == "session-stall"
    method = "scp" if stalled else "rsync"
    job.configure(method, slow=True)
    with server(job.work, 0o022) as daemon, job.launch() as run:
        wait_for_partial(job, run)
        children = descendants(daemon.pid)
        expect("fixture found the active SSH server session", bool(children))
        start = time.monotonic()
        if stalled:
            signal_processes(children, signal.SIGSTOP)
        else:
            daemon.terminate()
            daemon.wait(timeout=5)
            signal_processes(children, signal.SIGKILL)
        try:
            until = start + 45
            while time.monotonic() < until and run.process.poll() is None:
                if any(tool == method and event == "finish" for tool, event, _ in run.events()):
                    break
                time.sleep(0.05)
            expect("interrupted transfer tool exits within 45 seconds", any(
                tool == method and event == "finish" for tool, event, _ in run.events()
            ), run.output())
            run.expect_exit(1)
            first = job.backup()
            local_intact(first)
            expect(
                "interrupted copy remains retryable",
                first["status"] == "failed" and first["failure_class"] in {"connect", "transfer"},
            )
            expect("interrupted copy never publishes the incomplete archive",
                   not job.final(first).exists())
        finally:
            if stalled:
                signal_processes(children, signal.SIGCONT)
    with server(job.work, 0o022):
        recover(job, method, first)


def killed_transfer(job):
    job.configure("rsync", slow=True)
    with server(job.work, 0o022):
        with job.launch() as run:
            leftover = wait_for_partial(job, run)
            check_arguments(run, "rsync", "")
            os.killpg(run.process.pid, signal.SIGKILL)
            run.expect_exit(-signal.SIGKILL)
        first = job.backup()
        local_intact(first)
        expect("killed CLI leaves a pending delivery with one attempt",
               (first["status"], first["attempt_count"]) == ("pending", 1))
        expect("killed CLI leaves only the staged name for this backup",
               leftover.is_file() and not job.final(first).exists())
        recover(job, "rsync", first)
        expect("the previous temporary name did not block or get removed by retry",
               leftover.is_file())


def run_inside(ubuntu, selected):
    choices = (selected,) if selected else CASES
    for case in choices:
        masks = UMASKS if case in {"rsync", "scp"} else (0o022,)
        for umask in masks:
            print(f"Ubuntu {ubuntu} {case} (umask {umask:03o})", flush=True)
            with tempfile.TemporaryDirectory(prefix="bdbackup-ssh-case-", dir="/run") as directory:
                job = FileJob(Path(directory))
                if case in {"rsync", "scp"}:
                    copy_case(job, case, FIXTURES[ubuntu], umask)
                elif case == "existing-name":
                    existing_name_case(job)
                elif case in {"checksum", "publish-race"}:
                    hooked_case(job, case)
                elif case in {"unreachable", "unknown-host", "refused-key"}:
                    connection_case(job, case)
                elif case in {"session-stop", "session-stall"}:
                    disconnected_transfer(job, case)
                else:
                    killed_transfer(job)


def image(ubuntu):
    return f"bdbackup-ssh-copy:ubuntu-{ubuntu}"


def build_probe_images():
    fingerprint = source_hash()
    with tempfile.TemporaryDirectory(prefix="bdbackup-ssh-wheel-") as directory:
        subprocess.run(  # noqa: S603
            (sys.executable, "-m", "build", "--wheel", "--outdir", directory), check=True,
            cwd=ROOT,
        )
        expect("checkout source fingerprint survived the wheel build", source_hash() == fingerprint)
        for ubuntu in FIXTURES:
            subprocess.run(  # noqa: S603
                ("docker", "build", "-f", ROOT / "scripts/docker/ssh-copy.Dockerfile",  # noqa: S607
                 "--build-arg", f"UBUNTU={ubuntu}",
                 "--build-arg", f"SOURCE_HASH={fingerprint}", "-t", image(ubuntu), directory),
                check=True,
            )


def run_integration(case=None, ubuntu=None):
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required")
    candidate = source_hash()
    runner = Path(__file__).read_bytes()
    for fixture in (FIXTURES if ubuntu is None else (ubuntu,)):
        tag = image(fixture)
        label = capture(
            docker, "image", "inspect", "--format", '{{ index .Config.Labels "bdbackup.source" }}',
            tag, check=False,
        )
        if label.returncode or label.stdout.strip() != candidate:
            raise SystemExit(
                f"the image {tag} is missing or was built from other sources than this checkout. "
                "Build it with --build-probe-images."
            )
        expect(f"Ubuntu {fixture} image has the candidate source fingerprint", True)
        name = f"bdbackup-ssh-copy-{uuid4().hex[:12]}"
        args = ("--case", case) if case else ()
        try:
            subprocess.run(  # noqa: S603
                (docker, "run", "--rm", "--network", "none", "--name", name, "-i", tag,
                 "/opt/bdbackup/bin/python", "-", "--inside-integration", fixture, *args),
                input=runner, check=True, timeout=600,
            )
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            capture(docker, "rm", "-f", name, check=False)
            raise
    if case is None and ubuntu is None:
        print(COPY_SUCCESS)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--build-probe-images", action="store_true")
    action.add_argument("--probe", choices=("all", *FIXTURES), metavar="UBUNTU")
    action.add_argument("--source-hash", action="store_true")
    action.add_argument("--inside-container", choices=FIXTURES, help=argparse.SUPPRESS)
    action.add_argument("--inside-integration", choices=FIXTURES, help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--ubuntu", choices=FIXTURES)
    args = parser.parse_args()
    if (args.case or args.ubuntu) and (args.build_probe_images or args.probe or args.source_hash):
        parser.error("--case and --ubuntu apply only to the integration run")
    if args.source_hash:
        print(source_hash())
    elif args.build_probe_images:
        build_probe_images()
    elif args.inside_container or args.inside_integration:
        if not Path("/.dockerenv").is_file():
            parser.error("this case must run inside its Docker fixture")
        if args.inside_integration:
            run_inside(args.inside_integration, args.case)
        else:
            verify(args.inside_container, probe(args.inside_container))
            print(f"Ubuntu {args.inside_container}: SSH prerequisite probe passed")
    elif args.probe:
        fixtures = FIXTURES if args.probe == "all" else (args.probe,)
        for ubuntu in fixtures:
            subprocess.run(  # noqa: S603
                ("docker", "run", "--rm", "--network", "none", "-i", image(ubuntu),  # noqa: S607
                 "/opt/bdbackup/bin/python", "-", "--inside-container", ubuntu),
                input=Path(__file__).read_bytes(), check=True,
            )
    else:
        run_integration(args.case, args.ubuntu)


if __name__ == "__main__":
    main()
