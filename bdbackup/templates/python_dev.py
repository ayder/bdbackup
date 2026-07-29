"""Built-in exclusion template for Python development artifacts."""

from __future__ import annotations

from bdbackup.templates import ExclusionTemplate

TEMPLATE = ExclusionTemplate(
    name="python-dev",
    description="Python development artifacts: caches, virtualenvs, lockfiles, build output",
    patterns=(
        # Bytecode and tool caches
        "__pycache__/",
        "__pycache__/**",
        "*.py[cod]",
        ".mypy_cache/",
        ".mypy_cache/**",
        ".pytest_cache/",
        ".pytest_cache/**",
        ".ruff_cache/",
        ".ruff_cache/**",
        ".tox/",
        ".tox/**",
        ".nox/",
        ".nox/**",
        ".ipynb_checkpoints/",
        ".ipynb_checkpoints/**",
        ".hypothesis/",
        ".hypothesis/**",
        # Virtual environments
        ".venv/",
        ".venv/**",
        "venv/",
        "venv/**",
        # Lockfiles
        "uv.lock",
        "Pipfile.lock",
        "poetry.lock",
        "pdm.lock",
        # Build and packaging output
        "build/",
        "build/**",
        "dist/",
        "dist/**",
        "*.egg-info/",
        "*.egg-info/**",
        ".eggs/",
        ".eggs/**",
        # Coverage artifacts
        ".coverage",
        ".coverage.*",
        "htmlcov/",
        "htmlcov/**",
    ),
)
