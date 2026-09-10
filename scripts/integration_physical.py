"""Opt-in disposable physical full/incremental recovery test.

Use `percona` for Percona Server/XtraBackup 8.4 with Zstandard compression,
or `mariadb` for MariaDB 11.4 with no compression. Requires Docker and images.
Only task-created containers and volumes are accessed or removed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from bdbackup.mysql import XtraBackup


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
            xb = XtraBackup(
                work / "backups",
                user="root",
                compress="" if maria else "zstd",
                parallel=2,
                binary=binary,
            )

            def backup_command(cmd):
                extra = (
                    ["--socket=/var/lib/mysql/mysql.sock", "--datadir=/var/lib/mysql"]
                    if "--backup" in cmd
                    else []
                )
                target = next(
                    arg.split("=", 1)[1] for arg in cmd if arg.startswith("--target-dir=")
                )
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
                        target,
                        check=False,
                    )

            with (
                patch.object(xb, "_run", side_effect=backup_command),
                patch.object(tempfile, "tempdir", str(work)),
            ):
                full = xb.full_backup()
                print("Full backup verified", flush=True)
                sql(source, "INSERT INTO audit.data VALUES(2)")
                xb.incremental_backup()
                sql(source, "INSERT INTO audit.data VALUES(3)")
                tip = xb.incremental_backup()
                print("Two chained incrementals verified", flush=True)
                full_copy = xb.prepare(full.path, work / "full-recovery")
                chain_copy = xb.prepare(tip.path, work / "chain-recovery")
                print("Full and incremental recovery copies prepared", flush=True)
            for index, (copy, expected) in enumerate([(full_copy, "1"), (chain_copy, "1,2,3")]):
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
