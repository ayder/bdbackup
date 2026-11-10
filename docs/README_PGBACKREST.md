# PostgreSQL pgBackRest backups: nightly fulls exported to a GFS stage

A pgbackrest job copies the newest full backup of one pgBackRest stanza into the first
stage of a GFS job as a single tar file. GFS then ages the tars, for example 14 days on an
NFS share. Each tar restores on its own, without the WAL archive or any other backup. This
page covers the job keys, how to take the fulls, a worked example, the first stage, what a
run does, the dry run and validation, history and restore, and the known limits.

Part of the [bdbackup documentation](../README.md).

## What it does and does not do

pgBackRest keeps a local repository on the repository host: fulls, differentials and the
WAL archive. Point-in-time recovery stays with pgBackRest. bdbackup copies only the newest
full, once per backup label, so that a copy of it lives off the host.

- bdbackup does not take the full. Your own cron keeps running
  `pgbackrest --type=full backup`.
- bdbackup does not replace pgBackRest's schedule or retention.
- bdbackup writes, renames, deletes and locks nothing under `repo_path`. It opens every
  file there read-only and takes no pgBackRest lock.

Not supported: WAL, differential or incremental backups on the share, and point-in-time
recovery from it; object-store repositories (S3, GCS, Azure); a repository on another host
(`repo1-host`); restoring over SSH; and encrypted repositories (see
[Encrypted repositories](#encrypted-repositories)). The repository must be a local POSIX
directory.

## Configuration keys

| Key | Required | Description |
|-----|----------|-------------|
| `stanza` | yes | The pgBackRest stanza. One job exports one stanza. |
| `repo` | no (default `1`) | The index of the local POSIX repository in the host's `pgbackrest.conf`. The `info` command the job runs gets `--repo=<repo>`, so it lists only that repository's backups, even when the host also configures another repository such as S3. |
| `repo_path` | yes | The repository root (what pgBackRest calls `repo<repo>-path`). The job reads `backup/<stanza>/` and `archive/<stanza>/` there and passes it as `--repo<repo>-path` to the `info` command, so `info` and the job read the same repository. Relative paths resolve against the config file. |
| `out_dir` | yes | Where the tar is written. It must be the first stage path of a GFS job in the same config, or a directory below it. |
| `binary` | no (default `pgbackrest`) | The pgBackRest executable. |
| `config_file` | no | Passed as `--config` to the `info` command the job runs. When it is not set, pgBackRest's default applies. (`pgbackrest version` runs without any of these options.) |
| `max_full_age_hours` | no (default off) | A positive whole number. See [When there is no new full](#when-there-is-no-new-full). |

A pgbackrest job needs a `[history]` section; without one the config is rejected
(`pgbackrest jobs require a [history] section`). `first_stage` and `lock_file` are set by
bdbackup and are rejected in the config. `out_dir` must be outside `repo_path`, and no GFS
stage path may lie inside `repo_path` or contain it. Config paths expand `~`.

Run the job as the OS user that owns the repository, never as root. From pgBackRest 2.59.0,
pgBackRest itself refuses every command except `restore` as root by default. The job
refuses root too:
`pgbackrest jobs do not run as root; run as the OS user that owns the repository`.

## Take the fulls with archive-copy

The tar restores without the WAL archive only if the full holds the WAL it needs. That is
what pgBackRest's archive-copy does. Take every full with `--archive-copy` on the command
line, or set `archive-copy=y` for the backup command in `pgbackrest.conf`:

```ini
[global:backup]
archive-copy=y
```

If your cron runs `pgbackrest --type=full backup` today, it has to change: add
`--archive-copy` to the command, or add the line above to the configuration.

bdbackup reads the flag (`option-archive-copy`) from the full's entry in `backup.info`.
When the newest full was taken without it, the run fails (exit 1) and writes nothing:

```
full 20261109-010203F was taken without archive-copy; take fulls with --archive-copy or archive-copy=y
```

It does not fall back to an older full. The next full taken with archive-copy is accepted.

## A worked example: one NFS stage, 14 days

```toml
[history]
database = "state/history.sqlite3"

[pg-main]
type = "pgbackrest"
stanza = "main"
repo_path = "/var/lib/pgbackrest"
out_dir = "/mnt/backup/pg/main"        # inside the first stage of pg-gfs
max_full_age_hours = 30

[pg-gfs]
type = "gfs"

[[pg-gfs.stage]]
paths = ["/mnt/backup/pg"]             # a directory on the NFS share
period = "daily"
keep = "14d"
```

`/mnt/backup/pg` is the only stage, so GFS keeps each tar for 14 days and then deletes
it. The newest tar of each job always stays. After each GFS run, `pg-gfs.ledger.sqlite3`,
a copy of the whole history database, is written into `/mnt/backup/pg`, so the history
exists off the host too.

Prepare the stage once, while the share is mounted, then check and run:

```bash
bdbackup run -c /opt/dbs/config.toml -j pg-gfs --initialize-structure
bdbackup run -c /opt/dbs/config.toml --validate
bdbackup run -c /opt/dbs/config.toml -j pg-main --dry-run
bdbackup run -c /opt/dbs/config.toml -j pg-main
bdbackup run -c /opt/dbs/config.toml -j pg-gfs
```

A crontab for the repository's OS user, with the export after the full and GFS after the
export:

```cron
0 1 * * *   pgbackrest --stanza=main --type=full --archive-copy backup
0 3 * * *   bdbackup run -c /opt/dbs/config.toml -j pg-main
30 4 * * *  bdbackup run -c /opt/dbs/config.toml -j pg-gfs
```

The export may run at any time. pgBackRest lists only completed backups, so a full that is
still running is never picked; the export copies the previous full.

A repository with several stanzas gets one pgbackrest job per stanza, each with its own
`out_dir` (for example `/mnt/backup/pg/orders`) inside the same first stage. GFS selects
per job, so the stanzas never compete for a slot.

GFS dates a tar by the time its export completed, not by the full's stop time. With
nightly fulls taken shortly before the export, both fall on the same day. Keep that in mind
when you add weekly, monthly or yearly stages: an export that runs after midnight is dated
the next day.

## Preparing the first stage

`run -j pg-gfs --initialize-structure` also writes the `.bdbackup-destination` marker
(stage 1) into a first stage that a pgbackrest job writes into, and reports
`<path>: created, marker written` or `<path>: exists, marker written`. A GFS job whose
first stage no pgbackrest job writes into behaves as before: its first stage has no marker
and needs none.

Before the job creates any directory or file, it requires `<first stage>/.bdbackup-destination`
to be a regular file. If the share is not mounted, its mount point is an empty local
directory without the marker. The run fails with exit 1 and creates no directory or file
at or below the stage (a real run still records a `failed` row in the history database):

```
/mnt/backup/pg: .bdbackup-destination missing; run --initialize-structure for its gfs job
```

Only after that check does the job create the directories between the first stage and
`out_dir`. A dry run makes the same check when it would export. `--validate` of the GFS job
and of the pgbackrest job both report the marker. Run `--initialize-structure` only while
the share is mounted.

## What a run does

1. Refuse root, then take the job lock. The lock file is
   `<history database>.pgbackrest-<job>.lock`, beside the history database and never on the
   share. A second run of the same job exits 3.
2. Run `pgbackrest [--config=…] --repo=<repo> --repo<repo>-path=<repo_path> --stanza=<stanza>
   info --output=json` and pick the newest full by stop time. Differentials and incrementals
   are ignored.
3. Refuse, with exit 1 and no history row, an encrypted repository, a stanza with no full
   (`stanza main has no full backup`) and a full taken without archive-copy.
4. If this job already has a successful record for the full's label, there is nothing to
   export (see below). Otherwise write the tar, read it back once to verify it, and record
   the run in the history.

The tar is `<out_dir>/<stanza>-<label>.tar`, for example
`/mnt/backup/pg/main/main-20261109-010203F.tar`. It is not compressed: the repository's own
compression stays as pgBackRest wrote it. Its members have paths relative to `repo_path`,
so the extracted directory works as `--repo1-path`:

- `backup/<stanza>/backup.info` and `backup/<stanza>/backup.info.copy`
- `backup/<stanza>/<label>/`, the whole directory
- `archive/<stanza>/archive.info` and `archive/<stanza>/archive.info.copy`

The copied `backup.info` lists other backups that are not in the tar. That is expected;
pgBackRest restores the one label you name.

The job reads each info file together with its `.copy` after the full's directory, and
accepts the pair only when the two files are byte-identical, neither changed while it was
read, and `backup.info` still lists the label. If a pair is not consistent, because a
differential or an expire rewrote it, the job reads it again, up to five attempts one
second apart. If a full changes or disappears while it is exported (an expire removed it,
for example), the run fails with `input changed during export: <path>` and nothing is
published. Run it again.

The tar is written under a temporary name, synced, renamed into place, and the directory is
synced. A temporary file that an earlier run of the same stanza left behind is removed at
the start of the next run. The final name is never replaced: if it already exists without a
matching history record, the run fails with `unit already exists: <path>`.

Over NFS this costs one write and one read per tar. The SHA-256 is computed while the tar
is written, and the verification reads the tar back once, walking its structure and
hashing its bytes. The two hashes must match, and the history records the one computed
while writing. `--no-verify` skips the read-back.

A failure after the full was chosen (the marker check, the write, the read-back, a full
that changed) records a `failed` row and exits 1. It does not count as exported, so the
next run tries the same full again. A refusal records nothing.

## When there is no new full

If this job already exported the newest full, a run writes no history row and prints the
label and the age of that full, then exits 0:

```
no new full: newest 20261109-010203F, 2 hours old
```

Nothing is recorded for such a run. A nightly export that finds no new full every night
would therefore look healthy, so set `max_full_age_hours`. When the newest full is more
than that many hours old, the same message gets a suffix and the run exits 1 instead:

```
no new full: newest 20261109-010203F, 32 hours old (older than max_full_age_hours = 30)
```

The age check applies only when the newest full was already exported (no new full). When
a new full is exported, it does not apply. For nightly fulls, pick a limit a few hours over
24.

## Page checksum errors

When pgBackRest recorded invalid page checksums for the newest full (`"error": true` in
its `info` output), the full is still exported. Every run that sees that full as the newest
prints this line first, a dry run and a `no new full` run included:

```
WARNING: full 20261109-010203F has page checksum errors; it is exported, and restoring it needs checksum-page-error=n
```

The record holds `page_errors = true`, and the restore command adds
`--checksum-page-error=n` (see [Restore](#restore)).

## Dry run and validation

`run -j pg-main --dry-run` is the one dry run that runs a tool: it runs only the read-only
`pgbackrest info` call and reads `backup.info`. It writes nothing and takes no lock. It
prints either the `no new full` message above, with the same exit codes, or:

```
Dry run; would export 20261109-010203F (7340032 bytes) of stanza main to /mnt/backup/pg/main/main-20261109-010203F.tar
```

The size is the full's size in the repository. It refuses what a real run refuses, and
makes the marker check when it would export.

`run --validate -j pg-main` runs only the read-only `pgbackrest version` and `info` commands
and writes nothing. It reports, one line each:

- the `pgbackrest` binary is on `PATH`;
- `pgbackrest version` parses. The version is reported, with
  `(not a tested version: 2.58.0, 2.60.0)` when it is another one. That note is not a FAIL;
- the user is not root;
- the `info` command succeeds and lists the stanza;
- the repository is not encrypted;
- the user can read and search `backup/<stanza>/` and `archive/<stanza>/` under
  `repo_path`;
- the marker is present in the first stage;
- the output directory is writable, or can be created under an existing parent inside the
  first stage.

## Restore

The restore was tested against pgBackRest 2.58.0 and 2.60.0.

`bdbackup history` lists the exports as `pgbackrest-full`, with the tar as path and unit.
Restore one from the history like any other backup:

```bash
bdbackup restore -c /opt/dbs/config.toml --job pg-main
```

It checks the tar against its recorded checksum and extracts it into the new destination,
which becomes a directory that works as `--repo1-path`. Then it prints the command that
restores the cluster:

```
Restored to: /restore/bdbackup/pg-main-12

To restore this backup, run this pgBackRest command:
pgbackrest --no-config --stanza=main --repo1-path=/restore/bdbackup/pg-main-12 --pg1-path=<PGDATA> --set=20261109-010203F --type=none --archive-mode=off --log-level-file=off restore

  - <PGDATA> is a new, empty directory on the host that restores.
  - Run the command as the OS user that will own the cluster, never root. Unset every
    PGBACKREST_* environment variable first: pgBackRest reads them like options.
  - /restore/bdbackup/pg-main-12 belongs to the user who ran bdbackup (a history restore creates
    it with mode 0700). To restore as another user or on another host, copy it there first and
    change --repo1-path to the copy.
  - Start the restored cluster with PostgreSQL 16.
  - archive_mode must be off before the restored cluster starts for the first time. With
    --type=none the cluster stays on the production timeline, and one archived WAL segment would
    collide with production WAL. The command passes --archive-mode=off for this.
```

The command is built from the extracted directory, not from the history or the file name:
the stanza and the label are the directory names, and the PostgreSQL version and the
page-error flag come from the extracted `backup.info`. It assumes nothing about the host
that restores:

- `<PGDATA>` stays a placeholder. Replace it with a new empty directory on that host.
- `--no-config` keeps pgBackRest from loading a config file of the host that runs the
  command, so a stanza of the same name there cannot redirect the restore.
- `--repo1-path` is the extracted directory, so the restore reads nothing else.
- `--type=none` writes no recovery target. The cluster comes up at the end of the backup
  and stays on the production timeline.
- `--log-level-file=off` keeps pgBackRest from writing to a log directory that the cluster
  owner may not be able to write.
- A tar with page checksum errors adds `--checksum-page-error=n` and a line saying why.
  pgBackRest 2.60.0 and later stop a restore on an invalid page checksum unless it is
  given; the restored cluster keeps those pages as they were.

Run the command as the OS user that will own the cluster, never root. The extracted
directory belongs to the user who ran bdbackup. To restore as another user, or on another
host, copy the directory there first, hand it over to that user, and change `--repo1-path`
to the copy. bdbackup changes no ownership or permissions itself.

### When the repository host is lost

`restore --archive` needs no config and no history. Mount the share on another machine
(or copy the tar from it) and extract the tar there:

```bash
bdbackup restore --archive /mnt/backup/pg/main/main-20261109-010203F.tar -d /restore/pg-main
```

When the extracted directory is a pgBackRest unit, it prints the same restore command as a
history restore, with `--repo1-path=/restore/pg-main`. A directory is a unit when it has
exactly one `backup/<stanza>/` directory that holds `backup.info`, exactly one label
directory there, and `archive/<stanza>/archive.info`. Any other tar prints nothing extra.

This is also the way to restore after a remount or a reboot has made `history` and history
restore refuse the unit; see [Known limits](#known-limits).

### Tablespaces

A backup with tablespaces needs `--tablespace-map-all=<directory>` added to the printed
command. Without it pgBackRest restores each tablespace to the path it had on the source
host.

### The restored cluster is a copy of production

The restored cluster is a copy of a production data directory. It carries the production
`shared_preload_libraries` and any jobs stored in the database. The host that starts it
needs the same extension packages, and those jobs run once the cluster starts unless you
disable them first.

## Encrypted repositories

A repository for which `pgbackrest info` reports a cipher is refused in this release. No
passphrase or cipher option is read, stored or printed. The run fails with exit 1 and no
history row:

```
encrypted repositories are not supported in 0.7.3
```

`--validate` reports the same as FAIL.

## The cost of pruning

GFS verifies a unit by checksum before it deletes it. In steady state each night therefore
also reads the expiring tar once, as many bytes as the largest full of the stanza: a
50 GB full costs one more 50 GB read each night. This is existing GFS behaviour.

## Known limits

- A remount or a reboot can make the tars on an NFS share look unavailable, because their
  recorded identity includes the device number that NFS assigns at mount time. `history`
  then shows them as `unavailable` and history restore refuses them. Use
  `restore --archive`. See [GFS Known limits](README_GFS.md#known-limits).
- Units that GFS already manages keep being moved and pruned after a remount. A tar
  exported just before a remount and not yet seen by GFS stays unmanaged for good and is
  never pruned; delete it by hand.
- The behaviour on a real NFS share is proven at unit level only. A share that maps every
  user to `nobody` is handled (GFS copies tolerate EPERM from chmod, see
  [GFS Safety](README_GFS.md#safety)), and so is an unmounted share (the marker).
