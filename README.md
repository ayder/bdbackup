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
| `-c, --chdir` | Resolve template paths against this directory |
| `-f, --format` | Archive format: `tar`, `tar.gz`, `tar.zst` |
| `-x, --exclude` | Exact resolved path to exclude (repeatable) |
| `--exclude-pattern` | Glob pattern to exclude (repeatable) |
| `--follow-symlinks` | Follow symbolic links when archiving |
| `--dry-run` | List what would be archived without writing anything |
| `--verify / --no-verify` | Verify the archive after creation (default: on) |
| `--logging` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Restore a file archive:

```bash
bdbackup restore /backup/files/daily.tar -d /restore/here
```

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

For scheduled or multi-job usage, define a TOML config file:

```toml
# jobs.toml
[job.files-daily]
type = "file"
template_filename = "/etc/bdbackup/files.template"
backup_dst = "/backup/files/daily"
format = "tar.zst"
exclude_pattern = ["*.log", "node_modules"]

[job.mysql-prod]
type = "xtrabackup"
backup_root = "/backup/mysql/production"
database = "production"
user = "xtrabackup"
retention_days = 7
parallel = 2

[job.mysqldump-all]
type = "mysqldump"
out_dir = "/backup/mysql/dumps"
user = "backup"
full = true
```

Run one job:

```bash
bdbackup run --config jobs.toml mysql-prod
```

Run every job:

```bash
bdbackup run --config jobs.toml --all
```

## Bash companion scripts (deprecated)

The `scripts/` directory still contains the original `xtrabackup` workflows as
standalone Bash scripts. They are kept for reference but are no longer the
recommended interface; use the Python CLI instead.

```bash
scripts/full_backup.sh
scripts/inc_backup.sh
```

Both scripts respect environment variables such as `BACKUP_ROOT`, `MYSQL_USER`,
`MYSQL_PASSWORD`, `RETENTION_DAYS` and `BACKUP_BIN`.

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
