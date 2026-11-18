"""Opt-in disposable test of `bdbackup metrics` during a real NFS outage.

Requires Docker and the image built with the command in tests/AGENTS.md from
scripts/docker/nfs-outage.Dockerfile (building needs the network; this run does not). The
container is privileged, has no network but loopback, and holds everything:

- a kernel NFS server exporting a tmpfs at /srv/share, and an NFS client mount of it at
  /mnt/share, `hard` and `noac` so that a `stat` really goes to the server (NFSv3 if it
  mounts, else NFSv4);
- bdbackup, installed from a wheel built on the host from the sources this checkout holds;
- a config whose history database and first gfs stage are on local disk and whose second gfs
  stage is on the share, and whose `[metrics]` timeout_seconds is 3.

A second, self-contained scenario has its own config and history database (the outage part
never sees it): a file job feeds a three-stage gfs job whose first stage is local and whose
other two are on the share. Backdating the file backups in the history database makes a gfs run
move one unit from the local stage onto the share, which must be a copy, and a later run move
it between the two share stages, which must be a rename (same inode, the report line ends
`, renamed)`).

It takes a healthy run, whose file must also carry the new GFS series of the share stage (its
size and free space, the time of its ledger snapshot, the facts of the last run and a drift of
nothing missing and nothing unknown), and that scenario, after which the drift of the `gfs` job
must still be nothing: the scenario's directories on the share are not its entries. Then it
drops the NFS packets with iptables, and runs `bdbackup metrics` twice, each under `timeout 120`
so that a hang cannot hang this test: each must end within 3 x timeout_seconds + 5 seconds with
exit 1 (or 3), a rewritten file that reads the share's destination as 0, the destination timeout
error, no history series, no capacity or snapshot series and the GFS series. Then it removes the
rules, waits for the client to reconnect and runs once more: exit 0, share ready, capacity back.

Only this run's container is created and removed.
"""

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "bdbackup-nfs-outage:test"
SUCCESS = "Real NFS outage metrics test passed"
TIMEOUT_SECONDS = 3
BOUND = 3 * TIMEOUT_SECONDS + 5  # the longest a metrics run may take during the outage
CONFIG = f"""[history]
database = "/var/lib/bdbackup/history.sqlite3"
restore_root = "/var/lib/bdbackup/restores"

[metrics]
path = "/var/lib/metrics/bdbackup.prom"
timeout_seconds = {TIMEOUT_SECONDS}

[files]
type = "file"
template_filename = "paths"
chdir = "/var/lib/source"
backup_dst = "/var/backups/first/files"
format = "tar.gz"
timestamp = true

[gfs]
type = "gfs"

[[gfs.stage]]
paths = ["/var/backups/first"]
period = "daily"
keep = "2d"

[[gfs.stage]]
paths = ["/mnt/share/weekly"]
period = "weekly"
keep = "8w"
"""
CONFIG_PATH = "/etc/bdbackup/config.toml"
LOCAL_STAGE = "/var/backups/rename"
HOT = "/mnt/share/rename-hot"
COLD = "/mnt/share/rename-cold"
RENAME_CONFIG = f"""[history]
database = "/var/lib/bdbackup/rename.sqlite3"
restore_root = "/var/lib/bdbackup/rename-restores"

[moved]
type = "file"
template_filename = "paths"
chdir = "/var/lib/source"
backup_dst = "{LOCAL_STAGE}/moved"
format = "tar.gz"
timestamp = true

[gfs-rename]
type = "gfs"

[[gfs-rename.stage]]
paths = ["{LOCAL_STAGE}"]
period = "daily"
keep = "2d"

[[gfs-rename.stage]]
paths = ["{HOT}"]
period = "daily"
keep = "30d"

[[gfs-rename.stage]]
paths = ["{COLD}"]
period = "weekly"
keep = "52w"
"""
RENAME_CONFIG_PATH = "/etc/bdbackup/rename.toml"
RENAME_DATABASE = "/var/lib/bdbackup/rename.sqlite3"
# Run in the container with the interpreter bdbackup is installed in: the sqlite3 command line
# may be missing there. Sets the completion time of the one record of an archive to ``days``
# days ago, in the local zone the way GFS reads it, and prints the record's id.
BACKDATE = """\
import sqlite3, sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

database, path, days = sys.argv[1], sys.argv[2], int(sys.argv[3])
when = (datetime.now().astimezone() - timedelta(days=days)).astimezone(UTC).isoformat()
with closing(sqlite3.connect(database)) as db, db:
    ids = [row[0] for row in db.execute(
        "SELECT id FROM backup_runs WHERE path=? AND status='success'", (path,))]
    if len(ids) != 1:
        sys.exit(f"expected one successful record of {path}, found {ids}")
    db.execute("UPDATE backup_runs SET completed_at=? WHERE id=?", (when, ids[0]))
print(ids[0], when)
"""
# One gfs report line of a move: series, relative path, the two stage paths and the reason.
MOVE = re.compile(r"^  move (\S+) (\S+) \d{4}-\d\d-\d\d (\S+) -> (\S+) \((.*)\)$", re.MULTILINE)
METRICS_FILE = "/var/lib/metrics/bdbackup.prom"
SHARE = "/mnt/share/weekly"

# Taken first: the docker calls of this script go through it.
_run = subprocess.run

SERVER = """\
set -e
mkdir -p /srv/share /mnt/share
mount -t tmpfs -o size=64m tmpfs /srv/share
echo '/srv/share 127.0.0.1(rw,sync,no_subtree_check,no_root_squash,fsid=1)' > /etc/exports
mountpoint -q /proc/fs/nfsd || mount -t nfsd nfsd /proc/fs/nfsd
rpcbind
sleep 1
rpc.nfsd 8
rpc.mountd -p 32767
exportfs -rv
"""
SAMPLE = re.compile(r"([a-z_0-9]+)(?:\{(.*)\})? (\S+)")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def source_hash():
    """A hash of the bdbackup sources, written into the image as a label."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "bdbackup").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def moves(report):
    """The move lines of a gfs report: (series, relative path, from, to, reason)."""
    return MOVE.findall(report)


def prom_samples(text):
    samples = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, raw, value = SAMPLE.fullmatch(line).groups()
            samples.append((name, dict(LABEL.findall(raw or "")), float(value)))
    return samples


def series(samples, name, **labels):
    return [
        value for found, have, value in samples
        if found == name and all(have.get(key) == wanted for key, wanted in labels.items())
    ]


def one(samples, name, **labels):
    values = series(samples, name, **labels)
    if len(values) != 1:
        raise RuntimeError(f"expected one {name}{labels} series, found {values}")
    return values[0]


def check_share_series(samples, stamp):
    """The GFS series of the share stage of job ``gfs`` in a healthy run: its capacity, the
    time of its ledger snapshot, the facts of the last run and its drift. ``stamp`` is the
    time the metrics run ended. Returns (size, free) in bytes."""
    share = {"job_name": "gfs", "stage": "2", "path": SHARE}
    size = one(samples, "bdbackup_destination_size_bytes", **share)
    free = one(samples, "bdbackup_destination_free_bytes", **share)
    if not 0 < free <= size:
        raise RuntimeError(f"the share's capacity is not 0 < free <= size: {free}, {size}")
    for name in ("bdbackup_gfs_ledger_snapshot_timestamp_seconds",
                 "bdbackup_gfs_facts_timestamp_seconds"):
        labels = share if "snapshot" in name else {"job_name": "gfs"}
        taken = one(samples, name, **labels)
        if not stamp - 600 <= taken <= stamp + 5:
            raise RuntimeError(f"{name} is {taken}, not a recent time before {stamp}")
    for name in ("bdbackup_gfs_drift_missing_units", "bdbackup_gfs_drift_unknown_entries"):
        if one(samples, name, job_name="gfs", stage="2") != 0:
            raise RuntimeError(f"{name} of the share stage is not 0")
    # The first stage is local and not fed: its recorded units are looked at, it is not walked.
    if one(samples, "bdbackup_gfs_drift_missing_units", job_name="gfs", stage="1") != 0:
        raise RuntimeError("a unit of the first stage is missing")
    if series(samples, "bdbackup_gfs_drift_unknown_entries", job_name="gfs", stage="1"):
        raise RuntimeError("the first stage, which is not walked, has an unknown count")
    return size, free


def main():
    docker_binary = shutil.which("docker")
    if docker_binary is None:
        raise SystemExit("Docker is required")
    label = _run(
        [docker_binary, "image", "inspect", "--format",
         '{{ index .Config.Labels "bdbackup.source" }}', IMAGE],
        capture_output=True, text=True,
    )
    if label.returncode or label.stdout.strip() != source_hash():
        raise SystemExit(
            f"the image {IMAGE} is missing or was built from other sources than this checkout; "
            "build it with the command in tests/AGENTS.md"
        )
    container = "bdbackup-nfs-outage-" + uuid4().hex[:12]

    def docker(*args, check=True, timeout=300, stdin=None):
        result = _run([docker_binary, *args], capture_output=True, text=True, timeout=timeout,
                      input=stdin)
        if check and result.returncode:
            raise RuntimeError(
                f"docker {' '.join(args[:4])} exited {result.returncode}:\n"
                f"{result.stdout[-4000:]}{result.stderr[-4000:]}"
            )
        return result

    def sh(script, **kwargs):
        return docker("exec", container, "sh", "-c", script, **kwargs)

    def ps():
        listing = sh("ps -eo pid,stat,cmd | grep -v grep | grep -E 'python|bdbackup' || true")
        return listing.stdout.strip()

    def metrics(timeout=120):
        """One `bdbackup metrics` run, under ``timeout``; (exit code, seconds, stderr)."""
        started = time.monotonic()
        result = docker("exec", container, "timeout", str(timeout), "bdbackup", "metrics", "-c",
                        CONFIG_PATH, check=False, timeout=timeout + 30)
        elapsed = time.monotonic() - started
        print(f"$ bdbackup metrics -c {CONFIG_PATH}  -> exit {result.returncode} in "
              f"{elapsed:.1f} s", flush=True)
        for line in result.stderr.strip().splitlines():
            print(f"  {line}", flush=True)
        if result.stdout:
            raise RuntimeError(f"metrics printed on stdout: {result.stdout!r}")
        return result.returncode, elapsed, result.stderr

    def read_metrics():
        text = sh(f"cat {METRICS_FILE}").stdout
        return text, prom_samples(text)

    def bdbackup(*args):
        result = docker("exec", container, "bdbackup", *args, check=False)
        print(f"$ bdbackup {' '.join(args)}  -> exit {result.returncode}", flush=True)
        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"  {line}", flush=True)
        if result.returncode:
            raise RuntimeError(f"bdbackup {args[0]} exited {result.returncode}")
        return result.stdout

    def rules(action):
        """Insert (``I``) or delete (``D``) the rules that drop the NFS (2049), portmapper (111)
        and mountd (32767, pinned by the server setup) packets on loopback, both directions."""
        for chain, interface in (("INPUT", "-i lo"), ("OUTPUT", "-o lo")):
            for match in ("--dport", "--sport"):
                for port in ("2049", "111", "32767"):
                    command = (
                        f"iptables -{action} {chain} {interface} -p tcp {match} {port} -j DROP"
                    )
                    sh(command + (" 2>/dev/null || true" if action == "D" else ""))

    def probe_children():
        """The probe children still alive (a killed one lingers only as a zombie)."""
        return [line for line in ps().splitlines() if "-I -c" in line and "<defunct>" not in line]

    def inode(path):
        return sh(f"stat -c %i {path}").stdout.strip()

    def exists(path):
        return sh(f"test -e {path}", check=False).returncode == 0

    def move_once(older, source, target, backdate_days):
        """Backdate ``older`` (by its archive path in the local stage), run the gfs job and
        return its one move line; the archive must be the only thing the run moves."""
        stamped = docker("exec", container, "python", "-c", BACKDATE, RENAME_DATABASE,
                         f"{LOCAL_STAGE}/{older}", str(backdate_days))
        print(f"  backdated {older} by {backdate_days} days: record {stamped.stdout.strip()}",
              flush=True)
        report = bdbackup("run", "-c", RENAME_CONFIG_PATH, "-j", "gfs-rename")
        found = moves(report)
        if len(found) != 1 or found[0][:4] != ("moved", older, source, target):
            raise RuntimeError(f"expected one move of {older} from {source} to {target}, "
                               f"found {found}:\n{report}")
        if not re.search(r"^Summary: moved 1 ", report, re.MULTILINE):
            raise RuntimeError(f"the gfs run did not move exactly one unit:\n{report}")
        return found[0][4]

    with tempfile.TemporaryDirectory(prefix="bdbackup-nfs-outage-") as raw:
        config = Path(raw) / "config.toml"
        config.write_text(CONFIG)
        rename_config = Path(raw) / "rename.toml"
        rename_config.write_text(RENAME_CONFIG)
        started_at = time.monotonic()
        dropping = False
        try:
            docker("run", "-d", "--name", container, "--privileged", "--network", "none",
                   IMAGE, "sleep", "infinity")

            # --- 1. The server and the hard client mount.
            sh(SERVER)
            mounted = None
            for options in ("vers=3,proto=tcp,hard,noac,nolock", "vers=4.2,proto=tcp,hard,noac"):
                result = sh(f"mount -t nfs -o {options} 127.0.0.1:/srv/share /mnt/share",
                            check=False)
                if result.returncode == 0:
                    mounted = options
                    break
                print(f"  mount -o {options} failed: {result.stderr.strip()}", flush=True)
            if mounted is None:
                raise RuntimeError("the share could not be mounted over NFSv3 or NFSv4")
            line = sh("mount | grep ' /mnt/share '").stdout.strip()
            print(f"Step 1: NFS share mounted with -o {mounted}\n  {line}", flush=True)
            for option in ("hard", "noac"):
                if option not in line:
                    raise RuntimeError(f"the mount is not {option}: {line}")

            # --- 2. The config, the structure and one gfs run.
            sh("mkdir -p /etc/bdbackup /var/lib/bdbackup /var/lib/metrics /var/lib/source"
               " /var/backups/first && echo hello > /var/lib/source/hello"
               " && echo hello > /etc/bdbackup/paths")
            docker("cp", str(config), f"{container}:{CONFIG_PATH}")
            bdbackup("run", "-c", CONFIG_PATH, "-j", "gfs", "--initialize-structure")
            bdbackup("run", "-c", CONFIG_PATH, "-j", "files")
            report = bdbackup("run", "-c", CONFIG_PATH, "-j", "gfs")
            print("Step 2: structure initialized, one file backup and one gfs run recorded",
                  flush=True)
            stages = re.findall(r"Stage (.+): (\d+) units?", report)
            if stages != [("/var/backups/first", "1"), (SHARE, "0")]:
                raise RuntimeError(f"unexpected gfs report: {report}")

            # --- 3. A healthy run.
            code, elapsed, _ = metrics()
            text, samples = read_metrics()
            if code != 0:
                raise RuntimeError(f"the healthy metrics run exited {code}:\n{text}")
            if one(samples, "bdbackup_destination_ready", job_name="gfs", stage="2",
                   path=SHARE) != 1:
                raise RuntimeError("the share is not ready in the healthy run")
            if one(samples, "bdbackup_history_units", job_name="files", state="available") != 1:
                raise RuntimeError("the healthy run does not count the file backup")
            if one(samples, "bdbackup_gfs_stage_units", job_name="gfs", stage="1") != 1:
                raise RuntimeError("the healthy run does not count the unit in stage 1")
            healthy_stamp = one(samples, "bdbackup_metrics_last_run_timestamp_seconds")
            size, free = check_share_series(samples, healthy_stamp)
            print(f"Step 3: healthy run exit 0 in {elapsed:.1f} s; destination_ready 1, "
                  "history units available 1, stage 1 holds 1 unit; the share has "
                  f"{size} bytes, {free} free, a ledger snapshot, facts and a drift of 0 and 0",
                  flush=True)

            # --- 3a. A move onto the share copies; a move between two stages on it renames.
            # Its own config and history database: nothing above or below sees this scenario.
            docker("cp", str(rename_config), f"{container}:{RENAME_CONFIG_PATH}")
            bdbackup("run", "-c", RENAME_CONFIG_PATH, "-j", "gfs-rename",
                     "--initialize-structure")
            bdbackup("run", "-c", RENAME_CONFIG_PATH, "-j", "moved")
            time.sleep(2)  # a stamped archive is never replaced: the next name needs a new second
            bdbackup("run", "-c", RENAME_CONFIG_PATH, "-j", "moved")
            listing = sh(f"ls {LOCAL_STAGE}").stdout
            archives = sorted(re.findall(r"^moved-[\d-]+\.tar\.gz$", listing, re.MULTILINE))
            if len(archives) != 2:
                raise RuntimeError(f"expected two archives of job moved in {LOCAL_STAGE}:\n"
                                   f"{listing}")
            older, newer = archives
            # The newer archive is the newest of its series and stays; the older one is aged.
            copied = move_once(older, LOCAL_STAGE, HOT, 10)
            if "renamed" in copied:
                raise RuntimeError(f"a move from the local stage onto the share was a rename: "
                                   f"({copied})")
            if not (exists(f"{HOT}/{older}") and not exists(f"{LOCAL_STAGE}/{older}")
                    and exists(f"{LOCAL_STAGE}/{newer}")):
                raise RuntimeError(f"the copy left the archives in the wrong places:\n{listing}")
            before = inode(f"{HOT}/{older}")
            renamed = move_once(older, HOT, COLD, 40)
            if not re.fullmatch(r"weekly \d{4}-W\d\d, renamed", renamed):
                raise RuntimeError(f"the move between the share stages is not reported as a "
                                   f"rename: ({renamed})")
            after = inode(f"{COLD}/{older}")
            if after != before or exists(f"{HOT}/{older}"):
                raise RuntimeError(f"the archive was not renamed in place: inode {before} then "
                                   f"{after}; still in {HOT}: {exists(f'{HOT}/{older}')}")
            print(f"Step 3a: local stage -> share stage was a copy ({copied}); share stage -> "
                  f"share stage was a rename ({renamed}), inode {before} kept", flush=True)

            # --- 3b. The share now holds the scenario's directories next to the stage of job
            # gfs; they are not its entries, so after another run its drift is still nothing.
            bdbackup("run", "-c", CONFIG_PATH, "-j", "gfs")
            code, _, _ = metrics()
            text, samples = read_metrics()
            if code != 0:
                raise RuntimeError(f"the metrics run after the scenario exited {code}:\n{text}")
            check_share_series(samples, one(samples, "bdbackup_metrics_last_run_timestamp_seconds"))
            print("Step 3b: after the scenario, a gfs run and a metrics run: the drift of the "
                  "share stage is still 0 missing and 0 unknown", flush=True)

            # --- 4. The outage.
            rules("I")
            dropping = True
            sh("echo 3 > /proc/sys/vm/drop_caches")
            blocked = sh(f"timeout 5 ls {SHARE}", check=False)
            if blocked.returncode != 124:
                raise RuntimeError(f"a plain ls of the share did not hang (exit "
                                   f"{blocked.returncode}): the outage is not in effect")
            print("Step 4: iptables drops NFS (2049), portmapper (111) and mountd (32767) on "
                  "loopback; ls of the share hangs (killed by timeout 5, exit 124)", flush=True)
            # The control of the lexical config read: a load that resolves the share's paths
            # (run --validate loads the config that way) blocks on the same outage.
            resolving = sh(f"timeout 5 bdbackup run -c {CONFIG_PATH} --validate", check=False)
            if resolving.returncode != 124:
                raise RuntimeError(f"a resolving config load did not hang (exit "
                                   f"{resolving.returncode}): the outage does not block it, so "
                                   "the lexical read proves nothing here")
            print("  control: bdbackup run --validate, which resolves the config's paths, hangs "
                  "too (killed by timeout 5, exit 124)", flush=True)

            # --- 5. A metrics run during the outage; the process list in the middle of it.
            started = time.monotonic()
            running = subprocess.Popen(  # noqa: S603
                [docker_binary, "exec", container, "timeout", "120", "bdbackup", "metrics", "-c",
                 CONFIG_PATH], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(1.5)
            during = ps()
            out, err = running.communicate(timeout=150)
            first_elapsed = time.monotonic() - started
            first_code = running.returncode
            print(f"Step 5: bdbackup metrics during the outage -> exit {first_code} in "
                  f"{first_elapsed:.1f} s (bound {BOUND} s)", flush=True)
            for line in err.strip().splitlines():
                print(f"  {line}", flush=True)
            print("  processes 1.5 s into the run (pid stat cmd):", flush=True)
            for line in during.splitlines():
                print(f"    {line}", flush=True)
            after = ps()
            print("  processes after the run ended:", flush=True)
            for line in after.splitlines():
                print(f"    {line}", flush=True)
            if out:
                raise RuntimeError(f"metrics printed on stdout: {out!r}")
            if first_code != 1 or first_elapsed > BOUND:
                raise RuntimeError(f"the outage run exited {first_code} in {first_elapsed:.1f} s")
            text, samples = read_metrics()
            stamp = one(samples, "bdbackup_metrics_last_run_timestamp_seconds")
            if stamp <= healthy_stamp:
                raise RuntimeError("the metrics file was not rewritten during the outage")
            checks = {
                "destination_ready 0": one(samples, "bdbackup_destination_ready",
                                           job_name="gfs", stage="2", path=SHARE) == 0,
                "destination timeout error": one(
                    samples, "bdbackup_metrics_error", source="destination", job_name="",
                    reason="timeout") == 1,
                "success 0": one(samples, "bdbackup_metrics_success") == 0,
                "no history series": not series(samples, "bdbackup_history_units"),
                "no capacity series": not series(samples, "bdbackup_destination_size_bytes")
                and not series(samples, "bdbackup_destination_free_bytes"),
                "no ledger snapshot series": not series(
                    samples, "bdbackup_gfs_ledger_snapshot_timestamp_seconds"),
                "facts and drift from the database": len(series(
                    samples, "bdbackup_gfs_facts_timestamp_seconds", job_name="gfs")) == 1
                and one(samples, "bdbackup_gfs_drift_unknown_entries", job_name="gfs",
                        stage="2") == 0,
                "gfs stage info": len(series(samples, "bdbackup_gfs_stage_info")) == 2,
                "gfs stage units": len(series(samples, "bdbackup_gfs_stage_units")) == 2,
                "gfs last run": one(samples, "bdbackup_job_last_run_result", job_name="gfs",
                                    job_type="gfs", result="completed") == 1,
            }
            print(f"  the file: {checks}", flush=True)
            if not all(checks.values()):
                raise RuntimeError(f"the outage file is wrong:\n{text}")

            # --- 6. A second run right away: never a hang.
            code, second_elapsed, _ = metrics()
            if code not in (1, 3) or second_elapsed > BOUND:
                raise RuntimeError(f"the second outage run exited {code} in "
                                   f"{second_elapsed:.1f} s")
            print(f"Step 6: a second run during the outage -> exit {code} in "
                  f"{second_elapsed:.1f} s", flush=True)

            # --- 7. Recovery.
            rules("D")
            dropping = False
            recovery_started = time.monotonic()
            deadline = recovery_started + 240
            while True:
                if sh(f"timeout 5 stat {SHARE} >/dev/null", check=False).returncode == 0:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("the share did not come back within 240 s")
            reconnected = time.monotonic() - recovery_started
            print(f"Step 7: rules removed; the share answers again after {reconnected:.1f} s",
                  flush=True)
            deadline = time.monotonic() + 60
            while probe_children() and time.monotonic() < deadline:
                time.sleep(1)
            print("  processes before the recovery run:", flush=True)
            for line in ps().splitlines():
                print(f"    {line}", flush=True)
            code, elapsed, _ = metrics()
            text, samples = read_metrics()
            if code != 0:
                raise RuntimeError(f"the recovery run exited {code}:\n{text}")
            if one(samples, "bdbackup_destination_ready", job_name="gfs", stage="2",
                   path=SHARE) != 1 or not series(samples, "bdbackup_history_units"):
                raise RuntimeError("the recovery file does not show the share ready")
            check_share_series(samples, one(samples, "bdbackup_metrics_last_run_timestamp_seconds"))
            print(f"  recovery run exit 0 in {elapsed:.1f} s; destination_ready 1, history "
                  "series back, capacity and the ledger snapshot back", flush=True)
            print(f"Outage timings: run 5 {first_elapsed:.1f} s, run 6 {second_elapsed:.1f} s, "
                  f"recovery run {elapsed:.1f} s after {reconnected:.1f} s of reconnecting; "
                  f"whole test {time.monotonic() - started_at:.0f} s", flush=True)
            print(SUCCESS, flush=True)
        except BaseException:
            logs = docker("logs", "--tail", "40", container, check=False)
            if logs.returncode == 0:
                print(f"--- last log lines of {container}\n{logs.stdout}{logs.stderr}", flush=True)
            raise
        finally:
            if dropping:  # let a stuck mount recover, so that removing the container cannot hang
                rules("D")
            removed = docker("rm", "-f", "-v", container, check=False, timeout=120)
            if removed.returncode and "No such container" not in removed.stderr:
                print(f"Could not remove container {container}: {removed.stderr.strip()}",
                      flush=True)
                raise RuntimeError(f"left container {container} behind; remove it by hand")
            print("Removed the disposable NFS outage container", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--source-hash"]:
        print(source_hash())
    else:
        main()
