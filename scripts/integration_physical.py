"""Opt-in disposable physical full/incremental recovery test.

Use `percona` for Percona Server/XtraBackup 8.4 with Zstandard compression,
or `percona-encrypted` to also test AES256 encryption and decryption,
or `mariadb` for MariaDB 11.4 with no compression. Requires Docker and images.
Only task-created containers and volumes are accessed or removed.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from click.testing import CliRunner

from bdbackup.cli import main as cli
from bdbackup.config import Config
from bdbackup.gfs import runner
from bdbackup.history import History, HistorySettings
from bdbackup.mysql import XtraBackup
from bdbackup.recovery import backup_restore_info


def manifest_checksum(path):
    """Spec 2 §2 directory checksum, computed independently of bdbackup.ledger."""
    lines = sorted(
        (p.relative_to(path).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in path.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    lines.sort(key=lambda item: item[0].encode())
    manifest = "".join(f"{digest}  {name}\n" for name, digest in lines)
    return hashlib.sha256(manifest.encode()).hexdigest()


def check_ledger(records, root):
    if len(records) != 3:
        raise RuntimeError(f"expected 3 ledger records, found {len(records)}")
    for record in records:
        path = Path(record.path)
        if not record.checksum:
            raise RuntimeError(f"ledger checksum missing for {path}")
        if record.checksum != manifest_checksum(path):
            raise RuntimeError(f"ledger checksum mismatch for {path}")
        unit = root.resolve() / path.relative_to(root.resolve()).parts[0]
        if record.unit != str(unit):
            raise RuntimeError(f"ledger unit {record.unit} is not the date directory {unit}")


def move_and_restore_tip(work, history, xb, info, tip_path):
    """Spec 2 §6: GFS moves the chain's date directory to a cold daily stage, and
    ``bdbackup restore`` prepares its tip from there. Returns the recovery directory."""
    tomorrow = xb._utc_now() + timedelta(days=1)
    with patch.object(xb, "_utc_now", return_value=tomorrow):
        history.run("physical", "xtrabackup-full", xb.full_backup, info)
    records = history.records()
    chain = [r for r in records if r.unit == records[1].unit]
    if len(chain) != 3 or records[1].path != str(tip_path):
        raise RuntimeError("expected the second full newest and the chain of three before it")
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with closing(sqlite3.connect(work / "history.sqlite3")) as db, db:
        db.executemany("UPDATE backup_runs SET completed_at=? WHERE id=?",
                       [(week_ago, r.id) for r in chain])
    cold = work / "cold-daily"
    cold.mkdir()
    (cold / ".bdbackup-destination").touch()
    cfg = work / "gfs.toml"
    cfg.write_text(
        f"[history]\ndatabase = {json.dumps(str(work / 'history.sqlite3'))}\n"
        f"restore_root = {json.dumps(str(work / 'restores'))}\n"
        '[gfs-main]\ntype = "gfs"\n'
        f"[[gfs-main.stage]]\npaths = [{json.dumps(str(work / 'backups'))}]\n"
        'period = "daily"\nkeep = "5d"\n'
        f"[[gfs-main.stage]]\npaths = [{json.dumps(str(cold))}]\n"
        'period = "daily"\nkeep = "20d"\n'
    )
    config = Config(cfg)
    code = runner.run_job(config, config.get("gfs-main"), out=print)
    date_name = Path(chain[0].unit).name
    if code or not (cold / date_name).is_dir() or (work / "backups" / date_name).exists():
        raise RuntimeError(f"GFS did not move {date_name} to the cold daily stage (exit {code})")
    recovery = work / "moved-recovery"
    result = CliRunner().invoke(cli, ["restore", "--config", str(cfg), "--backup-id",
                                      str(records[1].id), "--yes", "--dst", str(recovery)])
    if result.exit_code:
        raise RuntimeError(f"restore of the moved tip exited {result.exit_code}: "
                           f"{result.output} {result.exception!r}")
    print("GFS moved the chain to the cold daily stage and its tip restored from there",
          flush=True)
    return recovery


def main(engine="percona"):
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required")
    prefix = "bdbackup-physical-" + uuid4().hex[:12]
    source = prefix + "-source"
    volume = prefix + "-data"
    restorations = []
    recovery_volumes = []
    maria = engine == "mariadb"
    encrypted = engine == "percona-encrypted"
    server_image = "mariadb:11.4" if maria else "percona/percona-server:8.4"
    backup_image = server_image if maria else "percona/percona-xtrabackup:8.4"
    client = "mariadb" if maria else "mysql"
    binary = "mariadb-backup" if maria else "xtrabackup"

    def command(*args, input=None, check=True):
        result = subprocess.run(  # noqa: S603
            [docker, *args],
            input=input,
            capture_output=True,
        )
        if check and result.returncode:
            raise RuntimeError(
                f"docker {args[:2]}: {result.stderr.decode(errors='replace')[-10000:]}"
            )
        return result

    def sql(container, query, check=True):
        return command(
            "exec",
            "-i",
            container,
            client,
            "-uroot",
            "--socket=/var/lib/mysql/mysql.sock",
            "-N",
            "-B",
            input=query.encode(),
            check=check,
        )

    def ready(container):
        for _ in range(120):
            result = sql(container, "SELECT @@skip_networking", check=False)
            if result.returncode == 0 and result.stdout.strip() == b"0":
                return
            state = command("inspect", "--format", "{{.State.Running}}", container, check=False)
            if state.stdout.strip() != b"true":
                break
            time.sleep(1)
        logs = command("logs", container, check=False)
        raise RuntimeError(f"Server not ready: {logs.stderr.decode(errors='replace')[-4000:]}")

    with tempfile.TemporaryDirectory(prefix="bdbackup-physical-") as raw:
        work = Path(raw).resolve()
        try:
            command("volume", "create", volume)
            command(
                "run",
                "-d",
                "--name",
                source,
                "--network",
                "none",
                "-e",
                "MYSQL_ALLOW_EMPTY_PASSWORD=yes",
                "--mount",
                f"type=volume,src={volume},dst=/var/lib/mysql",
                server_image,
                "--socket=/var/lib/mysql/mysql.sock",
                "--innodb-buffer-pool-size=64M",
            )
            ready(source)
            version = sql(source, "SELECT VERSION()").stdout.decode().strip()
            print(f"Disposable {server_image} server {version} is ready", flush=True)
            sql(
                source,
                "CREATE DATABASE audit; CREATE TABLE audit.data(id INT PRIMARY KEY); "
                "INSERT INTO audit.data VALUES(1);",
            )
            key_file = work / "xtrabackup.key"
            if encrypted:
                key_file.write_bytes(os.urandom(32))
                key_file.chmod(0o600)
            xb = XtraBackup(
                work / "backups",
                user="root",
                compress="" if maria else "zstd",
                parallel=2,
                retention_days=0,
                binary=binary,
                encrypt=encrypted,
                encrypt_key_file=key_file if encrypted else None,
            )

            def backup_command(cmd):
                extra = (
                    ["--socket=/var/lib/mysql/mysql.sock", "--datadir=/var/lib/mysql"]
                    if "--backup" in cmd
                    else []
                )
                writable_paths = [
                    arg.split("=", 1)[1]
                    for arg in cmd
                    if arg.startswith(("--target-dir=", "--incremental-dir="))
                ]
                try:
                    command(
                        "run",
                        "--rm",
                        "--user",
                        "0",
                        "--network",
                        "none",
                        "--mount",
                        f"type=bind,src={work},dst={work}",
                        "--mount",
                        f"type=volume,src={volume},dst=/var/lib/mysql,readonly",
                        "--entrypoint",
                        binary,
                        backup_image,
                        *cmd[1:],
                        *extra,
                    )
                finally:
                    # Linux bind mounts preserve container UIDs. Give the caller
                    # access to generated files before verification and cleanup.
                    # Prepare can also create redo files in its incremental copy.
                    command(
                        "run",
                        "--rm",
                        "--user",
                        "0",
                        "--network",
                        "none",
                        "--mount",
                        f"type=bind,src={work},dst={work}",
                        "--entrypoint",
                        "chown",
                        backup_image,
                        "-R",
                        f"{os.getuid()}:{os.getgid()}",
                        *writable_paths,
                    )

            with (
                # Class-level, so the XtraBackup `bdbackup restore` builds also runs in Docker.
                patch.object(XtraBackup, "_run", side_effect=backup_command),
                patch.object(tempfile, "tempdir", str(work)),
            ):
                history = History(HistorySettings(work / "history.sqlite3", work / "restores"))
                info = backup_restore_info(xb)
                full = history.run("physical", "xtrabackup-full", xb.full_backup, info)
                print("Full backup verified", flush=True)
                sql(source, "INSERT INTO audit.data VALUES(2)")
                history.run("physical", "xtrabackup-incremental", xb.incremental_backup, info)
                sql(source, "INSERT INTO audit.data VALUES(3)")
                tip = history.run(
                    "physical", "xtrabackup-incremental", xb.incremental_backup, info
                )
                print("Two chained incrementals verified", flush=True)
                check_ledger(history.records(), work / "backups")
                print("Ledger unit and checksum verified for full and two incrementals",
                      flush=True)
                full_copy = xb.prepare(full.path, work / "full-recovery")
                chain_copy = work / "chain-recovery"
                args = ["restore", "--backup", str(tip.path), "--root", str(xb.backup_root),
                        "-d", str(chain_copy), "--binary", binary]
                if encrypted:
                    args += ["--encrypt-key-file", str(key_file)]
                result = CliRunner().invoke(cli, args)
                if result.exit_code:
                    raise RuntimeError(f"restore --backup exited {result.exit_code}: "
                                       f"{result.output} {result.exception!r}")
                print("Physical restore through restore --backup prepared the chain", flush=True)
                print("Full and incremental recovery copies prepared", flush=True)
                moved_copy = move_and_restore_tip(work, history, xb, info, tip.path)
            recoveries = [(full_copy, "1"), (chain_copy, "1,2,3"), (moved_copy, "1,2,3")]
            for index, (copy, expected) in enumerate(recoveries):
                restored = prefix + f"-restore-{index}"
                restore_volume = prefix + f"-recovery-data-{index}"
                restorations.append(restored)
                recovery_volumes.append(restore_volume)
                command("volume", "create", restore_volume)
                # Use a Linux volume: MySQL rejects a case-insensitive macOS bind
                # mount when the source used a case-sensitive Linux filesystem.
                command(
                    "run",
                    "--rm",
                    "--user",
                    "0",
                    "--network",
                    "none",
                    "--mount",
                    f"type=bind,src={copy},dst=/recovery,readonly",
                    "--mount",
                    f"type=volume,src={restore_volume},dst=/var/lib/mysql",
                    "--entrypoint",
                    "sh",
                    server_image,
                    "-c",
                    "cp -a /recovery/. /var/lib/mysql/ && chown -R mysql:mysql /var/lib/mysql",
                )
                command(
                    "run",
                    "-d",
                    "--name",
                    restored,
                    "--network",
                    "none",
                    "-e",
                    "MYSQL_ALLOW_EMPTY_PASSWORD=yes",
                    "--mount",
                    f"type=volume,src={restore_volume},dst=/var/lib/mysql",
                    server_image,
                    "--socket=/var/lib/mysql/mysql.sock",
                    "--innodb-buffer-pool-size=64M",
                )
                ready(restored)
                actual = sql(restored, "SELECT GROUP_CONCAT(id ORDER BY id) FROM audit.data")
                if actual.stdout.decode().strip() != expected:
                    raise RuntimeError(f"Recovery returned {actual.stdout!r}, expected {expected}")
                command("rm", "-f", "-v", restored)
            print("Real full and two-incremental database recovery passed", flush=True)
        finally:
            for container in [*restorations, source]:
                command("rm", "-f", "-v", container, check=False)
            for data_volume in [*recovery_volumes, volume]:
                command("volume", "rm", data_volume, check=False)
            print("Removed disposable physical-test containers and data volume", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "percona")
