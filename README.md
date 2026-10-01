# bdbackup

**Backups by design.**

`bdbackup` is a Python library and command-line tool for file and MySQL/MariaDB
backups with verification, retention, and guided recovery. Define repeatable jobs
in a TOML file, run them with one command, track their outcomes, and prepare
recovery copies with explicit safeguards at each step.

Choose the backup method that fits your data:

- **file** backups from a plain-text template file into a tar archive
- **mysqldump** logical backups (gzip-compressed)
- **xtrabackup / mariabackup** physical full and incremental backups

Built for day-to-day system administration: TOML configuration, credential files,
verification before publication, process locking, atomic publication, and optional
SQLite history keep backup operations explicit and inspectable.

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

For versions published to PyPI:

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
bdbackup restore  -c CONFIG [--backup-id N] [--job JOB] [-y] [-d DIR] [--encrypt-key-file KEY]
bdbackup restore  --archive ARCHIVE -d DIR
bdbackup restore  --backup BACKUP --root ROOT -d DIR [--binary BINARY] [--encrypt-key-file KEY]
bdbackup history  -c CONFIG [--job JOB] [--successful | --create-checksum]
```

`-c` is always `--config`, and `-j` is `--job`. `bdbackup` itself takes only
`--version`, `--help` and `--logging LEVEL`, placed before the command:
`bdbackup --logging DEBUG run -c config.toml -j files-daily`. Levels: `DEBUG`,
`INFO`, `WARNING`, `ERROR`.

The CLI uses these exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Backup/verify failure, or validation checks failed/incomplete |
| 2 | Usage or configuration error, including an inactive job |
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
(`exclude_templates`). See [config.toml.example](https://github.com/ayder/bdbackup/blob/main/config.toml.example)
for a fully annotated config with every option explained.

Run one job:

```bash
bdbackup run -c ~/.config/bdbackup/config.toml -j mysql-prod
```

For an xtrabackup job, take an incremental using its own `backup_root`, credentials,
binary, and encryption settings:

```bash
bdbackup run -c ~/.config/bdbackup/config.toml -j mysql-prod --incremental
```

`--full` is the default; an incremental requires a successful full in that job's
root and chains to the latest successful incremental, if present. `--full` and
`--incremental` together exit 2, and so does `--incremental` on any other job type.
`--verify/--no-verify` applies to both backup kinds.

**Dry run.** `--dry-run` reports what a job would do and changes nothing: it runs
no backup tool, writes no file and records no history.

- A file job lists every entry it would archive and the archive name.
- A mysqldump job names the database (or all databases) and the dump directory.
- An xtrabackup job names the backup kind and its root, and an incremental also
  names the backup it would chain from.
- A GFS job prints its plan.

A dry run exits 1 where the real run would fail before writing anything (an
incremental without a full, a file job that selects nothing). `--dry-run` cannot
be combined with `--verify` or `--no-verify`.

**Pausing a job.** Set `active = false` in a job's table to pause it. Every
`run -j` of that job, including `--dry-run` and `--validate -j`, then exits 2 with
`Job '<name>' is inactive (active = false)`, and a full `--validate` names it as
not checked. `active` defaults to `true`. Configuration rules still apply to an
inactive job, and GFS still rotates its backups.

**Scheduling.** bdbackup does not install schedules. Add crontab lines yourself,
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

Pruning after each full deletes whole dated chains older than `retention_days`.
Set `retention_days` longer than the interval between fulls to preserve the previous
full's incrementals. `retention_days = 0` disables engine deletion for the job;
use it when GFS or another tool rotates the backups. History records physical
backups as `xtrabackup-full` / `xtrabackup-incremental`.

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
An unreachable database or missing client is a failed/incomplete check, not a pass.
Validation does not add SQLite history rows. It exits 0 when its checks pass, 1
for failed or incomplete checks, and 2 for invalid command or configuration syntax.

## File jobs

A file job archives the paths listed in a template file (one per line, `#` for
comments, whitespace allowed):

```text
# files.template
Documents
Videos
data/projects
```

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

List what a job would archive without writing anything:

```bash
bdbackup run -c config.toml -j files-daily --dry-run
```

Restore any file archive, with or without history:

```bash
bdbackup restore --archive /backup/files/daily.tar -d /restore/here
```

### Exclusion templates

`exclude_templates` applies named bundles of gitignore-style exclusion
patterns so you don't have to repeat common artifact rules:

```toml
exclude_templates = ["python-dev"]
```

The built-in `python-dev` template skips `__pycache__/`, `*.pyc`, `.venv/`,
`venv/`, `uv.lock`, `Pipfile.lock`, `poetry.lock`, `*.egg-info/`, `build/`,
`dist/`, and common tool caches (`.mypy_cache/`, `.pytest_cache/`,
`.ruff_cache/`, `.tox/`, ...). Templates combine with `exclude` and
`exclude_pattern`.

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

`active` and `restore_root` are job metadata and are not passed to an engine backend.

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
A dry run of a custom engine exits 2: only the built-in types describe their run.

## mysqldump jobs

| Key | Description |
|-----|-------------|
| `out_dir` | Directory for the dump file (created if missing) |
| `database` | Database to dump; omit it and add `--all-databases` to `options` to dump everything |
| `user`, `password`, `host`, `port` | MySQL connection (defaults: `root`, none, `localhost`, `3306`) |
| `options` | Additional mysqldump options, as an array of strings |

One job dumps one database or all of them; for several selected databases, define
one job per database. Keep a config that holds a `password` readable only by the
backup account (`chmod 600`). Child database processes receive only a temporary
credentials-file path; its contents are quoted, its permissions are `0600`, and it
is removed after the run.

Mysqldump retains `--single-transaction`, `--routines`, `--events` and `--triggers`
by default. `options` adds options; explicit `--skip-*` flags can override
applicable defaults. `--all-databases` retains these defaults.
Single-transaction consistency applies to transactional tables; quiesce writes
to nontransactional tables and avoid schema changes during a dump.

## xtrabackup jobs

| Key | Description |
|-----|-------------|
| `backup_root` | Backup root directory; dated subdirectories are created underneath |
| `user`, `password` | MySQL user and password |
| `binary` | `xtrabackup`, `mariabackup` or `mariadb-backup` |
| `compress` | Compression algorithm (default: uncompressed) |
| `compress_threads` | Compression threads (default: 4) |
| `encrypt` | AES256 backup encryption (default: `false`; Percona only) |
| `encrypt_key_file` | Required 32-byte key file when encrypting |
| `parallel` | Number of copy threads (default: 1) |
| `throttle` | Limit I/O to this many IOPS |
| `retention_days` | Days of backups to keep (default: 5); `0` disables engine deletion |

Take a full with `run -j JOB` and an incremental, which chains to the latest
successful full, with `run -j JOB --incremental`.

Prepare a full backup or an incremental recovery point into a new directory:

```bash
bdbackup restore --backup /backup/mysql/production/2026-09-10/Full_ID \
    --root /backup/mysql/production -d /restore/production
```

Use the exact path printed by the backup run in place of `Full_ID`. Passing
an incremental path prepares its full and every prerequisite incremental up to
that point. Preparation needs no MySQL credentials. `--binary` selects
`mariadb-backup` for MariaDB backups (default: `xtrabackup`), and
`--encrypt-key-file` is required for encrypted backups. Preparation copies sources to private working directories,
decompresses compressed copies, applies the increments in dependency order, and
publishes the recovery directory only on success. The destination must be new
and outside the backup root. Original backups remain available for new
incrementals and repeated recovery attempts. With `[history]` configured,
`restore -c CONFIG --backup-id N` finds the root, binary and key itself.

Use XtraBackup matching your MySQL/Percona server series (8.0 with 8.0, 8.4 with
8.4); use `mariabackup` or `mariadb-backup` matching your MariaDB installation.
MariaDB preparation omits XtraBackup's `--apply-log-only` option. Compression is
**off by default**. For a compatible recent XtraBackup, set `compress = "zstd"`;
MariaDB's deprecated built-in compression accepts only `quicklz` and requires
`qpress` for decompression. Compatibility must be established with an actual
recovery test for the exact server and backup binary versions in use.

New physical backups record parent/full identities and LSNs in `bdbackup.json`.
Incrementals live under `DATE/Incremental/FULL_ID/UNIQUE_ID`, and cannot attach to
another full taken on the same day. Older backups without this metadata require
a new full before taking further incrementals; full backups can still be
prepared as recovery copies. Engine pruning removes complete dated chains, preserves
the newest successful full's date, and refuses to prune without a valid full.

A process lock prevents two backup runs from corrupting the same backup root.
Failed backups are written to a temporary directory first and cleaned up on
error, so a partial backup can never be mistaken for a complete one.

### Encrypted physical backups

Percona XtraBackup jobs can optionally encrypt full and incremental backups with
AES256. In the existing job's TOML section, add:

```toml
encrypt = true
encrypt_key_file = "/etc/mysql/xtrabackup.key"
```

TOML uses `true`/`false`, not `yes`/`no`. The key file must already exist, be
readable by the backup account, and contain exactly 32 bytes. To create a new
key once (this command refuses to overwrite an existing key):

```bash
sudo python3 - <<'PY'
import os
from pathlib import Path

key_path = Path("/etc/mysql/xtrabackup.key")
fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as key_file:
    key_file.write(os.urandom(32))
PY
```

Keep this key outside the backup tree and store a separate secure copy: losing
it makes its encrypted backups unrecoverable. Take a new full backup when changing
keys or encryption settings; incrementals must use their parent's settings and
key. Encryption is not supported by the mariabackup backend.

Run the configured job normally:

```bash
bdbackup run -c /opt/dbs/config.toml -j mysql-prod
```

History records `encrypted = 1` in SQLite's `backup_runs` table for encrypted
attempts (`0` otherwise), including failed attempts. The `status` column indicates
whether an attempt succeeded. History also stores the algorithm and key-file
path for recovery, never the key itself. Existing history databases upgrade
automatically on the next backup; old entries remain unencrypted.

History restore uses the recorded key path. If the key has moved, provide its
new location:

```bash
bdbackup restore -c /opt/dbs/config.toml --backup-id 12 \
  --dst /restore/mysql-prod-12 --encrypt-key-file /secure/saved-xtrabackup.key --yes
```

Recovery decrypts each full/incremental work copy, then decompresses and prepares
it. Original backups remain encrypted; the recovery directory contains plaintext.
For `restore --backup`, supply `--encrypt-key-file` as well.
See [Percona's encryption documentation](https://docs.percona.com/percona-xtrabackup/8.4/encrypt-backups.html).

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
attempts and do not create rows. Direct Python backend calls are not automatically
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

Records taken before this release have no checksum. Record them explicitly:

```bash
bdbackup history -c config.toml --create-checksum
bdbackup history -c config.toml --create-checksum --job mysql-prod
```

It prints `<id>: recorded`, `<id>: skipped: unavailable` for an artifact that is
missing or was replaced, or `<id>: skipped: unexpected unit` for a physical backup
outside its dated layout, and then exits 1. A checksum already recorded is never
changed. `--create-checksum` cannot be combined with `--successful`. The checksum lives in the `checksum` column of the `backup_runs` table.

History uses schema version 5. An existing database upgrades on the next write;
older bdbackup versions refuse a version-5 database.

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

- File archives are extracted into the selected directory with the existing
  safe extraction filters.
- MySQL dumps are decompressed and checked into `<destination>/backup.sql`.
  Importing SQL into a running server is a separate administrator action.
- XtraBackup/MariaDB backups produce a prepared recovery directory, including
  prerequisite increments. The destination must be outside the backup root.
  Server ownership, copy-back, and startup remain administrator actions.
- Custom engines are recorded, but require their own restore support unless
  they inherit a supported backend.

Deleted artifacts remain in history as unavailable. File identity, size, and
modification time detect replaced archives, so older rows for a reused filename
are not offered as older recovery points. Availability uses these file checks.
Restore checks the recorded checksum of every file it uses for backups that GFS
manages (see [Ledger and GFS](#ledger-and-gfs)); for other backups it
validates the actual archive or physical dependency chain. History stores
references to artifacts and does not preserve an archive that a later backup
replaces.

## Ledger and GFS

GFS moves bdbackup's own backups through a chain of storage tiers, called stages,
using only the history ledger: it never guesses from what lies on disk. A typical
chain keeps everything for 5 days on a fast local disk, everything for 15 more
days on cheap NFS storage (point-in-time recovery from incrementals), then one
backup per week, per month and per year.

```toml
[gfs-main]
type = "gfs"

[[gfs-main.stage]]
paths = ["/BACKUP"]         # engines write here
period = "daily"
keep = "5d"

[[gfs-main.stage]]
paths = ["/NFS/daily"]
period = "daily"
keep = "20d"                # 5 days hot + 15 days on NFS

[[gfs-main.stage]]
paths = ["/NFS/weekly"]
period = "weekly"
keep = "8w"

[[gfs-main.stage]]
paths = ["/NFS/monthly"]
period = "monthly"
keep = "12m"

[[gfs-main.stage]]
paths = ["/NFS/yearly", "/DD/yearly"]   # every path receives a copy
period = "yearly"
keep = "7y"
```

Run it with `bdbackup run -c config.toml -j gfs-main`, after the backups (see
Scheduling under [Config-driven jobs](#config-driven-jobs)). A run applies its plan
unless `--dry-run` is given.

**Stages and ages.** `keep` is an age counted from the backup day: `Nd` days,
`Nw` weeks, `Nm` calendar months, `Ny` years. A backup belongs to the first stage
whose `keep` it is still within; past the last stage it is deleted. Each `keep`
must be longer than the previous one on every calendar (a month counts as 28–31
days), and periods never go backwards along the chain. The first stage has
exactly one path and is where backup jobs write.

**Periods.** A daily stage keeps every backup. A weekly stage keeps the newest
backup of each ISO week; a monthly stage, of those, the newest dated in each
month; a yearly stage, of those, the newest dated in each year. Selection is per
job, so a tar job and a MySQL job never compete for one slot. A week is decided
only once it has ended, and a month or year only once the ISO week holding its
last day has ended, so a choice never changes later; until then the backup is
reported `held: <bucket> not complete` and stays where it is.

With the configuration above on Tuesday 2026-09-29 and one backup per day:

| Backup | Result | Why |
|---|---|---|
| 2026-09-24 | `/NFS/daily` | within 20d |
| 2026-09-09 | deleted | its week's newest backup is 09-13 |
| 2026-09-06 (Sun) | `/NFS/weekly` | newest of ISO week 36 |
| 2026-08-30 (Sun) | `/NFS/weekly` | newest of week 35; also August's monthly backup |
| 2026-08-02 (Sun) | deleted | newest of its week, not of August |
| 2026-07-26 (Sun) | `/NFS/monthly` | July's newest weekly backup |
| 2025-12-28 (Sun) | `/NFS/monthly` | also 2025's yearly backup |
| 2024-12-29 (Sun) | `/NFS/yearly` | 2024's yearly backup |

**Units and paths.** GFS moves a unit as one piece: an archive or dump file, or
an xtrabackup dated directory with its full and every incremental (and its
`Full_Latest` and `.full_success` markers). A unit keeps its path relative to
the first stage, so `/BACKUP/mysql/prod/2026-09-29` becomes
`/NFS/daily/mysql/prod/2026-09-29`. The newest unit of each job always stays in
the first stage, so the next incremental finds its full.

**Safety.** Each later stage path must contain an empty file named
`.bdbackup-destination`; an unmounted mount point is an empty local directory
without it, and GFS writes nothing there (`--validate` reports it). GFS copies to
a temporary name, checks every checksum while reading the source, and removes
the original only after every copy is in place and recorded; a failed copy
removes its temporary copies and the next run retries. Before a move,
every copy of the unit is checked, not only the one copied, and
copies keep the permission bits of every file and directory. If the
ledger lists a unit in two stages (left by an earlier version or a hand edit),
the next run removes the earlier copy only after the later copies verify.
Copies hold no backup lock,
so a slow NFS copy never blocks a backup; if an xtrabackup root is locked when
GFS commits, the unit is reported `deferred: locked` and retried next run. GFS
only touches backups recorded in the ledger. It refuses, and reports:

- `refused: checksum mismatch`: the backup changed since it was recorded. To
  accept the change, set the `checksum` column of its `backup_runs` rows to the
  new value with `sqlite3`; the next run acts on it. A backup that changed
  before GFS first saw it is never managed.
- `refused: unexpected entry …`: a unit holds something that is not one of its
  recorded backups or engine markers, such as a leftover `.tmp` directory, or a
  symlink or special file inside a backup.
- `refused: stage not configured`: the unit lives under a stage no longer in the
  configuration. Removing a stage never deletes its backups; restore the stage
  or remove them yourself. Changing the first stage's path keeps managing units
  already moved to later stages; units left under the
  old first-stage path are no longer managed.
- `refused: destination not ready …`: a stage path lacks its marker.

Backup jobs writing into the first stage must not delete or overwrite on their
own: xtrabackup jobs set `retention_days = 0` and file jobs set
`timestamp = true`. Configuration validation enforces both, rejects stage paths
that overlap, and keeps the live history database out of stage paths.

`--dry-run` prints what would move or be deleted and changes nothing. Exit codes: 0 done, 1 a refusal or failure, 2 configuration error, 3
another run of the same job is in progress, or the only unfinished units were
deferred.

**Unmanaged backups.** A backup that was unavailable when GFS first saw it (for
example an older run of a file job without `timestamp`, whose archive a later
run replaced) stays unmanaged on every later run: GFS never moves or deletes it,
and the report counts it in `unmanaged`.

**Report.** Each run lists every unit it acted on, held, deferred or refused,
then a `Summary:` line with counts and bytes per action. One line per stage
follows, such as `Stage /NFS/daily: 15 units (3200000000 bytes)`, counting where
the units are when the run ends (where they would be, in a dry run), and then
the ledger copies written.

**Step log.** Every step is also recorded in the `gfs_steps` table of the history
database, one row per path, with its source, destination, outcome
and reason. A failed step keeps the error message.

**Run log.** Every applying run records one row in the `gfs_runs` table:
the GFS job, start and finish time, status, exit code and the Summary counts.
The status is `running` while the run works, `completed` once it reaches its report
(whatever the exit code), and `failed` if the run itself crashed. A row left
`running` shows a run that was cut off; check that run's steps for leftovers
(Known limits below). Each `gfs_steps` row names its run in `run_id`. A dry run
records nothing.

**Running backups.** While a job has a backup in state `running` in the
ledger, GFS leaves every backup of that job where it is, whatever its age, and
reports each one as
`deferred: backup running (record <id>, started <time>)`.
Other jobs proceed as usual, and a run whose only unfinished work is these
deferrals exits 3. A backup killed before it finished (`kill -9`, a power loss)
leaves its row `running`, and that job stays deferred until you fix the row. After
checking that no backup of that job is running, mark it failed:
`sqlite3 <history database> "UPDATE backup_runs SET status = 'failed' WHERE id = <id>"`.

**Restoring moved backups.** `bdbackup restore` and `bdbackup history` find a
backup where GFS put it. `history` shows a copy that was removed or replaced as
`unavailable`. Before restoring, every backup file used is checked against its
ledger checksum; with several paths in a stage, the first path that verifies is
used, and the `Backup:` line names it. An xtrabackup chain is prepared from the
stage directory holding it, which keeps the layout of the first stage.

**Ledger copies.** After every applying run, each path of every stage after the
first holds `<gfs job>.ledger.sqlite3`, a consistent copy of the whole history
database, written under a temporary name and renamed into place. A path without
its marker gets none, and the run exits 1. To restore with only a stage left,
copy that file somewhere outside the stages, point `[history] database` at the
copy, and run `bdbackup restore`. Paths in the ledger are absolute, so the
stages must be mounted at the same paths as when the copy was written.

**Known limits.** GFS does not yet keep a journal of a move or delete while it
runs, so a few failures leave work for the operator. The report and the step log
name the paths involved, and each leftover is
removed by hand:

- A move was recorded, but removing the old copy failed. The old copy stays on
  disk, no longer in the ledger, and GFS never touches it again. Delete it.
- A move failed, or the process stopped, after some new copies were renamed into
  place but before the move was recorded. The next run reports
  `refused: final name exists: <path>`. Delete that copy; the next run moves the
  unit again.
- A delete failed part way. The next run reports `refused: missing location …`
  for a copy already removed. Delete its row with
  `sqlite3 <history database> "DELETE FROM gfs_locations WHERE path = '<path>'"`.
  If that was the unit's last row, also mark the unit deleted, or later runs
  report `refused: stage not configured`:
  `sqlite3 <history database> "UPDATE gfs_units SET deleted_at = datetime('now') WHERE id = <unit_id>"`
  (the `unit_id` of the row you deleted).
- A restore stops when the xtrabackup copy at the first path of a stage has
  damaged checkpoints or chain metadata; it does not move on to the next path.
  Prepare the copy at another path of that stage with
  `bdbackup restore --backup <backup> --root <directory holding the copy> -d <new dir>`.

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

MIT License. See [LICENSE](https://github.com/ayder/bdbackup/blob/main/LICENSE).
