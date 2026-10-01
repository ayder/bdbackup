# GFS (grandfather-father-son) backup rotation across storage stages

A GFS job rotates the backups of every other job through a chain of storage
stages (daily, weekly, monthly, yearly) on local disks and NFS mounts, using
the checksummed backup ledger in the history database. This page covers the
stages, preparing stage directories, the safety rules, the dry run, the run
and step logs, ledger copies and the known limits.

Part of the [bdbackup documentation](../README.md).

GFS (grandfather-father-son) rotation moves bdbackup's own backups through a
chain of storage tiers, called stages,
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
[Scheduling with cron](../README.md#scheduling-with-cron)). A run applies its plan
unless `--dry-run` is given.

## Stages and ages

`keep` is an age counted from the backup day: `Nd` days,
`Nw` weeks, `Nm` calendar months, `Ny` years. A backup belongs to the first stage
whose `keep` it is still within; past the last stage it is deleted. Each `keep`
must be longer than the previous one on every calendar (a month counts as 28–31
days), and periods never go backwards along the chain. The first stage has
exactly one path, its period is `daily`, and it is where backup jobs write.

## Periods

A daily stage keeps every backup. A weekly stage keeps the newest
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

## Units and paths

GFS moves a unit as one piece: an archive or dump file, or
an xtrabackup dated directory with its full and every incremental (and its
`Full_Latest` and `.full_success` markers). A unit keeps its path relative to
the first stage, so `/BACKUP/mysql/prod/2026-09-29` becomes
`/NFS/daily/mysql/prod/2026-09-29`. The newest unit of each job always stays in
the first stage, so the next incremental finds its full.

## Preparing stages

Create the stage directories and their markers with:

```bash
bdbackup run -c config.toml -j gfs-main --initialize-structure
```

For each stage path, in order, it reports `<path>: created` or `<path>: exists`,
and for every stage after the first adds `, marker written`. A stage path is
created only when its parent directory already exists, and only its last
component; otherwise it fails with `parent does not exist`. Every path must be
writable by the OS user running the command. Into each path after the first stage
it writes `.bdbackup-destination`, holding the bdbackup version, the GFS job, the
stage number and the time, rewriting an existing marker in place. A failure
prints `FAIL <path>: <reason>` and the other paths are still prepared. It exits
0 when every path is ready, 1 when one failed, and 2 for a usage or configuration
error; it cannot be combined with other `run` options, starts no GFS run, and
writes nothing to the history database. It always ends, also after a failure,
with the reminder `Run --initialize-structure only while every destination's
filesystem is mounted, and as the OS user that runs GFS.`

The parent rule protects a stage path at least two levels below a mount point.
Create one directory on the share by hand while it is mounted, such as
`/NFS/bdbackup`, and put the stage paths inside it: if the share is not mounted,
`/NFS/bdbackup` is missing and the command fails. A stage path directly under
the mount point (`/NFS/daily`) is not protected, because an unmounted mount
point is an existing empty directory. Run the command only while every share is
mounted, and as the OS user that runs GFS. It never checks filesystem types or
mounts.

## Safety

Each later stage path must contain a file named
`.bdbackup-destination`, made by `--initialize-structure` or by hand (an empty
file is still valid); an unmounted mount point is an empty local directory
without it, and GFS writes nothing there (`--validate` reports it). GFS copies to
a temporary name, checks every checksum while reading the source, and removes
the original only after every copy is in place and recorded; a failed copy
removes its temporary copies and the next run retries. Before a move,
every copy of the unit is checked, not only the one copied, and
copies keep the permission bits of every file and directory. If the
ledger lists a unit in two stages (for example after a hand edit),
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

## Dry run

`--dry-run` prints the plan: what would move or be deleted. It moves, deletes
and records nothing, and creates only the job's lock file beside the history
database.

Exit codes: 0 done, 1 a refusal or failure, 2 configuration error, 3 another run
of the same job is in progress, or the only unfinished units were deferred.

## Unmanaged backups

A backup that was unavailable when GFS first saw it (for
example an older run of a file job without `timestamp`, whose archive a later
run replaced) stays unmanaged on every later run: GFS never moves or deletes it,
and the report counts it in `unmanaged`.

## Report

Each run lists every unit it acted on, held, deferred or refused,
then a `Summary:` line with counts and bytes per action. One line per stage
follows, such as `Stage /NFS/daily: 15 units (3200000000 bytes)`, counting where
the units are when the run ends (where they would be, in a dry run), and then
the ledger copies written.

## Step log

Every step is also recorded in the `gfs_steps` table of the history
database, one row per path, with its source, destination, outcome
and reason. A failed step keeps the error message.

## Run log

Every run without `--dry-run` records one row in the `gfs_runs` table:
the GFS job, start and finish time, status, exit code and the Summary counts.
The status is `running` while the run works, `completed` once it reaches its report
(whatever the exit code), and `failed` if the run itself crashed. A row left
`running` shows a run that was cut off; check that run's steps for leftovers
(Known limits below). Each `gfs_steps` row names its run in `run_id`. A dry run
records nothing.

## Running backups

While a job has a backup in state `running` in the
ledger, GFS leaves every backup of that job where it is, whatever its age, and
reports each one as
`deferred: backup running (record <id>, started <time>)`.
Other jobs proceed as usual, and a run whose only unfinished work is these
deferrals exits 3. A backup killed before it finished (`kill -9`, a power loss)
leaves its row `running`, and that job stays deferred until you fix the row. After
checking that no backup of that job is running, mark it failed:
`sqlite3 <history database> "UPDATE backup_runs SET status = 'failed' WHERE id = <id>"`.

## Restoring moved backups

`bdbackup restore` and `bdbackup history` find a
backup where GFS put it. `history` shows a copy that was removed or replaced as
`unavailable`. Before restoring, every backup file used is checked against its
ledger checksum; with several paths in a stage, the first path that verifies is
used, and the `Backup:` line names it. An xtrabackup chain is prepared from the
stage directory holding it, which keeps the layout of the first stage.

## Ledger copies

After every run without `--dry-run`, each path of every stage after the
first holds `<gfs job>.ledger.sqlite3`, a consistent copy of the whole history
database, written under a temporary name and renamed into place. A path without
its marker gets none, and the run exits 1. To restore with only a stage left,
copy that file somewhere outside the stages, point `[history] database` at the
copy, and run `bdbackup restore`. Paths in the ledger are absolute, so the
stages must be mounted at the same paths as when the copy was written.

## Known limits

GFS does not yet keep a journal of a move or delete while it
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
