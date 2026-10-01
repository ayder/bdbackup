# bdbackup

**Backups by design.**

bdbackup is a command-line backup tool and Python library for MySQL, MariaDB and
files on Linux and macOS. It takes MySQL and MariaDB backups with mysqldump,
Percona XtraBackup and MariaDB Backup (full and incremental physical backups,
compressed and optionally encrypted), archives files and directories as tar,
tar.gz or tar.zst, and rotates the results across storage stages with GFS
(grandfather-father-son) retention. Every backup is verified before it is
published and recorded with a SHA-256 checksum in an SQLite backup history, from
which a guided restore prepares a recovery copy. Jobs live in one TOML file and
run from cron.

## Features

- [MySQL and MariaDB physical backups](https://github.com/ayder/bdbackup/blob/master/docs/README_XTRABACKUP.md) with Percona
  XtraBackup and MariaDB Backup: full and incremental, compressed, AES-256
  encrypted with Percona, prepared for recovery with `restore --backup`.
- [MySQL and MariaDB logical backups](https://github.com/ayder/bdbackup/blob/master/docs/README_MYSQLDUMP.md) with mysqldump,
  gzip-compressed, one database or all.
- [File and directory backups](https://github.com/ayder/bdbackup/blob/master/docs/README_FILE.md) as tar archives, with
  gitignore-style exclusion templates.
- [GFS (grandfather-father-son) backup rotation](https://github.com/ayder/bdbackup/blob/master/docs/README_GFS.md): daily,
  weekly, monthly and yearly stages on local disks and NFS mounts, driven by a
  checksummed backup ledger.
- [Backup history and guided restore](#backup-history-and-guided-restore) with
  SHA-256 checksums in SQLite.
- [Configuration validation](#validate-configuration-and-permissions) of
  settings, filesystem permissions and MySQL grants, without running a backup.
- [Custom backup engines](https://github.com/ayder/bdbackup/blob/master/docs/README_ENGINES.md) as plugins.

## Documentation

| Document | Covers |
|---|---|
| [File and directory backups with tar](https://github.com/ayder/bdbackup/blob/master/docs/README_FILE.md) | file jobs, template files, exclusion templates, restoring an archive |
| [MySQL and MariaDB logical backups with mysqldump](https://github.com/ayder/bdbackup/blob/master/docs/README_MYSQLDUMP.md) | mysqldump jobs, credentials, default options |
| [MySQL and MariaDB physical backups with Percona XtraBackup and MariaDB Backup](https://github.com/ayder/bdbackup/blob/master/docs/README_XTRABACKUP.md) | xtrabackup jobs, full and incremental backups, encryption, preparing a recovery copy, engine pruning |
| [GFS (grandfather-father-son) backup rotation](https://github.com/ayder/bdbackup/blob/master/docs/README_GFS.md) | stages, preparing stage directories, safety rules, run and step logs, ledger copies |
| [Custom backup engines (plugins)](https://github.com/ayder/bdbackup/blob/master/docs/README_ENGINES.md) | the engine registry and writing your own engine |
| [Annotated configuration](https://github.com/ayder/bdbackup/blob/master/config.toml.example) | every config key with a comment |

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

Install the current code from the original GitHub repository:

```bash
pip install "git+https://github.com/ayder/bdbackup.git"
```

From PyPI:

```bash
pip install bdbackup
```

For development:

```bash
git clone https://github.com/ayder/bdbackup.git
cd bdbackup
pip install -e ".[dev]"
```

## Commands

`bdbackup` has three commands; everything else is an option:

```bash
bdbackup run      -c CONFIG -j JOB [--full | --incremental] [--no-verify] [--dry-run]
bdbackup run      -c CONFIG --validate [-j JOB]
bdbackup run      -c CONFIG -j GFS_JOB --initialize-structure
bdbackup restore  -c CONFIG [--backup-id N] [--job JOB] [-y] [-d DIR] [--encrypt-key-file KEY]
bdbackup restore  --archive ARCHIVE -d DIR
bdbackup restore  --backup BACKUP --root ROOT -d DIR [--binary BINARY] [--encrypt-key-file KEY]
bdbackup history  -c CONFIG [--job JOB] [--successful | --create-checksum]
```

`-c` is `--config` on every command; `-j` is the short form of `--job` on `run`
only (`restore` and `history` take `--job`). `bdbackup` itself takes only
`--version`, `--help` and `--logging LEVEL`, placed before the command:
`bdbackup --logging DEBUG run -c config.toml -j files-daily`. Levels: `DEBUG`,
`INFO`, `WARNING`, `ERROR`.

The CLI uses these exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Backup/verify failure (including a job whose backend rejects its settings in a real run), or validation checks failed/incomplete |
| 2 | Usage error, an inactive job, or a configuration error found when the config loads or by `--dry-run` |
| 3 | Lock held; for GFS, another run of the job is in progress or only deferred work remains |

## Config-driven jobs

Every backup is a job in a TOML config file. Convention: keep all bdbackup
settings under `~/.config/bdbackup/` (honours `$XDG_CONFIG_HOME` /
`$BDBACKUP_CONFIG_DIR`) — `config.toml`, the `files.template` path lists,
`templates/*.py` exclusion presets, and `engines/*.py` database engines.

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
```

Configuration paths expand `~`; relative config paths resolve against the
config file's directory. Template entries and `exclude` entries resolve against
`chdir` (or the process working directory if omitted). For a single-database
mysqldump job, set `database = "mydatabase"`; for all databases include
`--all-databases` in `options`.

Except for the optional `[history]` settings, each top-level table is one job;
its keys (except `type`, `active` and `restore_root`) are passed
to the backend constructor, so they use Python-style underscores
(`exclude_templates`). See [config.toml.example](https://github.com/ayder/bdbackup/blob/master/config.toml.example)
for a fully annotated config with every option explained.

Run one job:

```bash
bdbackup run -c ~/.config/bdbackup/config.toml -j mysql-prod
```

An xtrabackup job also takes `--incremental`; see
[MySQL and MariaDB physical backups](https://github.com/ayder/bdbackup/blob/master/docs/README_XTRABACKUP.md#full-and-incremental-backups).

### Dry run

`--dry-run` reports what a job would do: it runs no backup tool, writes no
backup and records no history; a GFS dry run creates only its job's lock file
beside the history database. What each job type reports is described with it:
[file jobs](https://github.com/ayder/bdbackup/blob/master/docs/README_FILE.md#dry-run),
[mysqldump jobs](https://github.com/ayder/bdbackup/blob/master/docs/README_MYSQLDUMP.md#dry-run),
[xtrabackup jobs](https://github.com/ayder/bdbackup/blob/master/docs/README_XTRABACKUP.md#dry-run) and
[GFS jobs](https://github.com/ayder/bdbackup/blob/master/docs/README_GFS.md#dry-run).

A dry run exits 1 where the real run would fail before writing anything (an
incremental without a full, a file job that selects nothing). `--dry-run` cannot
be combined with `--verify` or `--no-verify`.

### Pausing a job

Set `active = false` in a job's table to pause it. Every
`run -j` of that job, including `--dry-run` and `--validate -j`, then exits 2 with
`Job '<name>' is inactive (active = false)`, and a full `--validate` names it as
not checked. `active` defaults to `true`. Configuration rules still apply to an
inactive job, and GFS still rotates its backups.

### Scheduling with cron

bdbackup does not install schedules. Add crontab lines yourself,
run as the OS account the backups belong to. For example, a weekly full on
Sunday, incrementals on the other days, a nightly file backup, and GFS after
the backups:

```cron
0 2 * * 0   bdbackup run -c /opt/dbs/config.toml -j mysql-prod
0 2 * * 1-6 bdbackup run -c /opt/dbs/config.toml -j mysql-prod --incremental
30 2 * * *  bdbackup run -c /opt/dbs/config.toml -j files-daily
0 4 * * *   bdbackup run -c /opt/dbs/config.toml -j gfs-main
```

Times use the cron daemon's timezone, and separate lines do not wait for each
other: leave enough time for backups before GFS. Add `--dry-run` to the GFS line
until its report looks right.

### Validate configuration and permissions

Check all configured jobs without creating a backup or running retention:

```bash
bdbackup run -c /opt/dbs/config.toml --validate
# Or validate only one job:
bdbackup run -c /opt/dbs/config.toml --validate -j mysql-prod
```

`--validate` cannot be combined with `--full`, `--incremental`, `--verify`,
`--no-verify` or `--dry-run`. Run validation as the OS account that will run your cron jobs. It checks backend
settings, required executables, destination access, SQLite/recovery directories,
file-template sources, and encryption key requirements. Missing directories are
reported with `mkdir -p` guidance and the required OS-user permissions. A missing
directory that the backend can create under a writable parent is reported as
creatable; validation does not create it.

For MySQL jobs, the installed `mysql`/`mariadb` client authenticates using the job's
credentials and reads `CURRENT_USER()`, server version, datadir and `SHOW GRANTS`.
Passwords are passed through a temporary mode-0600 options file, removed afterward.
No `CREATE USER` or `GRANT` is executed. Missing privileges produce SQL for an
administrator, for example:

```sql
GRANT RELOAD, BACKUP_ADMIN, REPLICATION CLIENT, PROCESS, LOCK TABLES
ON *.* TO 'xtrabackup_user'@'localhost';
GRANT SELECT ON `performance_schema`.`log_status`
TO 'xtrabackup_user'@'localhost';
```

The helper also checks the performance-schema tables used by current Percona
XtraBackup and prints `CREATE TABLESPACE` separately as an optional privilege for
importing individual tables. MariaDB Backup gets its own privilege recommendations.
See [Percona privileges](https://docs.percona.com/percona-xtrabackup/8.4/privileges.html)
and [MariaDB Backup privileges](https://mariadb.com/docs/server/server-usage/backup-and-restore/mariadb-backup/mariadb-backup-overview).

Checks cover direct grants; role-derived privileges and partial revokes may need
manual review. Physical datadir checks cover root-directory access, not every data
file or external tablespace. Custom mysqldump options, routine visibility, GTID
settings and exact server/binary version compatibility still need review.
For a GFS job, every stage path after the first must exist and be writable, and
its `.bdbackup-destination` marker must be present and writable; a missing
marker's line names the `--initialize-structure` command that creates it.
An unreachable database or missing client is a failed/incomplete check, not a pass.
Validation does not add SQLite history rows. It exits 0 when its checks pass, 1
for failed or incomplete checks, and 2 for invalid command or configuration syntax.

## Backup history and guided restore

Enable SQLite history in your TOML configuration. No extra Python dependency
or database server is required:

```toml
[history]
database = "state/history.sqlite3"
restore_root = "/restore/bdbackup"
```

Both paths expand `~`; relative paths resolve against the configuration file.
`restore_root` defaults to `restores` beside that file. Omitting `[history]`
disables history. File jobs automatically exclude this live database and its
SQLite journal files; keep it outside backup inputs when possible. New history
databases are created with permissions `0600`.

`run` records every backup attempt of a configured job:

```bash
bdbackup run -c config.toml -j files-daily
bdbackup history -c config.toml
bdbackup history -c config.toml --job files-daily --successful
```

Each record contains an ID, job name, backup type, UTC start/completion times,
status (`running`, `success`, or `failed`), and the successful artifact's absolute
path and size. Physical full and incremental runs record their respective types
and the root/binary needed for preparation.
Credentials and raw error messages are not stored; failed rows contain only the
exception class. Restores, validation, GFS runs and dry runs are not backup
attempts and create no backup-attempt rows. Direct Python backend calls are not automatically
recorded; applications can wrap them with `History.run`.

Every successful backup also records its unit and a SHA-256 checksum taken right
after the backup, which costs one extra read of the backup. A file's checksum is
the SHA-256 of its content. A directory's checksum is the SHA-256 of a manifest
with one line per regular file, `<sha256>  <relative path>`, sorted by path; a
symlink or special file inside a physical backup fails the backup. The unit is
what a rotation tool moves as one piece: the archive or dump file itself, or for
xtrabackup the dated directory that holds the full and all of its incrementals.
`bdbackup history` shows it as `unit <path>`, or `unit -` for records without one.
For units that GFS manages, the line continues with `| stage <stage path> |
locations <path>, …`, or `| deleted <time>` once GFS has removed the unit.
Reproduce a directory checksum on Linux (use `shasum -a 256` on macOS):

```bash
cd /backup/mysql/production/2026-09-29/Full_<id> && find . -type f -print0 \
  | LC_ALL=C sort -z | xargs -0 sha256sum | sed 's|  \./|  |' | sha256sum
```

Records written by bdbackup 0.6.2 or earlier have no checksum. Record them explicitly:

```bash
bdbackup history -c config.toml --create-checksum
bdbackup history -c config.toml --create-checksum --job mysql-prod
```

It prints `<id>: recorded`, `<id>: skipped: unavailable` for an artifact that is
missing or was replaced, or `<id>: skipped: unexpected unit` for a physical backup
outside its dated layout, and then exits 1. A checksum already recorded is never
changed. `--create-checksum` cannot be combined with `--successful`. The checksum lives in the `checksum` column of the `backup_runs` table.

History uses schema version 5. A database at an older schema upgrades on the next
write; bdbackup 0.6.2 or earlier refuses a version-5 database.

History is written before work starts, and success only after the backup and its
requested verification finish. If history cannot be written, the command fails;
an artifact already created is preserved. A forcibly killed process may leave a
`running` row, which is never offered for restore. Existing backups are not
imported automatically.

Select a successful backup interactively:

```bash
bdbackup restore -c config.toml
bdbackup restore -c config.toml --job files-daily
```

Each backup job may set `restore_root = "/restore/mysql-prod"` to override
`[history] restore_root` for its guided-restore suggestions. It must be a nonempty
path string; `~` expands and relative paths resolve against the config file.
Job-level `restore_root` requires `[history]`.
Validation checks that each job's root is writable or can be created.
Physical jobs usually need a separate root with room for the full and its
incrementals, staged near the database datadir.

The current config's job is matched by the history record's job name. If that job
has no `restore_root`, or is no longer configured, the history root is used.
An explicit `--dst` always wins.

Restore lists successful, available artifacts, asks for the backup ID, suggests
`<restore_root>/<job>-<id>`, and asks for confirmation. You can edit the suggested
path. Repeated restores suggest a numbered alternative; history-based restore
requires a new directory even when `--dst` is supplied. For automation, specify
the exact backup ID and use `--yes`:

```bash
bdbackup restore -c config.toml --backup-id 12 --dst /restore/job-12 --yes
```

What a restore produces depends on the job type; see
[file jobs](https://github.com/ayder/bdbackup/blob/master/docs/README_FILE.md#history-restore),
[mysqldump jobs](https://github.com/ayder/bdbackup/blob/master/docs/README_MYSQLDUMP.md#history-restore),
[xtrabackup jobs](https://github.com/ayder/bdbackup/blob/master/docs/README_XTRABACKUP.md#history-restore) and
[custom engines](https://github.com/ayder/bdbackup/blob/master/docs/README_ENGINES.md#history-restore).

Deleted artifacts remain in history as unavailable. File identity, size, and
modification time detect replaced archives, so older rows for a reused filename
are not offered as older recovery points. Availability uses these file checks.
Restore checks the recorded checksum of every file it uses for backups that GFS
manages (see [GFS backup rotation](https://github.com/ayder/bdbackup/blob/master/docs/README_GFS.md)); for other backups it
validates the actual archive or physical dependency chain. History stores
references to artifacts and does not preserve an archive that a later backup
replaces.

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
python scripts/integration_physical.py percona-encrypted # AES256 + zstd recovery
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

MIT License. See [LICENSE](https://github.com/ayder/bdbackup/blob/master/LICENSE).
