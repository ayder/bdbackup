# File and directory backups with tar

A file job archives the files and directories listed in a template file into a
tar, tar.gz or tar.zst archive, with gitignore-style exclusion templates. This
page covers its keys, exclusions, the dry run and restoring an archive.

Part of the [bdbackup documentation](../README.md).

A file job archives the paths listed in a template file (one per line, `#` for
comments, whitespace allowed):

```text
# files.template
Documents
Videos
data/projects
```

## Configuration keys

| Key | Description |
|-----|-------------|
| `template_filename` | Template file listing paths to archive |
| `backup_dst` | Destination archive path (without extension) |
| `chdir` | Source path: resolve template paths and relative excludes against it |
| `format` | Archive format: `tar`, `tar.gz`, `tar.zst` (Python 3.14+) |
| `exclude` | Exact paths to exclude |
| `exclude_pattern` | Glob patterns to exclude |
| `exclude_templates` | Named exclusion templates, e.g. `["python-dev"]` |
| `follow_symlinks` | Follow symbolic links when archiving (default: `false`) |
| `timestamp` | Write `<dst>-<UTC YYYY-MM-DD-HHMMSS><ext>` instead of replacing one archive; an existing name is never overwritten (default: `false`) |

Without `timestamp`, every run replaces the same archive, so only the newest
run stays restorable. With it, each run writes a new archive such as
`daily-2026-09-29-020000.tar.gz`, and a run that would reuse an existing name
fails instead. File jobs that GFS rotates set `timestamp = true`.

## Dry run

List what a job would archive without writing it:

```bash
bdbackup run -c config.toml -j files-daily --dry-run
```

It prints the archive it would create (`Dry run; would create: <archive>`) and
logs each entry it would archive at INFO level on stderr
(`Would back up: <path> -> <member>`), which `--logging WARNING` or `ERROR`
hides.

## History restore

File archives are extracted into the selected directory with the existing
safe extraction filters.

Restore any file archive, with or without history:

```bash
bdbackup restore --archive /backup/files/daily.tar -d /restore/here
```

## Exclusion templates

`exclude_templates` applies named bundles of gitignore-style exclusion
patterns so you don't have to repeat common artifact rules:

```toml
exclude_templates = ["python-dev"]
```

The built-in `python-dev` template skips `__pycache__/`, `*.py[cod]`, `.venv/`,
`venv/`, `uv.lock`, `Pipfile.lock`, `poetry.lock`, `*.egg-info/`, `build/`,
`dist/`, and common tool caches (`.mypy_cache/`, `.pytest_cache/`,
`.ruff_cache/`, `.tox/`, ...). Templates combine with `exclude` and
`exclude_pattern`.

Patterns use gitignore-style syntax: `*.pyc` matches at any depth, a trailing
`/` matches directories only, patterns containing a `/` (e.g.
`tests/containers/*img`) match that path at any depth, a leading `/` anchors to
the root, and `!` negates a previous pattern.

Custom templates are plain Python files in `~/.config/bdbackup/templates/`
(honours `$XDG_CONFIG_HOME` and `$BDBACKUP_CONFIG_DIR`) that self-register —
no existing code needs to change to add one:

```python
# ~/.config/bdbackup/templates/go_dev.py
from bdbackup.templates import ExclusionTemplate

TEMPLATE = ExclusionTemplate(
    name="go-dev",
    patterns=("vendor/", "vendor/**", "*.test", "go.work"),
    description="Go development artifacts",
)
```
