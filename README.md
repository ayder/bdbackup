# bdbackup

A small, opinionated backup helper for day-to-day operations:

- **file** backups from a plain-text template file into a tar archive
- **mysqldump** logical backups (gzip-compressed)
- **xtrabackup / mariabackup** physical full and incremental backups

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

## Usage

All commands support `--logging DEBUG|INFO|WARNING|ERROR`.

### File backup

Create a template file listing paths (one per line, `#` for comments):

```text
# backup-template.txt
Documents
Videos
data/projects
```

Run the backup:

```bash
bdbackup file --template backup-template.txt -d /backup/files/daily -z
```

Options:

| Flag | Description |
|------|-------------|
| `--template` | Template file listing paths to archive |
| `-d, --dst` | Destination archive path (without extension) |
| `-c, --chdir` | Change to this directory before archiving |
| `-z, --compress` | Gzip-compress the archive |
| `-x, --exclude` | Exclude path (repeatable) |
| `--logging` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### mysqldump

Dump a single database:

```bash
bdbackup mysqldump --database mydatabase -o /backup/mysql/dumps -u root -p secret
```

Dump all databases:

```bash
bdbackup mysqldump --full -o /backup/mysql/dumps -u root -p secret
```

Options:

| Flag | Description |
|------|-------------|
| `--database` | Database name to dump (ignored when `--full`) |
| `-o, --out-dir` | Directory for the dump file |
| `-u, --user` | MySQL user |
| `-p, --password` | MySQL password |
| `-h, --host` | MySQL host |
| `-P, --port` | MySQL port |
| `--options` | Comma-separated mysqldump options |
| `--full` | Add `--all-databases` and dump everything |

### xtrabackup

Full backup:

```bash
bdbackup xtrabackup full --database production -r /backup/mysql -u xtrabackup -p secret
```

Incremental backup (requires a full backup from today):

```bash
bdbackup xtrabackup incremental --database production -r /backup/mysql -u xtrabackup -p secret
```

Options:

| Flag | Description |
|------|-------------|
| `mode` | `full` or `incremental` |
| `--database` | Database name (used for directory naming) |
| `-r, --root` | Backup root directory |
| `-u, --user` | MySQL user |
| `-p, --password` | MySQL password |
| `-b, --binary` | `xtrabackup` or `mariabackup` |
| `--compress` | Compression algorithm (default: `zstd`) |
| `--compress-threads` | Compression threads (default: 4) |
| `--retention` | Days of backups to keep (default: 5) |

## Bash companion scripts

The `scripts/` directory still contains the original `xtrabackup` workflows as
standalone Bash scripts. They are installed with the package for environments
that prefer shell-based orchestration:

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
