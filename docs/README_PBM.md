# MongoDB backups with Percona Backup for MongoDB

A `pbm` job asks Percona Backup for MongoDB (PBM) to take a full logical or
physical backup and waits for it to finish. bdbackup confirms the backup's
type and status, then exports that backup's metadata and data tree as one
tar. It records the tar's size and SHA-256 in SQLite. You can rotate the tar
with GFS and send it to an SSH server with rsync or scp.

PBM creates and manages its source files. bdbackup opens them read-only,
copies only the backup it took in the current run, and writes its tar
separately. Retention of those source files is a PBM task.

## Install and connect

Install the `pbm` command and run agents at the same PBM version. A physical
backup requires Percona Server for MongoDB. The tested pairings are:

| MongoDB | PBM |
|---|---|
| 6.0 | 2.10.0 |
| 7.0 | 2.17.0 |

Validation prints the PBM version, notes versions outside the tested list,
and repeats PBM's own compatibility warnings. A version note does not fail
validation or prevent a backup. Version reporting comes from the PBM
executable and PBM's warnings.

Provide a replica-set connection string in `PBM_MONGODB_URI` in the environment
of the scheduled run. If you set `mongodb_uri` in TOML, it takes precedence.
Keep that file at mode `0600`. bdbackup passes the connection string in the
PBM process's environment and keeps it out of commands, logs and history.
Leave out `directConnection=true` so PBM can reach the replica set from a
secondary as well as from the primary.

## Where the storage is

Set PBM's main storage type to `filesystem`. The directory at `storage_path`
must contain PBM's `.pbm.init` marker and be visible from the host running
bdbackup. Run bdbackup as an OS user that can search the directories and
read the files that PBM writes, such as the user running `mongod` and its PBM
agent on the storage node. Check permissions under that account.

- **Storage on one node.** For example, mount the filesystem on one secondary
  and run bdbackup there. Agents on nodes without the storage report storage
  failures, so PBM chooses the agent that can write the backup. Validation
  lists those failed agents and accepts the cluster if at least one agent
  is healthy. For a restore, copy the backup to storage every target node
  can read.
- **Shared storage.** Mount the same filesystem at the same path on every
  replica-set node. bdbackup can run on any host with a readable mount,
  including a secondary.

Separate local directories at the same path on every node can make otherwise
healthy agents eligible. PBM can choose another secondary, even if you give
one node priority. If the files are on a different node, the run names the
node PBM selected and fails without publishing a tar.

## Configure a job with rotation and SSH copy

The example keeps PBM's files in `pbm-storage`, writes the tar below the
first GFS stage, and copies the published tar to a dedicated SSH directory.
Paths here are relative to the TOML file. Substitute the mount paths and SSH
destination for your environment.

```toml
[history]
database = "state/history.sqlite3"
restore_root = "recovery"

[mongo-nightly]
type = "pbm"
storage_path = "pbm-storage"
out_dir = "exports/daily/mongo"
backup_type = "logical"
# binary = "pbm"
# Set PBM_MONGODB_URI in the scheduler's environment, or set mongodb_uri here.

[mongo-nightly.copy]
destination = "ssh://backup@vault.example.com/srv/bdbackup/mongo-nightly"
method = "rsync"

[rotate-mongo]
type = "gfs"

[[rotate-mongo.stage]]
paths = ["exports/daily"]
period = "daily"
keep = "5d"

[[rotate-mongo.stage]]
paths = ["exports/weekly"]
period = "weekly"
keep = "8w"
```

| Key | Meaning |
|---|---|
| `type` | Set to `pbm`. |
| `storage_path` | Required. PBM's filesystem storage as seen by this host. |
| `out_dir` | Required. Destination for the tar. The filesystem must support hard links for publication. To rotate the tar, put this path at or below the first GFS stage. |
| `backup_type` | `logical` by default, or `physical`. |
| `binary` | Path or name of the PBM executable. Defaults to `pbm`. |
| `mongodb_uri` | Optional replica-set connection string. Uses `PBM_MONGODB_URI` when omitted. |
| `copy` | Optional SSH destination and transfer settings. See the [SSH copy guide](README_COPY.md). |

A PBM job requires `[history]`. Its name can contain ASCII letters, digits
and `. _ - + = @ , % ~`. Keep `storage_path` separate from `out_dir`, the
history database, the configured recovery roots and every GFS stage. Neither
path in each pair can contain the other. Two PBM jobs may share a storage
path. Schedule them separately because PBM accepts one operation at a time.

## Run and validate

```sh
bdbackup run -c config.toml -j mongo-nightly --dry-run
bdbackup run -c config.toml -j mongo-nightly --validate
bdbackup run -c config.toml -j mongo-nightly
bdbackup history -c config.toml --job mongo-nightly
```

The dry run prints the backup type, output path and configured copy. It
starts neither PBM nor SSH and writes nothing. Validation checks the
executable, connection and agents, filesystem storage, local storage marker,
and output permissions without taking a backup or editing PBM settings.

A successful run prints `Job <job>: <path>` and records `pbm-logical` or
`pbm-physical`. The tar name drops the colons from the PBM timestamp, for
example `mongo-nightly-2026-11-25T085927Z.tar`. Inside the tar, PBM's original
names and bytes are preserved. Use `--no-verify` to skip the tar read-back.
The recorded SHA-256 still controls SSH copy verification.

A PBM or export failure exits 1 and leaves no new unit. A run that finds the
same job's lock held exits 3. When an export is interrupted, the next run
takes a fresh PBM backup and clears the job's stale export temp file. PBM
retention still owns the earlier backup in its source storage.

With the stage paths mounted and their parents in place, initialize GFS once,
then schedule rotation after the backup. Prepare the SSH destination and host
key as explained in the [SSH copy guide](README_COPY.md). For more on moves,
expiry and stage markers, see the [GFS guide](README_GFS.md).

```sh
bdbackup run -c config.toml -j rotate-mongo --initialize-structure
bdbackup run -c config.toml -j rotate-mongo
```

## Retention in PBM storage

GFS manages the exported tars. Set a separate policy for PBM's own storage:

- On PBM 2.16 or later, set `lifecycle.enabled` and the desired retention
  periods in PBM's configuration. Preview with
  `pbm cleanup --lifecycle --dry-run`, then apply the policy with
  `pbm cleanup --lifecycle`. [Percona lifecycle settings](https://docs.percona.com/percona-backup-mongodb/2.17.0/usage/backup-lifecycle-management.html)
- For age-based cleanup, use `pbm cleanup --older-than <age>`, for example
  `pbm cleanup --older-than 30d --dry-run` to preview. PBM can remove old
  backups and point-in-time recovery oplog slices. [Percona cleanup guide](https://docs.percona.com/percona-backup-mongodb/2.16.0/usage/schedule-backup.html)

## Restore

Extract from the history ID or use a copy of the standalone tar. bdbackup
prints the backup name and type and the steps for the operator to run.

```sh
bdbackup restore -c config.toml --backup-id 12 --dst recovery/mongo-12 --yes
bdbackup restore --archive mongo-nightly-2026-11-25T085927Z.tar --dst recovery/from-tar
```

1. Put the extracted `<name>.pbm.json` and `<name>/` into filesystem storage
   that every target replica-set node mounts at the same path. Configure PBM
   to use that storage. The restore fails until every node can read both
   entries.
2. Run `pbm config --force-resync --wait` to resync the backup list.
3. Run `pbm list` and confirm that the list contains `<name>`.
4. Run `pbm restore <name>` with the original PBM name, including the colons.
   Follow Percona's steps for that backup type. After a physical restore,
   restart `mongod` and the PBM agents on every node. [Logical restore](https://docs.percona.com/percona-backup-mongodb/2.17.0/usage/restore.html) · [Physical restore](https://docs.percona.com/percona-backup-mongodb/2.17.0/usage/restore-physical.html)

## Limits

- Use local mounts of PBM filesystem storage. Object stores and storage on
  a host that bdbackup cannot access are outside this engine's scope.
- The engine exports full logical or physical backups. Incremental, selective
  (`--ns`) and snapshot-based (`external`) backups are outside that scope.
  The tar omits `pbmPitr/` oplog chunks, so it is for snapshot restore.
- The operator runs the restore. The documented workflow uses the same
  topology and replica-set names. Sharded clusters are outside the test matrix.
- Use the scheduler's limits to bound a hung PBM invocation. The PBM job
  records backups in history and has no dedicated metric family or
  `last_runs` entries.
