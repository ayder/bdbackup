# bdbackup

A small, opinionated backup helper for day-to-day operations:

- **file** backups from a plain-text template file into a tar archive
- **mysqldump** logical backups (gzip-compressed)
- **xtrabackup / mariabackup** physical full and incremental backups

Designed as a general-purpose backup tool with safety nets:
credential files instead of exposed passwords, post-backup verification,
process locking, atomic target directories, and config-driven jobs.

## Installation

```bash
pip install bdbackup
```

With optional MySQL metadata support:

```bash
pip install bdbackup[mysql]
```

For development:

```bash
git clone https://github.com/ayder/bdbackup.git
cd bdbackup
pip install -e ".[dev]"
```

## Global options

All commands support `--logging DEBUG|INFO|WARNING|ERROR`.
The CLI uses these exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Backup/verify failure |
| 2 | Usage error |
| 3 | Another backup is already running (lock held) |

## File backup

Create a template file listing paths (one per line, `#` for comments,
whitespace allowed):

```text
# backup-template.txt
Documents
Videos
data/projects
```

Run the backup:

```bash
bdbackup file --template backup-template.txt -d /backup/files/daily
```

Gzip-compress:

```bash
bdbackup file --template backup-template.txt -d /backup/files/daily --format tar.gz
```

Options:

| Flag | Description |
|------|-------------|
| `--template` | Template file listing paths to archive |
| `-d, --dst` | Destination archive path (without extension) |
| `-c, --chdir` | Source path: resolve template paths and relative excludes against it |
| `-f, --format` | Archive format: `tar`, `tar.gz`, `tar.zst` |
| `-x, --exclude` | Exact resolved path to exclude (repeatable) |
| `--exclude-pattern` | Glob pattern to exclude (repeatable) |
| `--exclude-template` | Named exclusion template, e.g. `python-dev` (repeatable, comma-separated allowed) |
| `--follow-symlinks` | Follow symbolic links when archiving |
| `--dry-run` | List what would be archived without writing anything |
| `--verify / --no-verify` | Verify the archive after creation (default: on) |
| `--logging` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Restore a file archive:

```bash
bdbackup restore /backup/files/daily.tar -d /restore/here
```

### Exclusion templates

`--exclude-template` applies a named bundle of gitignore-style exclusion
patterns so you don't have to repeat common artifact rules:

```bash
bdbackup file --template backup-template.txt -d /backup/files/daily \
    --exclude-template python-dev
```

The built-in `python-dev` template skips `__pycache__/`, `*.pyc`, `.venv/`,
`venv/`, `uv.lock`, `Pipfile.lock`, `poetry.lock`, `*.egg-info/`, `build/`,
`dist/`, and common tool caches (`.mypy_cache/`, `.pytest_cache/`,
`.ruff_cache/`, `.tox/`, ...). Templates are repeatable and comma-separated
values work too; they combine with `-x/--exclude` and `--exclude-pattern`.

Patterns use gitignore-style syntax: `*.pyc` matches at any depth, a trailing
`/` matches directories only, patterns containing a `/` (e.g.
`tests/containers/*img`) match relative to the backup root, a leading `/`
anchors to the root, and `!` negates a previous pattern.

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

## Database engines

Database engines are pluggable and live in per-database family packages
(`bdbackup/mysql/` today; `bdbackup/postgres/` is planned). Each engine maps a
config `type` name to a backend class; adding one never requires editing
existing wiring code:

| Engine | Config `type` | Backend | Notes |
|--------|---------------|---------|-------|
| `mysqldump` | `mysqldump` | `bdbackup.mysql.MySQLBackup` | Logical, gzip-compressed SQL dumps |
| `xtrabackup` | `xtrabackup` | `bdbackup.mysql.XtraBackup` | Legacy Percona XtraBackup / MariaDB mariabackup |

To add an engine, drop a self-registering module into either
`bdbackup/mysql/` (shipped with the package) or
`~/.config/bdbackup/engines/` (user-level, no package changes) — a versioned
xtrabackup variant or a mysql-shell `util.dump()` engine both follow the same
recipe:

```python
# ~/.config/bdbackup/engines/mysql_shell.py
from bdbackup.engines import EngineInfo

class MySQLShellDump:
    def __init__(self, out_dir: str = ".", **params): ...
    def backup(self, name=None): ...          # BackupBackend protocol
    def verify(self, result=None): ...
    def prune(self): ...

ENGINE = EngineInfo(
    name="mysql-shell",          # usable as type = "mysql-shell" in config
    backend=MySQLShellDump,
    description="MySQL Shell util.dump() backups",
    family="mysql",
)
```

A config `type` is validated against the live registry, so the new engine is
immediately usable from `config.toml`. MySQL-specific helpers shared by the
family (e.g. `mysql_cnf_file` for credential-safe defaults files) live in
`bdbackup/mysql/helpers.py`. A new database family means creating
`bdbackup/postgres/` and appending `"bdbackup.postgres"` to
`bdbackup.engines.FAMILIES` — the single deliberate modification point.

## mysqldump

Dump a single database:

```bash
bdbackup mysqldump --database mydatabase -o /backup/mysql/dumps -u root -p
```

Dump all databases:

```bash
bdbackup mysqldump --full -o /backup/mysql/dumps -u root -p
```

Parallel dumps of multiple databases:

```bash
bdbackup mysqldump --database db1,db2,db3 -o /backup/mysql/dumps -u root -p --jobs 4
```

Options:

| Flag | Description |
|------|-------------|
| `--database` | Database name(s), comma-separated (ignored when `--full`) |
| `-o, --out-dir` | Directory for the dump file |
| `-u, --user` | MySQL user |
| `-p, --password` | MySQL password; omit value to be prompted securely |
| `-h, --host` | MySQL host |
| `-P, --port` | MySQL port |
| `--options` | Comma-separated mysqldump options |
| `--full` | Add `--all-databases` and dump everything |
| `-j, --jobs` | Parallel dumps when multiple databases are specified |
| `--verify / --no-verify` | Verify the dump after creation (default: on) |

Passwords are never passed on the command line; they are written to a
temporary credentials file (`0600`) and removed after the run.

## xtrabackup

Full backup:

```bash
bdbackup xtrabackup full --database production -r /backup/mysql -u xtrabackup -p
```

Incremental backup (chains to the latest successful full):

```bash
bdbackup xtrabackup incremental --database production -r /backup/mysql -u xtrabackup -p
```

Prune old backups by retention days:

```bash
bdbackup xtrabackup prune --database production -r /backup/mysql --retention 7
```

Prepare a backup to make it restorable:

```bash
bdbackup xtrabackup prepare /backup/mysql/production/2025-01-15/Full_123045 -u xtrabackup -p
```

Options:

| Flag | Description |
|------|-------------|
| `mode` | `full`, `incremental`, `prune`, or `prepare` |
| `--database` | Database name (used for directory naming) |
| `-r, --root` | Backup root directory |
| `-u, --user` | MySQL user |
| `-p, --password` | MySQL password; omit value to be prompted securely |
| `-b, --binary` | `xtrabackup` or `mariabackup` |
| `--compress` | Compression algorithm (default: `zstd`) |
| `--compress-threads` | Compression threads (default: 4) |
| `--parallel` | Number of copy threads (default: 1) |
| `--throttle` | Limit I/O to this many IOPS |
| `--retention` | Days of backups to keep (default: 5) |
| `--verify / --no-verify` | Verify the backup after creation (default: on) |

A process lock prevents two backup runs from corrupting the same backup root.
Failed backups are written to a temporary directory first and cleaned up on
error, so a partial backup can never be mistaken for a complete one.

## Config-driven jobs

For scheduled or multi-job usage, define a TOML config file. Convention:
keep all bdbackup settings under `~/.config/bdbackup/` (honours
`$XDG_CONFIG_HOME` / `$BDBACKUP_CONFIG_DIR`) — `config.toml`, the
`files.template` path lists, and `templates/*.py` exclusion presets.

```toml
# ~/.config/bdbackup/config.toml
[files-daily]
type = "file"
template_filename = "~/.config/bdbackup/files.template"
backup_dst = "/backup/files/daily"
chdir = "/srv/www"   # source path: template/exclude entries resolve against it
format = "tar.zst"
exclude_pattern = ["*.log", "node_modules"]
exclude_templates = ["python-dev"]

[mysql-prod]
type = "xtrabackup"
backup_root = "/backup/mysql/production"
user = "xtrabackup"
retention_days = 7
parallel = 2

[mysqldump-all]
type = "mysqldump"
out_dir = "/backup/mysql/dumps"
user = "backup"
options = ["--single-transaction", "--all-databases"]
```

Each top-level table is one job; its keys (except `type`) are passed verbatim
to the backend constructor, so they use Python-style underscores
(`exclude_templates`), not CLI dashes. See [config.toml.example](config.toml.example)
for a fully annotated config with every option explained.

Run one job:

```bash
bdbackup run --config ~/.config/bdbackup/config.toml mysql-prod
```

Run every job:

```bash
bdbackup run --config ~/.config/bdbackup/config.toml --all
```

## Development

Run tests and linting:

```bash
pytest
ruff check bdbackup tests
```

Build a release:

```bash
python -m build
twine check dist/*
```

## License

MIT License. See [LICENSE](LICENSE).
