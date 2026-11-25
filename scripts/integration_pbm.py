"""Probe PBM and test the installed bdbackup wheel with real replica sets.

Build both candidate images, then run the probe and integration:

    .venv/bin/python scripts/integration_pbm.py --build-probe-images
    .venv/bin/python scripts/integration_pbm.py --probe all
    .venv/bin/python scripts/integration_pbm.py

Run one integration fixture or one case:

    .venv/bin/python scripts/integration_pbm.py --fixture mongo6-pbm210
    .venv/bin/python scripts/integration_pbm.py --fixture mongo7-pbm217 --case physical

Select individual probes separately:

    .venv/bin/python scripts/integration_pbm.py --probe mongo6-pbm210
    .venv/bin/python scripts/integration_pbm.py --probe mongo7-pbm217 --case restore-physical

Add --fixture mongo6-pbm210 to the build command to build one image only.
The fixtures run sequentially. Each replica set uses its own internal Docker
network with no published ports and contains only synthetic data.
"""

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = {
    "mongo6-pbm210": ("6.0", "2.10.0", "storage"),
    "mongo7-pbm217": ("7.0", "2.17.0", "storagePath"),
}
CASES = (
    "single-secondary", "local-layouts", "restore-logical", "restore-physical", "modes", "pitr",
)
INTEGRATION_CASES = ("logical", "physical", "backup-failed", "missing-files")
SUCCESS = "Real PBM backup export and restore passed"
LOCAL = "/pbm-local"
SHARED = "/pbm-shared"
WORK = Path("/var/tmp/bdbackup-pbm")  # noqa: S108
CONFIG = str(WORK / "config.yaml")
URI = "mongodb://localhost:27017/"
PYTHON = "/opt/bdbackup/bin/python"
BDBACKUP = "/opt/bdbackup/bin/bdbackup"
JOB = "mongo"
JOB_CONFIG = WORK / "job.toml"
LEDGER = WORK / "history.sqlite3"
UNITS = WORK / "units"
DATA = 'db.getSiblingDB("app").items'
ROWS = [{"_id": index, "value": f"row-{index}"} for index in range(100)]


def source_hash(root=ROOT):
    digest = hashlib.sha256()
    package = root / "bdbackup"
    sources = sorted((*package.rglob("*.py"), *package.rglob("*.sh")))
    for path in [root / "pyproject.toml", *sources]:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def expect(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name} failed" + (f"\n{detail}" if detail else ""))
    print(f"PASS: {name}", flush=True)


def image(fixture):
    return f"bdbackup-pbm:{fixture}"


def docker_path():
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required")
    return docker


def build_probe_images(fixtures):
    docker = docker_path()
    candidate = source_hash()
    with tempfile.TemporaryDirectory(prefix="bdbackup-pbm-wheel-") as directory:
        subprocess.run(  # noqa: S603
            (sys.executable, "-m", "build", "--wheel", "--outdir", directory),
            cwd=ROOT, check=True,
        )
        expect("checkout source fingerprint survived the wheel build", source_hash() == candidate)
        for fixture in fixtures:
            mongo, pbm, _ = FIXTURES[fixture]
            subprocess.run(  # noqa: S603
                (docker, "build", "-f", ROOT / "scripts/docker/pbm.Dockerfile",
                 "--build-arg", f"MONGO_TAG={mongo}", "--build-arg", f"PBM_TAG={pbm}",
                 "--build-arg", f"SOURCE_HASH={candidate}", "-t", image(fixture), directory),
                check=True,
            )


def run(docker, *args, env=None, stdin=None, timeout=30):
    return subprocess.run(  # noqa: S603
        (docker, *args), capture_output=True, text=True, check=False, env=env,
        input=stdin, timeout=timeout,
    )


def detail(result):
    return (result.stderr.strip() or result.stdout.strip()).replace(URI, "<fixture URI>")


def output(result, check):
    expect(check, result.returncode == 0, detail(result))
    return result.stdout.strip()


def json_output(result, check):
    raw = output(result, check)
    try:
        return json.loads(raw)
    except ValueError:
        raise AssertionError(f"{check}: stdout is not JSON") from None


def wait_for(name, probe, seconds=90):
    expires = time.monotonic() + seconds
    while time.monotonic() < expires:
        if value := probe():
            expect(name, True)
            return value
        time.sleep(1)
    expect(name, False, f"timed out after {seconds}s")


class ReplicaSet:
    def __init__(self, docker, tag):
        self.docker = docker
        self.tag = tag
        self.network = f"bdbackup-pbm-{uuid4().hex[:12]}"
        self.volume = f"{self.network}-share"
        self.nodes = tuple(f"{self.network}-{index}" for index in (1, 2, 3))
        self.primary, self.secondary, self.other = self.nodes

    def command(self, *args, **kwargs):
        return run(self.docker, *args, **kwargs)

    def cleanup(self, *args):
        result = self.command(*args)
        if result.returncode and not re.search(r"not found|no such", detail(result), re.I):
            raise RuntimeError(f"fixture cleanup failed ({' '.join(args)}): {detail(result)}")

    @contextmanager
    def started(self, *, umask=None, local_everywhere=False, storage_node=None):
        writer = storage_node or self.secondary
        with ExitStack() as cleanup:
            cleanup.callback(self.cleanup, "network", "rm", self.network)
            output(
                self.command("network", "create", "--internal", self.network),
                "fixture network is internal",
            )
            cleanup.callback(self.cleanup, "volume", "rm", self.volume)
            output(self.command("volume", "create", self.volume), "create shared restore storage")
            for node in self.nodes:
                cleanup.callback(self.cleanup, "rm", "--force", "--volumes", node)
                output(
                    self.command(
                        "run", "--detach", "--name", node, "--hostname", node,
                        "--network", self.network, "--mount",
                        f"type=volume,source={self.volume},target={SHARED}",
                        "--entrypoint", "sleep", self.tag, "infinity",
                    ),
                    f"{node}: start isolated container",
                )
            output(
                self.exec(self.secondary, BDBACKUP, "--version"),
                "candidate CLI runs from the installed wheel on the secondary",
            )
            output(
                self.exec(self.primary, "chown", "1001:0", SHARED, user="0:0"),
                "shared storage owned by the mongod user",
            )
            for node in self.nodes if local_everywhere else (writer,):
                self.local_storage(node)

            for node in self.nodes:
                output(self.exec(node, "mkdir", "-p", str(WORK)), f"{node}: create work area")
                self.start_mongod(node)
            members = [
                {"_id": index, "host": f"{node}:27017", "priority": 2 if not index else 1}
                for index, node in enumerate(self.nodes)
            ]
            replica = json.dumps({"_id": "rs0", "members": members})
            output(
                self.mongo(self.primary, f"if (!rs.initiate({replica}).ok) quit(1)"),
                "initialise rs0 with three members",
            )
            self.wait_members()
            self.seed()
            for node in self.nodes:
                self.start_agent(node, umask)
            self.configure(LOCAL)
            self.wait_agents(self.nodes if local_everywhere else (writer,))
            yield self

    def exec(self, node, *args, user=None, detach=False, stdin=None, uri=None, timeout=30):
        options = []
        env = None
        if uri is not None:
            options.extend(("--env", "PBM_MONGODB_URI"))
            env = dict(os.environ, PBM_MONGODB_URI=uri)
        if user:
            options.extend(("--user", user))
        if detach:
            options.append("--detach")
        if stdin is not None:
            options.append("--interactive")
        return self.command("exec", *options, node, *args, env=env, stdin=stdin, timeout=timeout)

    def local_storage(self, node):
        output(
            self.exec(node, "install", "-d", "-o", "1001", "-g", "0", "-m", "0755", LOCAL,
                      user="0:0"),
            f"{node}: create local PBM storage",
        )

    def mongo(self, node, javascript):
        return self.exec(node, "mongosh", "--quiet", "--eval", javascript, timeout=15)

    def start_mongod(self, node):
        output(
            self.exec(
                node, "mongod", "--replSet", "rs0", "--bind_ip_all", "--dbpath", "/data/db",
                "--fork", "--logpath", str(WORK / "mongod.log"), "--logappend",
            ),
            f"{node}: start mongod",
        )

    def wait_members(self):
        def ready():
            reply = self.mongo(
                self.primary,
                'print(JSON.stringify(rs.status().members.map(m => [m.name, m.stateStr])))',
            )
            if reply.returncode:
                return False
            states = dict(json.loads(reply.stdout))
            wanted = ["PRIMARY", "SECONDARY", "SECONDARY"]
            return [states.get(f"{node}:27017") for node in self.nodes] == wanted

        wait_for("replica set elects the first node and both secondaries are ready", ready)

    def start_agent(self, node, umask=None):
        mask = "" if umask is None else f"umask {umask:03o}; "
        output(
            self.exec(
                node, "sh", "-c",
                f'{mask}echo "$(umask)" > {WORK}/agent-umask; '
                f'echo $$ > {WORK}/agent-pid; exec pbm-agent >> {WORK}/agent.log 2>&1',
                detach=True, uri=URI,
            ),
            f"{node}: start pbm-agent",
        )

    def stop_agent(self, node):
        output(
            self.exec(node, "sh", "-c",
                      f'if test -s {WORK}/agent-pid; then kill "$(cat {WORK}/agent-pid)" '
                      '2>/dev/null || true; fi'),
            f"{node}: stop previous pbm-agent if still running",
        )

    def seed(self):
        output(
            self.mongo(self.primary, f"{DATA}.insertMany({json.dumps(ROWS)}, "
                       '{writeConcern: {w: "majority"}})'),
            "write the fixture rows with majority acknowledgement",
        )

    def pbm(self, *args, node=None, uri=URI):
        return self.exec(node or self.secondary, "pbm", *args, uri=uri, timeout=300)

    def cli(self, *args, uri=None):
        return self.exec(self.secondary, BDBACKUP, *args, uri=uri, timeout=360)

    def configure(self, storage, *, wait=False):
        yaml = f"storage:\n  type: filesystem\n  filesystem:\n    path: {storage}\n"
        for node in self.nodes:
            output(self.exec(node, "sh", "-c", f"cat > {CONFIG}", stdin=yaml),
                   f"{node}: write fixture PBM config")
        flags = ("--wait",) if wait else ()
        output(self.pbm("config", "--file", CONFIG, *flags),
               f"configure filesystem storage at {storage}")

    def wait_agents(self, healthy):
        want = {f"{node}:27017" for node in healthy}
        members = {f"{node}:27017" for node in self.nodes}

        def check():
            result = self.pbm("status", "--sections", "cluster", "--out", "json")
            if result.returncode:
                return False
            status = json.loads(result.stdout)
            nodes = [node for rs in status.get("cluster", []) for node in rs.get("nodes", [])]
            hosts = {node.get("host") for node in nodes}
            okay = {node.get("host") for node in nodes if node.get("ok") is True}
            if hosts == members and okay == want:
                return nodes
            return False

        return wait_for("PBM status reports the expected healthy agents", check)

    def take(self, backup_type, storage=LOCAL, *, expected_node=None, field=None):
        backup = json_output(
            self.pbm("backup", "--type", backup_type, "--wait", "--out", "json"),
            f"{backup_type}: pbm backup from a secondary exits 0 with stdout JSON",
        )
        name = backup.get("name", "")
        expect(f"{backup_type}: PBM names the backup",
               bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", name)))
        if field:
            expect(f"{backup_type}: PBM reports {field}", backup.get(field) == storage)
        node = self.confirm(name, backup_type, storage, expected_node=expected_node)
        return name, node

    def confirm(self, name, backup_type, storage=LOCAL, *, expected_node=None):
        desc = json_output(
            self.pbm("describe-backup", name, "--out", "json", node=expected_node),
            f"{backup_type}: describe-backup exits 0",
        )
        expect(f"{backup_type}: PBM reports done and the configured type",
               (desc.get("status"), desc.get("type")) == ("done", backup_type))
        nodes = [replica.get("node") for replica in desc.get("replsets", ())]
        expect(f"{backup_type}: PBM reports the backup node", len(nodes) == 1 and bool(nodes[0]))
        if expected_node:
            expect(f"{backup_type}: backup taken on {expected_node}", nodes ==
                   [f"{expected_node}:27017"], str(nodes))
        node, _, _ = nodes[0].partition(":")
        output(
            self.exec(node, "test", "-f", f"{storage}/{name}.pbm.json"),
            f"{backup_type}: metadata file is present",
        )
        output(
            self.exec(node, "test", "-d", f"{storage}/{name}/rs0"),
            f"{backup_type}: replica set directory is present",
        )
        return node


def check_versions(cluster, fixture):
    mongo_version, pbm_version, _ = FIXTURES[fixture]
    version = json_output(cluster.pbm("version", "--out", "json"), "pbm version exits 0")
    expect("pinned PBM version", version["Version"] == pbm_version, version["Version"])
    mongo = output(cluster.mongo(cluster.primary, "print(db.version())"), "read server version")
    expect("MongoDB fixture version", mongo.startswith(f"{mongo_version}."), mongo)


def single_secondary(cluster, fixture):
    _, pbm_version, storage_field = FIXTURES[fixture]
    check_versions(cluster, fixture)
    nodes = cluster.wait_agents((cluster.secondary,))
    for node in nodes:
        if node["host"] != f"{cluster.secondary}:27017":
            expect(f'{node["host"]}: agent reports a storage error',
                   not node["ok"] and "storage" in json.dumps(node.get("errors", [])).lower())
    direct = cluster.pbm("backup", "--wait", "--out", "json",
                         uri=f"{URI}?directConnection=true")
    expect("direct connection from a secondary fails with config server URI diagnostic",
           direct.returncode == 1 and "get config server connection URI" in detail(direct),
           detail(direct))
    names = {}
    for backup_type in ("logical", "physical"):
        name, _ = cluster.take(backup_type, expected_node=cluster.secondary, field=storage_field)
        names[backup_type] = name
        for node in (cluster.primary, cluster.other):
            expect(f"{node}: {backup_type} storage absent",
                   cluster.exec(node, "test", "-e", LOCAL).returncode == 1)
    flags = ("--yes",) if pbm_version == "2.17.0" else ()
    started = json_output(cluster.pbm("backup", "--type", "logical", "--out", "json"),
                          "start a backup without waiting")
    busy = cluster.pbm("backup", "--type", "logical", "--wait", "--out", "json")
    expect("a concurrent backup exits 1 with the operation-in-progress diagnostic",
           busy.returncode == 1 and "another operation in progress" in detail(busy), detail(busy))

    def completed():
        result = cluster.pbm("describe-backup", started["name"], "--out", "json")
        return result.returncode == 0 and json.loads(result.stdout).get("status") == "done"

    wait_for("the first of the concurrent backups finishes", completed)
    denied = cluster.pbm("restore", names["logical"], *flags, "--wait")
    expect("restore with storage only on a secondary fails with storage diagnostic",
           denied.returncode == 1 and "get backup storage" in detail(denied), detail(denied))
    output(cluster.exec(cluster.secondary, "chmod", "0555", LOCAL),
           "make the only backup storage unwritable")
    failed = cluster.pbm("backup", "--type", "logical", "--wait", "--out", "json")
    expect("backup with unwritable storage exits 1 with a diagnostic",
           failed.returncode == 1 and bool(detail(failed)), detail(failed))


def local_layouts(cluster):
    name, node = cluster.take("logical")
    expect("with local directories and no priority PBM selects a secondary",
           node in (cluster.secondary, cluster.other), node)
    for other in cluster.nodes:
        if other != node:
            expect(f"{other}: the chosen secondary's backup is absent from this local directory",
                   cluster.exec(other, "test", "-e", f"{LOCAL}/{name}.pbm.json").returncode == 1)
    priorities = json.dumps({f"{cluster.secondary}:27017": 10,
                             f"{cluster.other}:27017": 1, f"{cluster.primary}:27017": 0.5})
    # A fresh YAML config sets the whole priority map. PBM's --set works on scalar keys.
    yaml = (
        f"storage:\n  type: filesystem\n  filesystem:\n    path: {LOCAL}\n"
        f"backup:\n  priority: {priorities}\n"
    )
    output(cluster.exec(cluster.secondary, "sh", "-c", f"cat > {CONFIG}", stdin=yaml),
           "write fixture backup priorities")
    output(cluster.pbm("config", "--file", CONFIG), "prefer the storage secondary")
    cluster.take("logical", expected_node=cluster.secondary)
    cluster.stop_agent(cluster.secondary)
    _, node = cluster.take("logical")
    expect("with the preferred agent stopped PBM falls back", node != cluster.secondary)


def restore(cluster, backup_type, fixture):
    name, _ = cluster.take(backup_type, expected_node=cluster.secondary)
    output(
        cluster.exec(cluster.secondary, "cp", "-R", f"{LOCAL}/{name}",
                     f"{LOCAL}/{name}.pbm.json", SHARED),
        f"{backup_type}: copy just the two backup entries to shared storage",
    )
    restore_shared(cluster, backup_type, fixture, name)


def restore_shared(cluster, backup_type, fixture, name):
    for node in cluster.nodes:
        output(cluster.exec(node, "test", "-r", f"{SHARED}/{name}.pbm.json"),
               f"{node}: metadata readable at the same shared path")
        output(cluster.exec(node, "test", "-d", f"{SHARED}/{name}/rs0"),
               f"{node}: data readable at the same shared path")
    output(cluster.mongo(cluster.primary, f'{DATA}.deleteMany({{}}, '
                        '{writeConcern: {w: "majority"}})'),
           f"{backup_type}: remove fixture data after backup")
    expect("there are no rows before restore",
           cluster.mongo(cluster.primary, f"{DATA}.countDocuments()").stdout.strip() == "0")
    cluster.configure(SHARED, wait=True)
    cluster.wait_agents(cluster.nodes)
    output(cluster.pbm("config", "--force-resync", "--wait"), "resync the copied storage")
    listing = output(cluster.pbm("list"), "list backups after resync")
    expect(f"{backup_type}: PBM lists the backup after resync", name in listing)
    flags = ("--yes",) if FIXTURES[fixture][1] == "2.17.0" else ()
    reply = json_output(
        cluster.pbm("restore", name, *flags, "--wait", "--out", "json"),
        f"{backup_type}: restore finishes",
    )
    # Physical restore reports its state through the copied filesystem, while MongoDB is down.
    if backup_type == "physical":
        restored = reply.get("name", "")
        expect("physical restore returns its operation name", bool(restored))
        report = json_output(
            cluster.pbm("describe-restore", restored, "--config", CONFIG, "--out", "json"),
            "read physical restore status from storage",
        )
        expect("physical restore done on the full replica set", report["status"] == "done",
               str(report.get("status")))
        for node in cluster.nodes:
            cluster.stop_agent(node)
            cluster.start_mongod(node)
        cluster.wait_members()
        for node in cluster.nodes:
            cluster.start_agent(node)
        cluster.configure(SHARED, wait=True)
        cluster.wait_agents(cluster.nodes)
        output(cluster.pbm("config", "--force-resync", "--wait"), "resync after physical restart")
    actual = json_output(cluster.mongo(
        cluster.primary, f"print(JSON.stringify({DATA}.find().sort({{_id: 1}}).toArray()))",
    ), f"{backup_type}: query restored rows")
    expect(f"{backup_type}: restored rows match every original row", actual == ROWS)


def modes(cluster, umask):
    mask = output(cluster.exec(cluster.secondary, "cat", str(WORK / "agent-umask")),
                  "read agent umask")
    if umask is not None:
        expect("agent runs with the requested umask", int(mask, 8) == umask)
    for backup_type in ("logical", "physical"):
        name, _ = cluster.take(backup_type, expected_node=cluster.secondary)
        paths = (f"{LOCAL}/{name}.pbm.json", f"{LOCAL}/{name}")
        manifest = output(
            cluster.exec(cluster.secondary, "find", *paths, "-printf", "%y %m %U %G %p\\n"),
            f"{backup_type}: observe PBM file and directory modes and owners",
        )
        entries = [line.split(" ", 4) for line in manifest.splitlines()]
        observed = Counter((kind, mode, uid, gid) for kind, mode, uid, gid, _ in entries)
        expect(f"{backup_type}: both regular files and directories measured",
               {line[0] for line in entries} == {"f", "d"})
        expect(f"{backup_type}: entries are owned by the mongod user",
               all(uid == "1001" for _, _, uid, _, _ in entries))
        print(f"{backup_type}, umask {mask}: {dict(observed)}", flush=True)
        for user in ("1001:0", "65534:0", "65534:65534"):
            read = output(
                cluster.exec(
                    cluster.secondary, PYTHON, "-c",
                    "import sys\n"
                    "def readable(path):\n"
                    "    try:\n"
                    "        with open(path, 'rb') as source:\n"
                    "            source.read(1)\n"
                    "    except OSError:\n"
                    "        return False\n"
                    "    return True\n"
                    "print(sum(readable(path) for path in sys.argv[1:]))",
                    *(path for kind, _, _, _, path in entries if kind == "f"), user=user,
                ),
                f"{backup_type}: measure file access as {user}",
            )
            file_count = sum(kind == "f" for kind, *_ in entries)
            print(f"{backup_type}, umask {mask}, uid:gid {user}: "
                  f"{read}/{file_count} files readable", flush=True)
            if user == "1001:0":
                expect(f"{backup_type}: mongod user can read every file", int(read) == file_count)


def pitr(cluster):
    cluster.take("logical", expected_node=cluster.secondary)
    for setting in ("pitr.oplogSpanMin=1", "pitr.enabled=true"):
        output(cluster.pbm("config", "--set", setting), f"configure {setting}")
    config = json_output(cluster.pbm("config", "--list", "--out", "json"), "read PITR config")
    expect("PITR is enabled before taking the next backup", config["pitr"]["enabled"] is True)

    def chunks():
        result = cluster.exec(cluster.secondary, "find", f"{LOCAL}/pbmPitr", "-type", "f")
        return set(result.stdout.splitlines()) if not result.returncode else set()

    before = wait_for("PITR writes an oplog slice", chunks, seconds=180)
    name, _ = cluster.take("logical", expected_node=cluster.secondary)
    output(cluster.mongo(cluster.primary, f'{DATA}.insertOne({{_id: 100, value: "after backup"}})'),
           "write a new oplog entry after the backup")
    after = wait_for("PITR adds an oplog slice after a successful backup",
                     lambda: chunks() - before, seconds=180)
    expect("PITR data stays outside the two backup entries",
           all(path.startswith(f"{LOCAL}/pbmPitr/") for path in after))
    print(f"PITR backup {name}: added {len(after)} oplog slice(s)", flush=True)


def run_case(docker, fixture, case):
    print(f"PBM fixture {fixture}, case {case}", flush=True)
    masks = (None, 0o077) if case == "modes" else (None,)
    for umask in masks:
        with ReplicaSet(docker, image(fixture)).started(
            umask=umask, local_everywhere=case == "local-layouts",
        ) as cluster:
            if case == "single-secondary":
                single_secondary(cluster, fixture)
            elif case == "local-layouts":
                local_layouts(cluster)
            elif case.startswith("restore-"):
                restore(cluster, case.removeprefix("restore-"), fixture)
            elif case == "modes":
                modes(cluster, umask)
            else:
                pitr(cluster)


def checked_fixtures(docker, fixture):
    candidate = source_hash()
    for selected in FIXTURES if fixture == "all" else (fixture,):
        tag = image(selected)
        label = run(docker, "image", "inspect", "--format",
                    '{{ index .Config.Labels "bdbackup.source" }}', tag)
        if label.returncode or label.stdout.strip() != candidate:
            raise SystemExit(
                f"{tag} is missing or was built from other sources than this checkout. "
                "Build it with --build-probe-images."
            )
        expect(f"{tag}: installed candidate provenance matches the checkout", True)
        yield selected


def run_probes(fixture, case):
    docker = docker_path()
    for selected in checked_fixtures(docker, fixture):
        for selected_case in (case,) if case else CASES:
            run_case(docker, selected, selected_case)
    print("PBM probe passed", flush=True)


def archive_facts(path, storage, name):
    """Compare the candidate's tar to PBM's files, on the node that holds both."""
    # This function runs under the candidate's Python inside the replica-set container.
    import hashlib
    import tarfile
    from pathlib import Path

    path = Path(path)
    storage = Path(storage)
    tree = storage / name
    source = {
        file.relative_to(storage).as_posix(): file
        for file in (storage / f"{name}.pbm.json", tree, *tree.rglob("*"))
    }
    types_match = True
    different_files = []
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        names_match = names == source.keys() and len(names) == len(members)
        for member in members:
            file = source.get(member.name)
            if file is None:
                continue
            if file.is_dir() and member.isdir() and not file.is_symlink():
                continue
            if not (file.is_file() and member.isfile() and not file.is_symlink()):
                types_match = False
                continue
            with file.open("rb") as stored, archive.extractfile(member) as archived:
                while chunk := stored.read(1024 * 1024):
                    if archived.read(len(chunk)) != chunk:
                        different_files.append(member.name)
                        break
                else:
                    if archived.read(1):
                        different_files.append(member.name)
    with path.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    return {
        "names_match": names_match, "types_match": types_match,
        "different_files": different_files, "size": path.stat().st_size, "sha256": digest,
    }


def configure_job(cluster, backup_type):
    config = (
        f'[history]\ndatabase="{LEDGER}"\n'
        f'[{JOB}]\ntype="pbm"\nbackup_type="{backup_type}"\n'
        f'storage_path="{LOCAL}"\nout_dir="{UNITS}"\n'
    )
    output(
        cluster.exec(cluster.secondary, "sh", "-c", f"cat > {JOB_CONFIG}", stdin=config),
        f"{backup_type}: configure the installed bdbackup job on the secondary",
    )


def history_record(cluster):
    rows = json_output(
        cluster.exec(
            cluster.secondary, PYTHON, "-c",
            "import json, sqlite3, sys; "
            "db = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True); "
            "db.row_factory = sqlite3.Row; "
            "print(json.dumps([dict(row) for row in db.execute("
            "'SELECT * FROM backup_runs ORDER BY id')]))",
            str(LEDGER),
        ),
        "read the installed wheel's persisted history",
    )
    expect("one run recorded in history", len(rows) == 1)
    return rows[0]


def backup_and_restore(cluster, backup_type, fixture):
    result = cluster.cli("run", "-c", str(JOB_CONFIG), "-j", JOB, uri=URI)
    stdout = output(result, f"{backup_type}: installed wheel backup exits 0")
    row = history_record(cluster)
    expect(f"{backup_type}: history records a successful PBM run",
           (row["status"], row["job_name"], row["backup_type"]) ==
           ("success", JOB, f"pbm-{backup_type}"))
    info = json.loads(row["restore_info"])
    name = info.get("name", "")
    expect(f"{backup_type}: history records PBM's timestamp",
           bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", name)))
    expect(f"{backup_type}: history names the backup type and source secondary",
           info == {
               "kind": "pbm", "name": name, "backup_type": backup_type,
               "replsets": [{"name": "rs0", "node": f"{cluster.secondary}:27017"}],
           }, str(info))
    cluster.confirm(name, backup_type, expected_node=cluster.secondary)
    for node in (cluster.primary, cluster.other):
        expect(f"{node}: source backup files exist only on the storage secondary",
               cluster.exec(node, "test", "-e", LOCAL).returncode == 1)

    path = str(UNITS / f"{JOB}-{name.replace(':', '')}.tar")
    expect(f"{backup_type}: CLI prints the expected archive path", stdout == f"Job {JOB}: {path}")
    expect(f"{backup_type}: history records that archive as the unit",
           row["path"] == row["unit"] == path)
    facts = json_output(
        cluster.exec(
            cluster.secondary, PYTHON, "-c",
            inspect.getsource(archive_facts)
            + "\nimport json, sys\nprint(json.dumps(archive_facts(*sys.argv[1:])))\n",
            path, LOCAL, name, timeout=120,
        ),
        f"{backup_type}: compare the recorded tar with the source storage",
    )
    expect(f"{backup_type}: archive holds exactly PBM's metadata and data tree",
           facts["names_match"])
    expect(f"{backup_type}: archive file types match storage", facts["types_match"])
    expect(f"{backup_type}: all archive files match storage byte for byte",
           not facts["different_files"], str(facts["different_files"]))
    expect(f"{backup_type}: history size and SHA-256 match the archive",
           (row["size_bytes"], row["checksum"]) == (facts["size"], facts["sha256"]))

    extracted = output(
        cluster.cli("restore", "--archive", path, "--dst", SHARED),
        f"{backup_type}: installed wheel extracts the exported tar into shared storage",
    )
    expect(f"{backup_type}: extracted unit prints the PBM restore steps",
           f"To restore PBM {backup_type} backup {name}" in extracted
           and "pbm config --force-resync --wait" in extracted
           and "pbm list" in extracted and f"pbm restore {name}" in extracted, extracted)
    restore_shared(cluster, backup_type, fixture, name)


def rejected_backup(cluster, case):
    if case == "backup-failed":
        output(cluster.exec(cluster.secondary, "chmod", "0555", LOCAL),
               "make the only backup storage unwritable")
    result = cluster.cli("run", "-c", str(JOB_CONFIG), "-j", JOB, uri=URI)
    diagnostic = detail(result)
    expect(f"{case}: installed wheel exits 1", result.returncode == 1, diagnostic)
    if case == "backup-failed":
        expect("failed PBM backup shows its diagnostic",
               bool(re.search(r"pbm backup failed: .+", diagnostic)), diagnostic)
    else:
        report = re.search(r"PBM took backup (\S+) on (\S+), but its files are not in ", diagnostic)
        expect("missing files diagnostic names the backup and its actual node",
               report is not None and report[2] == f"{cluster.other}:27017", diagnostic)
        expect("local PBM storage stays absent on the bdbackup node",
               cluster.exec(cluster.secondary, "test", "-e", LOCAL).returncode == 1)
        cluster.confirm(report[1], "logical", expected_node=cluster.other)
    expect(f"{case}: CLI prints no published path", not result.stdout.strip())
    row = history_record(cluster)
    expect(
        f"{case}: history records failure with no artifact or checksum",
        (row["status"], row["path"], row["unit"], row["checksum"]) == ("failed", None, None, None),
    )
    archives = json_output(
        cluster.exec(
            cluster.secondary, PYTHON, "-c",
            "import json, sys; from pathlib import Path; "
            "print(json.dumps(sorted(p.name for p in Path(sys.argv[1]).glob('*'))))",
            str(UNITS),
        ),
        f"{case}: inspect the archive output directory",
    )
    expect(f"{case}: no archive or temporary file was published", not archives, str(archives))


def run_integration_case(docker, fixture, case):
    print(f"PBM fixture {fixture}, integration case {case}", flush=True)
    cluster = ReplicaSet(docker, image(fixture))
    writer = cluster.other if case == "missing-files" else cluster.secondary
    with cluster.started(storage_node=writer):
        check_versions(cluster, fixture)
        configure_job(cluster, "physical" if case == "physical" else "logical")
        if case in {"logical", "physical"}:
            backup_and_restore(cluster, case, fixture)
        else:
            rejected_backup(cluster, case)


def run_integration(fixture, case):
    docker = docker_path()
    for selected in checked_fixtures(docker, fixture or "all"):
        for selected_case in (case,) if case else INTEGRATION_CASES:
            run_integration_case(docker, selected, selected_case)
    print(SUCCESS, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--build-probe-images", action="store_true")
    action.add_argument("--probe", choices=("all", *FIXTURES))
    action.add_argument("--source-hash", action="store_true")
    parser.add_argument("--case", choices=(*CASES, *INTEGRATION_CASES))
    parser.add_argument("--fixture", choices=FIXTURES, help="build or run only this fixture")
    args = parser.parse_args(argv)
    if args.case and args.case not in (CASES if args.probe else INTEGRATION_CASES):
        parser.error("--case is not available in the selected mode")
    if args.case and (args.build_probe_images or args.source_hash):
        parser.error("--case applies only to a probe or integration run")
    if args.fixture and (args.probe or args.source_hash):
        parser.error("--fixture applies only to a build or integration run")
    if args.probe:
        run_probes(args.probe, args.case)
    elif args.build_probe_images:
        build_probe_images((args.fixture,) if args.fixture else FIXTURES)
    elif args.source_hash:
        print(source_hash())
    else:
        run_integration(args.fixture, args.case)


if __name__ == "__main__":
    main()
