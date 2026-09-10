# bdbackup

A small, opinionated backup helper for day-to-day operations:

- **file** backups from a plain-text template file into a tar archive
- **mysqldump** logical backups (gzip-compressed)
- **xtrabackup / mariabackup** physical full and incremental backups

Designed as a general-purpose backup tool with safety nets:
credential files instead of exposed passwords, post-backup verification,
process locking, atomic target directories, and config-driven jobs.

## Support and safety

Python 3.12+ on POSIX systems (Linux/macOS); Windows is not supported.
`tar` and `tar.gz` work on every supported Python; `tar.zst` requires Python 3.14
with Zstandard support. Database binaries are separate system dependencies.

Every built-in backend verifies staging output before publishing it. A missing
or unreadable file fails the backup and preserves the previous archive. A process
lock prevents concurrent writers to the same file destination or physical root.
`--no-verify` skips only the additional verification pass after publication.
Verification checks archive readability or physical checkpoints; it does not
replace testing a restore. File backups are not filesystem snapshots: quiesce
applications or back up snapshots when files can change during a run.

Safe relative symlinks, hardlinks, empty directories and ordinary directory
permissions/timestamps round-trip. Restore rejects escaping paths/links and
special files on all supported Python versions. Ownership and privileged
permission bits are intentionally not restored. Use a destination that is not
being modified by another process during extraction.

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

Place the global option before the command: `bdbackup --logging DEBUG file ...`.
Levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`.
The CLI uses these exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Backup/verify failure |
| 2 | Usage error |
| 3 | Lock held, or retention refused an unsafe/incomplete scan |

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
| `-f, --format` | Archive format: `tar`, `tar.gz`, `tar.zst` (Python 3.14+) |
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
| `xtrabackup` | `xtrabackup` | `bdbackup.mysql.XtraBackup` | Percona XtraBackup / MariaDB mariabackup |

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

Use a bare `-p` to enter a password securely. Passing `-p PASSWORD` puts it in
bdbackup's own process arguments and potentially shell history. Child database
processes receive only a temporary credentials-file path; its contents are
quoted, its permissions are `0600`, and it is removed after the run.

Mysqldump retains `--single-transaction`, `--routines`, `--events` and `--triggers`
by default. `--options` adds options; explicit `--skip-*` flags can override
applicable defaults. `--full` retains these defaults and adds `--all-databases`.
Single-transaction consistency applies to transactional tables; quiesce writes
to nontransactional tables and avoid schema changes during a dump.

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

Prepare a full backup or an incremental recovery point into a new directory:

```bash
bdbackup xtrabackup prepare /backup/mysql/production/2026-09-10/Full_ID \
    -r /backup/mysql/production -d /restore/production -u xtrabackup -p
```

Use the exact path printed by the backup command in place of `Full_ID`. Passing
an incremental path prepares its full and every prerequisite incremental up to
that point. Preparation copies sources to private working directories,
decompresses compressed copies, applies the increments in dependency order, and
publishes the recovery directory only on success. The destination must be new
and outside the backup root. Original backups remain available for new
incrementals and repeated recovery attempts.

Use XtraBackup matching your MySQL/Percona server series (8.0 with 8.0, 8.4 with
8.4); use `mariabackup` or `mariadb-backup` matching your MariaDB installation.
MariaDB preparation omits XtraBackup's `--apply-log-only` option. Compression is
**off by default**. For a compatible recent XtraBackup, select `--compress zstd`;
MariaDB's deprecated built-in compression accepts only `quicklz` and requires
`qpress` for decompression. Compatibility must be established with an actual
recovery test for the exact server and backup binary versions in use.

New physical backups record parent/full identities and LSNs in `bdbackup.json`.
Incrementals live under `DATE/Incremental/FULL_ID/UNIQUE_ID`, and cannot attach to
another full taken on the same day. Older backups without this metadata require
a new full before taking further incrementals; full backups can still be
prepared as recovery copies. Retention removes complete dated chains, preserves
the newest successful full's date, and refuses to prune without a valid full.

Options:

| Flag | Description |
|------|-------------|
| `mode` | `full`, `incremental`, `prune`, or `prepare` |
| `--database` | Database name (used for directory naming) |
| `-r, --root` | Backup root directory |
| `-u, --user` | MySQL user |
| `-p, --password` | MySQL password; omit value to be prompted securely |
| `-b, --binary` | `xtrabackup` or `mariabackup` |
| `--compress` | Compression algorithm (default: uncompressed) |
| `--compress-threads` | Compression threads (default: 4) |
| `--parallel` | Number of copy threads (default: 1) |
| `--throttle` | Limit I/O to this many IOPS |
| `--retention` | Days of backups to keep (default: 5) |
| `--verify / --no-verify` | Verify the backup after creation (default: on) |

A process lock prevents two backup runs from corrupting the same backup root.
Failed backups are written to a temporary directory first and cleaned up on
error, so a partial backup can never be mistaken for a complete one.

## Retention policy (GFS)

The `bdbackup retention` command applies a grandfather-father-son policy to
a backup tree: keep **every** backup for the last N days, then **one per ISO
week** for N weeks, then **one per calendar month** for N months. Tiers are
sequential and never overlap, so the retained set is deterministic and easy to
reason about.

```bash
# Dry run (default) -- lists what would be deleted, changes nothing
bdbackup retention --full-dir /backup/mysql/production \
    --incr-dir /backup/mysql/production/incr \
    --log-dir /backup/mysql/production/log \
    --daily 7 --weekly 4 --monthly 6 \
    --incr-days 7 --log-days 30 \
    --min-keep-fulls 2 --pick first

# Actually delete
bdbackup retention ... --apply
```

| Tier | Flag | Meaning |
|------|------|---------|
| Daily | `--daily N` | Keep every backup of the last N calendar days |
| Weekly | `--weekly N` | Then one backup per ISO week, for N weeks |
| Monthly | `--monthly N` | Then one backup per calendar month, for N months |
| Incrementals | `--incr-days N` | Keep incrementals for N days, never past their full |
| Logs | `--log-days N` | Keep log files for N days |
| Safety | `--min-keep-fulls N` | Never leave fewer than N newest fulls |
| Pick | `--pick first/last` | Which backup survives in a weekly/monthly bucket |

**Chain safety:** this flat-file policy expects one chronological backup
stream: each new full starts a chain, and subsequent increments continue that
chain until the next full. A retained incremental pins its full and every
preceding incremental in that chain, including prerequisites older than the age
window. A missing full makes an incremental an orphan. If a full/incremental
scan skips a file (for example while it is being written), expiration is deferred
because its dependencies are uncertain. Keep all files for a stream together;
arbitrary overlapping chains or missing intermediate backups require explicit
external metadata and are not supported by this filename-based policy.

This command handles flat backup files such as `full_*.mbi`, not the dated
XtraBackup directory tree. Use `bdbackup xtrabackup prune` for physical backups.

Only `--full-dir` is required. `--incr-dir` and `--log-dir` are optional.
`--full-glob`, `--incr-glob`, and `--log-glob` default to `full_*.mbi`,
`incr_*.mbi`, and `*.log`.

## Config-driven jobs

For scheduled or multi-job usage, define a TOML config file. Convention:
keep all bdbackup settings under `~/.config/bdbackup/` (honours
`$XDG_CONFIG_HOME` / `$BDBACKUP_CONFIG_DIR`) — `config.toml`, the
`files.template` path lists, `templates/*.py` exclusion presets, and
`engines/*.py` database engines.

```toml
# ~/.config/bdbackup/config.toml
[files-daily]
type = "file"
template_filename = "~/.config/bdbackup/files.template"
backup_dst = "/backup/files/daily"
chdir = "/srv/www"   # source path: template/exclude entries resolve against it
format = "tar.gz"  # tar.zst requires Python 3.14+
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

[mysql-prod-retention]
type = "retention"
full_dir = "/backup/flat/full"
incr_dir = "/backup/flat/incr"
log_dir = "/backup/flat/log"
daily = 7
weekly = 4
monthly = 6
incr_days = 7
log_days = 30
min_keep_fulls = 2
pick = "first"
apply = false           # set true once the dry-run output looks right
```

Configuration paths expand `~`; relative config paths resolve against the
config file's directory. Template entries and `exclude` entries resolve against
`chdir` (or the process working directory if omitted). For a single-database
mysqldump job, set `database = "mydatabase"`; for all databases include
`--all-databases` in `options`.

Each top-level table is one job; its keys (except `type`) are passed
to the backend constructor, so they use Python-style underscores
(`exclude_templates`), not CLI dashes. See [config.toml.example](https://github.com/ayder/bdbackup/blob/main/config.toml.example)
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
ruff check bdbackup tests scripts
```

Optional database integration tests use disposable, network-isolated Docker
containers with synthetic data. They remove only the containers and volumes
created for that run. Pull the matching images before running:

```bash
python scripts/integration_mysql.py           # mysql:8.4
python scripts/integration_physical.py percona # percona-server/xtrabackup:8.4
python scripts/integration_physical.py mariadb # mariadb:11.4
```

Physical recovery returns a prepared data directory; copying it into a server's
data directory, assigning ownership to the server user, and starting that server
are separate administrator actions. Use the same database version and
filesystem case-sensitivity as the source.

`pyproject.toml` (`project.version`) is the sole version source. The package's
`__version__` and CLI read installed distribution metadata generated from it.
After changing the version, run `uv lock` to refresh the generated lockfile and
reinstall with `pip install -e '.[dev]'` to refresh local metadata. Source
development requires this editable installation.

Build and validate a release:

```bash
python -m build
twine check dist/*
python scripts/smoke_install.py dist
```

Commit the validated changes and create an annotated tag named
`v<project.version>`. The publishing workflow accepts only that matching tag. It
runs the reusable CI workflow on that commit (tests on Python 3.12–3.14, lint,
real MySQL/Percona/MariaDB recovery tests, source/wheel build, Twine, and an
installed-wheel recovery smoke test), then
uploads those exact artifacts. Configure the PyPI trusted publisher for
`ayder/bdbackup`, `publish.yml`, environment `pypi` before releasing. Account
configuration and actual database recovery evidence are release prerequisites.

## License

MIT License. See [LICENSE](LICENSE).
