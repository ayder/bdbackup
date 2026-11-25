"""Install a wheel into a clean environment and exercise its real entry point."""

import os
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path


def _check_pbm_dry_run(work, cli, run):
    pass


def main(dist: str) -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected_version = tomllib.loads(project.read_text())["project"]["version"]
    wheels = list(Path(dist).resolve().glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one wheel")
    with tempfile.TemporaryDirectory(prefix="bdbackup-wheel-check-") as raw:
        work = Path(raw)
        env = work / "venv"
        venv.EnvBuilder(with_pip=True).create(env)
        python = env / "bin/python"
        cli = env / "bin/bdbackup"

        def run(args, expected=0, *, process_env=None):
            result = subprocess.run(  # noqa: S603
                args, cwd=work, capture_output=True, text=True, env=process_env,
            )
            if result.returncode != expected:
                raise RuntimeError(f"{args}: {result.stdout}\n{result.stderr}")
            return result

        run([str(python), "-m", "pip", "install", str(wheels[0])])
        run([str(python), "-m", "pip", "check"])
        installed_version = run(
            [
                str(python),
                "-c",
                "from importlib.metadata import version; import bdbackup; "
                "assert bdbackup.__version__ == version('bdbackup'); print(bdbackup.__version__)",
            ]
        ).stdout.strip()
        if installed_version != expected_version:
            raise RuntimeError("Installed version does not match pyproject.toml")
        if run([str(cli), "--version"]).stdout.strip() != f"bdbackup, version {expected_version}":
            raise RuntimeError("CLI version does not match pyproject.toml")
        helper = Path(run([
            str(python), "-c",
            "import bdbackup; from pathlib import Path; "
            "print(Path(bdbackup.__file__).with_name('_ssh_status.sh'))",
        ]).stdout.strip())
        if (
            not helper.resolve().is_relative_to(env.resolve())
            or not helper.is_file()
            or not os.access(helper, os.X_OK)
        ):
            raise RuntimeError("Installed wheel's _ssh_status.sh must exist and be executable")
        run([str(cli), "--help"])
        (work / "source").mkdir()
        (work / "source/data").write_text("recovery check")
        (work / "paths").write_text("data\n")
        (work / "direct.toml").write_text(
            '[direct]\ntype="file"\ntemplate_filename="paths"\n'
            'chdir="source"\nbackup_dst="backup"\nformat="tar.gz"\n'
        )
        run([str(cli), "run", "-c", "direct.toml", "-j", "direct"])
        (work / "restore").mkdir()
        run([str(cli), "restore", "--archive", "backup.tar.gz", "-d", "restore"])
        if (work / "restore/data").read_text() != "recovery check":
            raise RuntimeError("Installed wheel failed backup/restore round trip")
        (work / "config.toml").write_text(
            '[history]\ndatabase="state/history.sqlite3"\nrestore_root="recovery"\n'
            '[daily]\ntype="file"\ntemplate_filename="paths"\n'
            'chdir="source"\nbackup_dst="tracked"\nformat="tar.gz"\n'
        )
        run([str(cli), "run", "-c", "config.toml", "-j", "daily"])
        listing = run([str(cli), "history", "-c", "config.toml"])
        if "success | available" not in listing.stdout:
            raise RuntimeError("Installed wheel did not persist successful backup history")
        run([str(cli), "restore", "-c", "config.toml", "--backup-id", "1", "--yes"])
        if (work / "recovery/daily-1/data").read_text() != "recovery check":
            raise RuntimeError("Installed wheel failed history-based recovery")

        destination = "ssh://backup@127.0.0.1:1/fixtures/daily"
        (work / "copy.toml").write_text(
            '[history]\ndatabase="dry-history/ledger.sqlite3"\n'
            '[archive]\ntype="file"\ntemplate_filename="paths"\nchdir="source"\n'
            'backup_dst="dry-archives/daily"\ntimestamp=true\n'
            f'[archive.copy]\ndestination="{destination}"\nmethod="rsync"\n'
        )
        tools = work / "no-ssh"
        tools.mkdir()
        marker = work / "ssh-invoked"
        for name in ("ssh", "scp", "rsync"):
            sentinel = tools / name
            sentinel.write_text(
                '#!/bin/sh\nprintf "called\\n" > "$BDBACKUP_SMOKE_SSH_MARKER"\nexit 99\n'
            )
            sentinel.chmod(0o755)
        dry_copy = run(
            [str(cli), "run", "-c", "copy.toml", "-j", "archive", "--dry-run"],
            process_env={
                **os.environ,
                "PATH": str(tools),
                "BDBACKUP_SMOKE_SSH_MARKER": str(marker),
            },
        )
        if f"Dry run; configured copy: {destination} (rsync)" not in dry_copy.stdout.splitlines():
            raise RuntimeError("Installed wheel dry run did not report the SSH copy")
        if any(path.exists() for path in (marker, work / "dry-history", work / "dry-archives")):
            raise RuntimeError("Installed wheel copy dry run invoked an SSH tool or wrote state")

        previous = (work / "backup.tar.gz").read_bytes()
        (work / "paths").write_text("missing\n")
        run([str(cli), "run", "-c", "direct.toml", "-j", "direct"], expected=1)
        if (work / "backup.tar.gz").read_bytes() != previous:
            raise RuntimeError("Failed backup replaced the good archive")
    print(
        "Installed wheel: imports, CLI, recovery, history, failed replacement, "
        "SSH copy dry run and executable SSH helper passed"
    )


if __name__ == "__main__":
    main(sys.argv[1])
