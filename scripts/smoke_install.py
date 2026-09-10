"""Install a wheel into a clean environment and exercise its real entry point."""

import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path


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

        def run(args, expected=0):
            result = subprocess.run(args, cwd=work, capture_output=True, text=True)  # noqa: S603
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
        run([str(cli), "--help"])
        run([str(python), "-c", "import bdbackup.dboperations"])
        (work / "source").mkdir()
        (work / "source/data").write_text("recovery check")
        (work / "paths").write_text("data\n")
        run(
            [
                str(cli),
                "file",
                "--template",
                "paths",
                "-c",
                "source",
                "-d",
                "backup",
                "--format",
                "tar.gz",
            ]
        )
        (work / "restore").mkdir()
        run([str(cli), "restore", "backup.tar.gz", "-d", "restore"])
        if (work / "restore/data").read_text() != "recovery check":
            raise RuntimeError("Installed wheel failed backup/restore round trip")
        (work / "config.toml").write_text(
            '[history]\ndatabase="state/history.sqlite3"\nrestore_root="recovery"\n'
            '[daily]\ntype="file"\ntemplate_filename="paths"\n'
            'chdir="source"\nbackup_dst="tracked"\nformat="tar.gz"\n'
        )
        run([str(cli), "run", "--config", "config.toml", "daily"])
        listing = run([str(cli), "history", "--config", "config.toml"])
        if "success | available" not in listing.stdout:
            raise RuntimeError("Installed wheel did not persist successful backup history")
        run([str(cli), "restore", "--config", "config.toml", "--backup-id", "1", "--yes"])
        if (work / "recovery/daily-1/data").read_text() != "recovery check":
            raise RuntimeError("Installed wheel failed history-based recovery")
        previous = (work / "backup.tar.gz").read_bytes()
        (work / "paths").write_text("missing\n")
        run(
            [
                str(cli),
                "file",
                "--template",
                "paths",
                "-c",
                "source",
                "-d",
                "backup",
                "--format",
                "tar.gz",
            ],
            expected=1,
        )
        if (work / "backup.tar.gz").read_bytes() != previous:
            raise RuntimeError("Failed backup replaced the good archive")
    print("Installed wheel: imports, CLI, recovery, history and failed replacement passed")


if __name__ == "__main__":
    main(sys.argv[1])
