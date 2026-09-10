"""Require the release tag to match the version declared in pyproject.toml."""

import os
import tomllib
from pathlib import Path


def check_release(ref: str, root: Path = Path(".")) -> None:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if ref != f"refs/tags/v{version}":
        raise SystemExit(f"Release requires tag v{version}, matching pyproject.toml")


if __name__ == "__main__":
    check_release(os.environ.get("GITHUB_REF", ""))
