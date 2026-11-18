# GFS (grandfather-father-son) backup rotation across storage stages

A GFS job rotates the backups of every other job through a chain of storage
stages (daily, weekly, monthly, yearly) on local disks and NFS mounts, using
the checksummed backup ledger in the history database. This page covers the
stages, the `verify` key, preparing stage directories, the safety rules, drift,
the dry run, the run and step logs, ledger copies and the known limits.

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

GFS moves a unit as one piece: an archive, dump or pgBackRest export file, or
an xtrabackup dated directory with its full and every incremental (and its
`Full_Latest` and `.full_success` markers). A unit keeps its path relative to
the first stage, so `/BACKUP/mysql/prod/2026-09-29` becomes
`/NFS/daily/mysql/prod/2026-09-29`. The newest unit of each job always stays in
the first stage, so the next incremental finds its full.

## Skipping checksum re-reads: `verify`

A stage can set `verify = false`:

```toml
[[gfs-main.stage]]
paths = ["/NFS/daily"]
period = "daily"
keep = "20d"
verify = false              # do not read units here again only to checksum them
```

`verify` is `true` or `false` and defaults to `true`. Any other value fails the
configuration with `stage <n>: verify must be true or false`. Every stage takes
it, the first one too. It is set per stage, so local stages can keep verifying
while an NFS stage, where each re-read crosses the network, does not.

GFS recomputes the checksums of a unit's copies at a stage's paths in these
places. With `verify = false` at that stage it skips them:

| Step | What `verify = true` does | `verify = false` |
|---|---|---|
| Deleting a unit | Checksums every location first. | Reads nothing. |
| Moving a unit that has several copies (a stage with several paths) | Checksums the copies other than the one it reads. | Reads nothing. |
| Moving by rename (see Safety) | Checksums the source once before the rename. | Reads nothing. |
| Completing an interrupted move | Checksums the later copies, then the earlier copy, before it is removed. | Reads nothing at that stage. |

Each location follows the flag of the stage it is in, whichever stage GFS moves
it to. After an interrupted move, or when the ledger lists a unit in two stages,
the later copies must hold before the earlier copy is removed. In a
`verify = false` stage holding means that the copy exists and has the right
structure: it is not read. The earlier copy is still checked under its own
stage's flag before it is removed, so a default, local first stage still
checksums it, which is a local read.

Three things do not change:

- The contents check runs at every stage. GFS still refuses a location that is
  missing (an unmounted share lacks the unit) and an entry that does not belong,
  with `refused: missing location` and `refused: unexpected entry`.
- A move by copy hashes its source while it reads it for the copy, which costs no
  second read. A source that changed is still `refused: checksum mismatch`.
- `restore` checks every file it uses against the ledger, whatever the flag.

What to expect from `verify = false`:

- A unit that changed, or was replaced at its path, in a `verify = false` stage is
  deleted when it expires, where a default stage would refuse it with
  `refused: checksum mismatch`.
- A unit renamed out of a `verify = false` stage is not read either. With
  `verify = false` on every share stage, and the shares mounted once, GFS reads a
  unit when it copies it onto the first share, and again only when you restore it.
- The report marks the stage with `checksums not re-read` (see Report), and
  `bdbackup_gfs_stage_verify` is 0 for it in the metrics. Drift (below) still tells you
  when a unit is missing from a share.

## Preparing stages

Create the stage directories and their markers with:

```bash
bdbackup run -c config.toml -j gfs-main --initialize-structure
```

For each stage path, in order, it reports `<path>: created` or `<path>: exists`,
and for every stage after the first adds `, marker written`. The first stage gets
its marker too when a [pgbackrest job](README_PGBACKREST.md) writes into it
(see below). A stage path is
created only when its parent directory already exists, and only its last
component; otherwise it fails with `parent does not exist`. Every path must be
writable by the OS user running the command. Into each path after the first stage
(and into the first stage, when a pgbackrest job writes into it) it writes
`.bdbackup-destination`, holding the bdbackup version, the GFS job, the
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
a temporary name, checks every checksum while reading the source (the copy's own
read, with no second one), and removes the original only after every copy is in
place and recorded; a failed copy removes its temporary copies and the next run
retries. Before a move out of a stage with several paths, the other copies of the
unit are checked, not only the one copied; `verify = false` skips the checksum
there. Copies keep the permission bits of every file and directory. If the
ledger lists a unit in two stages (for example after a hand edit, or after an
interrupted move), the next run removes the earlier copy only after the later
copies hold: they are checksummed, or in a `verify = false` stage checked for
existence and structure only. Each location is checked under its own stage's
flag.

A move within one filesystem is a rename. When the target stage has one path, the
unit has one location, and the two stage paths are on the same device (the same
`st_dev`, compared live when the run moves the unit), GFS renames the unit and
reads and writes none of its data. A move from a local disk onto NFS, a move onto
a stage with several paths and a move of a unit the ledger lists in two stages
still copy. Before the rename GFS checks the unit under its own stage's flag: a
verifying stage checksums it once, a `verify = false` stage reads nothing.
The rename takes the same engine locks as the commit of a copy, refuses a final
name that exists (`rename` would replace a file or an empty directory without a
word), and syncs both directories. When one filesystem is mounted at two paths the
rename can fail with `EXDEV`; GFS then copies, and reads a verifying stage's unit
a second time. The report line of an applied rename ends `, renamed)`. A dry run
cannot tell a rename from a copy, so it prints its line as before. A rename cut
short leaves the unit at its final name while the ledger still names the source;
see Final name exists.

On a share that maps every user to one account (an NFS export that squashes
all users to `nobody`, for example), the share can refuse the `chmod` that gives
a copy its permission bits. GFS then keeps the copy with whatever bits the share
assigned and goes on; the run prints
`permission bits not kept on <n> entries: the destination refused chmod` after
its summary. Any other error from `chmod` still fails the copy.

Copies hold no backup lock,
so a slow NFS copy never blocks a backup; if an xtrabackup root is locked when
GFS commits, the unit is reported `deferred: locked` and retried next run. GFS
only touches backups recorded in the ledger. It refuses, and reports:

- `refused: checksum mismatch`: the backup changed since it was recorded. To
  accept the change, set the `checksum` column of its `backup_runs` rows to the
  new value with `sqlite3`; the next run acts on it. A backup that changed
  before GFS first saw it is never managed. A `verify = false` stage does not
  read the unit, so a changed unit there is deleted, not refused; a move by copy
  still refuses a source that changed.
- `refused: missing location …`: a location the ledger lists is not there, for
  example a unit removed by hand or a share that lacks it. This check runs at
  every stage.
- `refused: unexpected entry …`: a unit holds something that is not one of its
  recorded backups or engine markers, such as a leftover `.tmp` directory, or a
  symlink or special file inside a backup. This check runs at every stage.
- `refused: stage not configured`: the unit lives under a stage no longer in the
  configuration. Removing a stage never deletes its backups; restore the stage
  or remove them yourself. Changing the first stage's path keeps managing units
  already moved to later stages; units left under the
  old first-stage path are no longer managed.
- `refused: destination not ready …`: a stage path lacks its marker.
- `refused: final name exists: <path>; see docs/README_GFS.md, "Final name exists"`:
  the unit's final name is taken. See Final name exists.

Backup jobs writing into the first stage must not delete or overwrite on their
own: xtrabackup jobs set `retention_days = 0` and file jobs set
`timestamp = true`. Configuration validation enforces both, rejects stage paths
that overlap, and keeps the live history database out of stage paths.
A [pgbackrest job](README_PGBACKREST.md) deletes and overwrites nothing on its own,
apart from its own temporary files (each tar's name carries its backup label), so it needs
no such setting.

## A first stage fed by a pgbackrest job

A `pgbackrest` job writes its tars into the first stage of a GFS job: its
`out_dir` must be that path or a directory below it, and the config is rejected
otherwise. A GFS job with a single stage is valid, so a first stage on an NFS
share can age its tars alone, for example with `keep = "14d"`. Such a first stage
follows the rules of a later stage:

- It needs the `.bdbackup-destination` marker. `--initialize-structure` writes it
  (stage 1), `--validate` of the GFS job and of the pgbackrest job reports it, and
  the pgbackrest job fails before it creates any directory or file at or below the
  stage when the marker is missing, so an unmounted share is never written to.
- It receives the ledger copy (see Ledger copies), under the same marker
  condition.

A first stage that no pgbackrest job writes into behaves as before: it has no
marker and no ledger copy.

## Final name exists

A move that is cut short, by a crash or a power loss between the move and its
entry in the ledger, leaves a unit at its final name. The next run finds the name
taken and reports:

```
refused: final name exists: <final>; see docs/README_GFS.md, "Final name exists"
```

GFS removes nothing and moves nothing until you decide which copy is the unit.
Look at the source, the path the ledger lists for the unit:

```bash
sqlite3 <history database> "SELECT l.id, l.stage_path, l.path FROM gfs_locations l JOIN gfs_units u ON u.id = l.unit_id WHERE u.relative = '<unit path relative to the first stage>'"
```

- **The source is gone.** The move was a rename, and the unit is at `<final>`.
  Point the ledger at it:

  ```bash
  sqlite3 <history database> "UPDATE gfs_locations SET stage_path = '<target stage path>', path = '<final>' WHERE path = '<source>'"
  ```

  The row keeps its `identities`, which a rename does not change. The next run
  goes on from there.
- **The source is still there.** The move was a copy, and `<final>` is a complete
  copy that was never recorded. Remove it, for example `rm -r <final>`. The next
  run copies the unit again.

How to recognise it without the log:

- `final_name_exists` is in the last run's reason classes
  (`bdbackup_gfs_last_run_reason_units{reason="final_name_exists"}`).
- After a rename, drift shows 1 missing in the source stage (the path the ledger
  names) and 1 unknown in the target stage (the final name). After a copy it shows
  1 unknown in the target stage. See Drift.

The window is as wide as the move. A setup with a single stage never renames, so
the rename case needs a second stage on the same filesystem. A copy has the same
window between its last rename and the ledger.

## Drift

After its actions, and before the ledger copies, a run compares each stage path
with the ledger. It reads names and metadata only, never a unit's data. Drift is a
report, and never changes the exit code or stops an action.

- **Missing**: a location the ledger lists that is not on disk (`lstat` fails). Every
  stage path is looked at, units that only stay in their stage included, so a unit
  removed by hand shows the night after.
- **Unknown**: an entry in a stage path that the ledger does not list, found by a
  walk of the path. Only a path that carries the destination marker is walked: the
  later stages, and a first stage a pgbackrest job writes into. A first stage no
  pgbackrest job writes into is checked for missing locations when its path exists,
  and not walked.

The walk never follows a symlink: a link is one entry. It never enters another
device: a directory on another mount is one entry. It never enters a unit the ledger
lists, and it counts a stray directory as one entry, not its contents. A directory
that holds nothing it would count is not an entry: GFS makes the parent directory of
a unit and never removes it, so the parent of a unit that moved on stays, empty or
with its `.bdbackup.lock`. It does not count:

- the marker `.bdbackup-destination` and the ledger copy `<job>.ledger.sqlite3`;
- `.bdbackup.lock` and bdbackup's temporary names (`.gfs-tmp-*`, and the temporary tar of a
  pgbackrest export);
- the path of any successful backup record, so a backup GFS does not manage is not
  unknown (see Unmanaged backups);
- `lost+found` and `.snapshot`, by name at any level, which a filesystem or an
  appliance keeps in its directories.

Nothing else is ignored. In particular, the `<archive>.lock` of a file job in a first
stage that a pgbackrest job feeds counts as unknown, so keep file jobs out of such a
stage.

The report prints a line for each stage path with a non-zero count, in an applying
run and in a dry run:

```
Drift /NFS/daily: 1 missing, 0 unknown
Drift /BACKUP: 1 missing
```

The second form is a first stage that is not walked. The lines follow the `Stage`
lines. A dry run writes no facts. The counts go to the metrics (see
[Prometheus metrics](README_METRICS.md#gfs-last-run-facts-and-drift)).

A path that should carry its marker and lacks it, such as an unmounted share, is not
checked and prints no line, so an unmounted share is not read as every unit missing.
A first stage that no pgbackrest job writes into has no marker to tell: if it is a
share that is not mounted, its units read as missing. Keep that stage on local disk,
or mount it. A path that cannot be read prints `drift not checked: <path>: <reason>`
and is not counted.

The check runs in a child process under one deadline: `[metrics] timeout_seconds`
when the config has `[metrics]`, 60 seconds otherwise. When it passes, the child is
killed, the stages it had not finished are not checked, and the report says
`drift not checked: timeout after <n> s`. See Known limits.

## Dry run

`--dry-run` prints the plan: what would move or be deleted, and the drift lines.
It moves, deletes and records nothing, and creates only the job's lock file beside
the history database.

Exit codes: 0 done, 1 a refusal or failure, 2 configuration error, 3 another run
of the same job is in progress, or the only unfinished units were deferred.

## Unmanaged backups

A backup that was unavailable when GFS first saw it (for
example an older run of a file job without `timestamp`, whose archive a later
run replaced) stays unmanaged on every later run: GFS never moves or deletes it,
and the report counts it in `unmanaged`. The metrics count unmanaged backups by
reason: `no_checksum` (a record with no unit or no checksum) and `unavailable`
(missing or replaced when GFS first saw it).

## Report

Each run lists every unit it acted on, held, deferred or refused,
then a `Summary:` line with counts and bytes per action. A move by rename ends
`, renamed)`, for example `(within 20d, renamed)`. One line per stage
follows, such as `Stage /NFS/daily: 15 units (3200000000 bytes)`, counting where
the units are when the run ends (where they would be, in a dry run). A stage with
`verify = false` adds `, checksums not re-read` to its line. The `Drift` lines
(see Drift) come next, and then the ledger copies written.

## Step log

Every step is also recorded in the `gfs_steps` table of the history
database, one row per path, with its source, destination, outcome
and reason. A failed step keeps the error message.

## Run log

Every run without `--dry-run` records one row in the `gfs_runs` table:
the GFS job, start and finish time, status, exit code and the Summary counts.
The status is `running` while the run works, through the drift check and the ledger
copies, `completed` once they end (whatever the exit code), and `failed` if the run
itself crashed. The copy of the ledger that goes to each stage already shows the run
as `completed`, with the counts it had then. A run killed before it finished leaves
its row `running`; the next run of the job marks it `failed`, because it holds the
job lock. Check that run's steps for leftovers (Known limits below). Each `gfs_steps`
row names its run in `run_id`. A dry run records nothing.

Every such run also records its outcome in the `last_runs` table (history schema 6), one row
per job: its end, its exit code and a result. The results are `completed` (exit 0), `failed`
(exit 1), `deferred` (exit 3, only deferred work remained), `locked` (exit 3, another run of
the job is in progress) and `error` (the run raised). A run that finds the job lock busy
records `locked` and changes nothing else.

A run that finishes also stores its facts in the row, for `bdbackup metrics`: the time
they were computed, the units it held, deferred, refused or failed by stage and reason
class, its moves by rename and by copy, and its drift. A locked run, a run that raised
and a dry run leave the facts of the last run as they were.

## Metrics

`bdbackup metrics` writes a Prometheus textfile with the last run of each gfs job, the units
in each stage, the stage settings, the readiness and capacity of every destination, how
completely each backup job fills its stages, the reason classes and drift of the last run,
and when the ledger copy was written. The stage figures come from the history database and
equal the `Stage` lines of the run's report. See [Prometheus metrics](README_METRICS.md#gfs).

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
ledger checksum, whatever the stage's `verify` flag; with several paths in a stage,
the first path that verifies is used, and the `Backup:` line names it. An xtrabackup chain is prepared from the
stage directory holding it, which keeps the layout of the first stage.

## Ledger copies

After every run without `--dry-run`, each path of every stage after the
first holds `<gfs job>.ledger.sqlite3`, a consistent copy of the whole history
database, written under a temporary name and renamed into place. So does the
first stage, when a pgbackrest job writes into it. The copy shows the run's own
row as finished. A path without its marker gets none, and the run exits 1. To restore with only a stage left,
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
- A move failed, or the process stopped, after the unit was renamed, or after some
  new copies were renamed into place, but before the move was recorded. The next
  run reports `refused: final name exists: <path>`. After a rename, point the
  ledger at the unit; after a copy, delete the copy. Final name exists has both
  fixes.
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

A run can still hang on a hung hard mount. After its actions it writes the ledger
copy to every later stage path (Ledger copies), and a hard mount that stops
answering blocks that write for good. The drift check runs under a deadline, but
the deadline does not make the run hang-proof, since the copy step comes after it.
Such a run shows in `bdbackup_job_running_since_timestamp_seconds`, because its row stays
`running` until the step ends; alert on that (see
[Prometheus metrics](README_METRICS.md#staleness-and-alerting)). Making the run itself
hang-proof is left to a later release.

A remount can also make units on an NFS share look unavailable. Their recorded
identity includes the device number (`st_dev`), which NFS assigns at mount time,
so a remount or a reboot can change it. This affects every NFS stage, not only
pgbackrest tars. It is documented, not fixed:

- `bdbackup history` shows the units on the share as `unavailable`, and history
  restore refuses them. `bdbackup_history_units{state="unavailable"}` counts them in the
  metrics, so a remount is visible in monitoring (see
  [Prometheus metrics](README_METRICS.md#history-units)). Restore a tar with `bdbackup restore --archive` and an
  xtrabackup directory with `bdbackup restore --backup`; for a pgBackRest tar,
  `--archive` also prints the restore command (see
  [the pgbackrest document](README_PGBACKREST.md#when-the-repository-host-is-lost)).
- If the remount falls between a backup job writing a unit into the first stage
  and the next GFS run, GFS sees the unit as unavailable when it first judges it
  and treats it as unmanaged for good (see Unmanaged backups). It never moves or
  prunes it; delete it by hand.
- Units GFS already manages keep being moved and pruned after a remount. Before it
  moves or deletes a unit, GFS verifies it by checksum, not by identity; a stage with
  `verify = false` checks only that the unit exists and has the right structure. A
  remount onto the wrong export shows in drift as the units of the stage missing.
