"""Opt-in real MySQL recovery check in a disposable, network-isolated container.

Run with the project installed: python scripts/integration_mysql.py [mysql:8.4]
Requires Docker and a local server image. Never attaches to existing databases.
"""

import gzip
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from bdbackup.mysql import MySQLBackup


def main(image="mysql:8.4"):
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required")
    name = "bdbackup-recovery-" + uuid4().hex[:12]

    def command(*args, input=None, check=True):
        return subprocess.run(  # noqa: S603
            [docker, *args],
            input=input,
            capture_output=True,
            check=check,
        )

    def sql(query):
        return (
            command("exec", "-i", name, "mysql", "-uroot", "-N", "-B", input=query.encode())
            .stdout.decode()
            .strip()
        )

    with tempfile.TemporaryDirectory(prefix="bdbackup-real-mysql-") as raw:
        work = Path(raw).resolve()
        try:
            command(
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "--network",
                "none",
                "--mount",
                f"type=bind,src={work},dst={work},readonly",
                "--mount",
                "type=tmpfs,destination=/var/lib/mysql",
                "-e",
                "MYSQL_ALLOW_EMPTY_PASSWORD=yes",
                image,
                "--innodb-buffer-pool-size=64M",
                "--max-connections=20",
            )
            print(f"Started disposable {image} server", flush=True)
            for _ in range(120):
                result = command(
                    "exec",
                    name,
                    "mysql",
                    "-uroot",
                    "-N",
                    "-B",
                    "-e",
                    "SELECT @@skip_networking",
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip() == b"0":
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Disposable MySQL server did not become ready")
            version = sql("SELECT VERSION()")
            sql(
                "CREATE DATABASE audit_one; CREATE DATABASE audit_two; "
                "CREATE TABLE audit_one.data (id INT PRIMARY KEY, payload TEXT); "
                "INSERT INTO audit_one.data VALUES (1, 'saved'); "
                "CREATE TABLE audit_two.data (id INT PRIMARY KEY); "
                "INSERT INTO audit_two.data VALUES (2); "
                "CREATE PROCEDURE audit_one.echo_value() SELECT 42; "
                "CREATE EVENT audit_one.tick ON SCHEDULE EVERY 1 DAY DO SELECT 1; "
                "CREATE TRIGGER audit_one.fill BEFORE INSERT ON audit_one.data "
                "FOR EACH ROW SET NEW.payload = COALESCE(NEW.payload, 'fallback');"
            )
            password = 'hash#quote"back\\slash'  # noqa: S105 - synthetic test account
            escaped = password.replace("\\", "\\\\").replace("'", "''")
            sql(
                f"CREATE USER 'backup'@'localhost' IDENTIFIED BY '{escaped}'; "
                "GRANT ALL PRIVILEGES ON *.* TO 'backup'@'localhost';"
            )
            binaries = work / "bin"
            binaries.mkdir()
            wrapper = binaries / "mysqldump"
            wrapper.write_text(
                f'#!/bin/sh\nexec {shlex.quote(docker)} exec {name} mysqldump "$@"\n'
            )
            wrapper.chmod(0o700)
            with (
                patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}),
                patch.object(tempfile, "tempdir", str(work)),
            ):
                backend = MySQLBackup(work / "dumps", jobs=2, user="backup", password=password)
                dumps = backend.backup_all(["audit_one", "audit_two"])
                full = MySQLBackup(
                    work / "full", options=["--all-databases"], user="backup", password=password
                ).backup()
                if b"audit_two" not in gzip.decompress(full.path.read_bytes()):
                    raise RuntimeError("Full dump omitted a database")
            sql("DROP DATABASE audit_one; DROP DATABASE audit_two;")
            for dump in dumps:
                command(
                    "exec",
                    "-i",
                    name,
                    "mysql",
                    "-uroot",
                    input=gzip.decompress(dump.path.read_bytes()),
                )
            checks = {
                "data": sql("SELECT payload FROM audit_one.data WHERE id=1") == "saved",
                "second database": sql("SELECT id FROM audit_two.data") == "2",
                "routine": sql("CALL audit_one.echo_value()") == "42",
                "event": sql(
                    "SELECT COUNT(*) FROM information_schema.events WHERE event_schema='audit_one'"
                )
                == "1",
                "trigger": sql(
                    "SELECT COUNT(*) FROM information_schema.triggers "
                    "WHERE trigger_schema='audit_one'"
                )
                == "1",
            }
            if not all(checks.values()):
                raise RuntimeError(f"Recovery failed: {checks}")
            print(
                f"MySQL {version}: parallel dumps, full dump, data/routine/event/trigger "
                "recovery passed",
                flush=True,
            )
        finally:
            command("rm", "-f", "-v", name, check=False)
            print("Removed disposable database container", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mysql:8.4")
