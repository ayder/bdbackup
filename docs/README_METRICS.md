# Prometheus metrics: `bdbackup metrics`

`bdbackup metrics` writes one Prometheus textfile that tells you what your backups did and
what state they are in: the last run of each pgbackrest and gfs job, the units in the history,
the GFS stages, the destinations, and the pgBackRest repository with its WAL archive. A
node_exporter on the same host serves the file. This page covers the command, its
configuration, the node_exporter and PMM setup, every metric, alerting, what happens when a
share stops answering, and the known limits.

Part of the [bdbackup documentation](../README.md).

## What it does and does not do

The command is read-only. It writes the `.prom` file, the temporary file it renames into
place, and its own lock file. It never takes a backup, prunes, moves or deletes a unit, or
writes to a destination, to `repo_path` or to the history database. It opens the history
database read-only and never migrates it.

Backup and GFS runs never touch the metrics file, and a failure of `metrics` never changes
the result of a backup or a prune. A run shows up in the file the next time `metrics` runs,
at most one cron interval later.

## The command

```bash
bdbackup metrics -c CONFIG
```

`-c` is the only option. Run it from cron every 10 to 15 minutes, as the OS user that owns the
history database. A pgBackRest diff can run several times a day, so a file written only by the
nightly export would go stale.

```cron
*/10 * * * * bdbackup metrics -c /etc/bdbackup/config.toml
```

On success it prints nothing. Each failed source is one line on stderr, for example
`Metrics: pgbackrest (pg-main) failed: timeout`, and one `bdbackup_metrics_error` series in the
file.

| Code | Meaning |
|------|---------|
| 0 | The file was written and holds no error series. |
| 1 | The file was written with at least one error series, or it could not be written (the error is logged). |
| 2 | Usage error or invalid configuration. When the TOML parses and `[metrics] path` is readable, the file is written first with `bdbackup_metrics_error{source="config",job_name="",reason="invalid"}`; when the TOML does not parse or there is no usable path, nothing is written. |
| 3 | Another `metrics` run holds the lock. Nothing is written and the file is left as it was. |

The lock is `<history database>.metrics.lock`, beside the history database and never in the
node_exporter directory.

## Configuration

```toml
[history]
database = "/var/lib/bdbackup/history.sqlite3"

[metrics]
path = "/usr/local/percona/pmm/collectors/textfile-collector/low-resolution/bdbackup.prom"
timeout_seconds = 60
```

| Key | Required | Description |
|-----|----------|-------------|
| `path` | yes | The file to write. A nonempty path ending in `.prom`; `~` expands and a relative path resolves against the config file. Its directory must exist. |
| `timeout_seconds` | no (default `60`) | A positive whole number of seconds. It limits each wait of a run, see [Hangs and outages](#hangs-and-outages). |

`[metrics]` requires `[history]` and accepts only these keys. `metrics` is a reserved table
name: a job named `metrics` is a configuration error. Only active jobs are reported.

`run --validate` checks the metrics directory with the same checks as the other directories:
the OS user needs write and search access, a missing directory is reported with `mkdir -p`
guidance, and nothing is created. When the file already exists it says so: the file is
replaced as a whole, so only the directory needs write access, not the file.

## node_exporter and PMM

node_exporter's textfile collector reads every `*.prom` file in one directory. Point it at the
directory with `--collector.textfile.directory`, and write `path` there.

PMM 3 clients run a node_exporter of their own. PMM 3.9.1 reads its low-resolution textfiles
from `/usr/local/percona/pmm/collectors/textfile-collector/low-resolution`, which is where to
write `path`. Check the directory on other versions.

That directory belongs to `pmm-agent` and has mode 0755, so the bdbackup OS user cannot write
there until you allow it. bdbackup assumes neither way; choose one, for example an ACL
or a group:

```bash
setfacl -m u:<bdbackup user>:rwx /usr/local/percona/pmm/collectors/textfile-collector/low-resolution
```

The file is written with mode 0644 through a hidden temporary file in the same directory
(`.<name>.tmp-<random>`, which does not end in `.prom`) and a rename, so node_exporter never
reads a half-written file. Write access to the directory is enough to replace the file.

## Which jobs report

- **pgbackrest and gfs jobs** record the outcome of every run that is not a dry run: its end,
  exit code and a result word. This is the `last_runs` table of history schema 6. A pgbackrest
  run that is refused for root records nothing.
- **History unit states** (`bdbackup_history_units`) cover every job name with successful
  records, of any type.
- **File, mysqldump and xtrabackup run outcomes are not reported yet.** Their backups still
  count in the history unit states.

## The metrics

Every metric is a gauge. Timestamps are Unix seconds, sizes are bytes and durations are
seconds. A job is labelled `job_name` and its configured `type` is `job_type`. No series uses
the labels `job` or `instance`, which Prometheus sets on the scrape target. A value that is not
known has no series; it is never written as 0.

Names and label sets are stable. Later releases add series and families, and do not rename a
family or change its labels.

### The run itself

Always written when the file can be written.

| Metric | Labels | Value |
|--------|--------|-------|
| `bdbackup_metrics_last_run_timestamp_seconds` | | End of this `metrics` run. |
| `bdbackup_metrics_success` | | 1 when no error series is written, else 0. |
| `bdbackup_metrics_error` | `source`, `job_name`, `reason` | 1 for each source that failed. `job_name` is empty unless the failure belongs to one job. |

The error sources and reasons:

| `source` | `reason` | Meaning |
|----------|----------|---------|
| `config` | `invalid` | The configuration did not load. |
| `config` | `timeout` | Reading the configuration did not finish in `timeout_seconds`. The file then holds only the three series above. |
| `history` | `unreadable` | The history database could not be read. |
| `destination` | `timeout`, `failed` | The probe that checks the destinations did not finish in `timeout_seconds`, or could not run. |
| `collector` | `failed`, `timeout` | A collector raised or produced samples that do not fit, or did not return from its preparation step in `timeout_seconds`. Its job's other families are absent. |
| `pgbackrest` | `binary_missing`, `timeout`, `failed`, `output_invalid` | The job's `pgbackrest info` call failed. `binary_missing` covers a binary that is missing or not executable. |
| `pgbackrest` | `wal_mtime_unavailable` | The arrival time of the newest WAL segment could not be read. |
| an engine's name | `timeout` | The preparation step of that engine's collector did not return in `timeout_seconds`. |

### Jobs

Written for each active job of a type that records its runs (pgbackrest and gfs). Labels:
`job_name`, `job_type`.

| Metric | Extra label | Value |
|--------|-------------|-------|
| `bdbackup_job_last_run_timestamp_seconds` | | End of the job's last run, dry runs excluded. |
| `bdbackup_job_last_run_exit_code` | | Its exit code. Alert on this across job types. |
| `bdbackup_job_last_run_result` | `result` | 1; the result word of that run. |
| `bdbackup_job_running_since_timestamp_seconds` | | Start of the job's oldest unfinished run; absent when none runs. A run that was killed leaves its row `running`, and GFS defers that job. |
| `bdbackup_job_last_success_timestamp_seconds` | | Completion of the newest successful backup. For a gfs job, the newest run that completed with exit code 0. |
| `bdbackup_job_last_success_bytes` | | Its size. Not written for a gfs job. |
| `bdbackup_job_last_success_phase_seconds` | `phase` | The time one phase of that backup took. A pgbackrest export has `write` (the tar, its fsync and the close) and `verify` (the read-back; absent after `--no-verify`). Written only while the facts of the last run describe the newest successful backup. |

The result words:

| Job type | `result` |
|----------|----------|
| `pgbackrest` | `exported`, `no_new_full`, `full_too_old`, `locked`, `destination_not_ready`, `export_failed`, `verify_failed`, `failed` |
| `gfs` | `completed` (exit 0), `failed` (exit 1: a failure or refusal), `deferred` (exit 3, deferred work only), `locked` (exit 3, another run of the job is in progress), `error` (the run raised) |

### History units

| Metric | Labels | Value |
|--------|--------|-------|
| `bdbackup_history_units` | `job_name`, `state` | The job's successful records, by state. |

The states: `available`; `deleted`, which GFS deleted (read from the database); and
`unavailable`, which is missing or replaced and was not deleted by GFS. A remount that changes
the device number of an NFS share shows up as `unavailable`, see
[GFS Known limits](README_GFS.md#known-limits). The three states are written for every job
with successful records, zero counts included. Availability is judged with the rule `history`
and restore use. When the destination probe does not finish, the whole family is absent.

### Destinations

| Metric | Labels | Value |
|--------|--------|-------|
| `bdbackup_destination_ready` | `job_name`, `stage`, `path` | 1 when `<path>/.bdbackup-destination` is a file, else 0. |

It covers every stage path after the first of each gfs job, and the first stage when a
pgbackrest job writes into it. `stage` is the 1-based index and `job_name` the gfs job that owns
the path. A path the probe did not report in time reads 0. On autofs, the check mounts the
share, so it is also the reachability test.

### GFS

Labels: `job_name`, and the extra ones below. The stage figures come from the history database
only; they never touch a share.

| Metric | Extra labels | Value |
|--------|--------------|-------|
| `bdbackup_gfs_last_run_units` | `action` | The Summary counts of the newest finished run: `moved`, `deleted`, `held`, `deferred`, `refused`, `failed`, `unmanaged`. Absent when that run raised. |
| `bdbackup_gfs_stage_info` | `stage`, `period`, `keep`, `path` | 1; maps a stage index to its settings and first path. |
| `bdbackup_gfs_stage_units` | `stage` | Units whose newest location is in the stage, as the run's `Stage` report line counts them. |
| `bdbackup_gfs_stage_bytes` | `stage` | Their bytes. |

Stage labels are indexes, not paths. A successful record under the first stage that no ledger
row names yet counts as a first-stage unit.

### pgBackRest

Written for each active pgbackrest job. Labels: `job_name`, `stanza`, and the extra ones below.

The last export, from the history database:

| Metric | Extra label | Value |
|--------|-------------|-------|
| `bdbackup_pgbackrest_export_info` | `label` | 1; the label of the newest successful export. |
| `bdbackup_pgbackrest_export_full_age_seconds` | | Seconds from the full's stop time to the start of its export, at least 0. Written only while the facts of the last run describe that export. |

The repository, from one `pgbackrest info --output=json` call per job, made with the job's own
command line and `timeout_seconds`. When the call fails, these series are absent and a
`bdbackup_metrics_error{source="pgbackrest"}` series is written.

| Metric | Extra label | Value |
|--------|-------------|-------|
| `bdbackup_pgbackrest_info_duration_seconds` | | Seconds the call took. |
| `bdbackup_pgbackrest_stanza_status` | | The status code of the stanza; 0 is ok, 1 is a missing stanza, 2 has no valid backup. |
| `bdbackup_pgbackrest_last_backup_timestamp_seconds` | `type` | Stop time of the newest backup of the type (`full`, `diff`, `incr`; ties go to the later label). |
| `bdbackup_pgbackrest_last_backup_bytes` | `type` | Its database size. |
| `bdbackup_pgbackrest_last_backup_repo_bytes` | `type` | Its size in the repository. |
| `bdbackup_pgbackrest_backups` | `type` | The backups of the type the repository holds; 0 when there are none. |

A type with no backup has only the count. A key that `info` does not print leaves out only the
series that needs it.

The WAL archive. The current database is the entry of `info`'s `db[]` with the highest `id`,
and `db_id` is that id. Every WAL series carries it.

| Metric | Extra label | Value |
|--------|-------------|-------|
| `bdbackup_pgbackrest_wal_archive_present` | `db_id` | 1 when the archive entry of the current database has both a `min` and a `max`, else 0. |
| `bdbackup_pgbackrest_wal_archive_max_timeline` | `db_id` | The timeline of the archive max, the first 8 hex digits of its name. |
| `bdbackup_pgbackrest_wal_archive_max_segment` | `db_id` | The segment number of the archive max: its log number shifted left 32 bits, plus its segment number. It only grows, so `increase()` shows progress. |
| `bdbackup_pgbackrest_wal_archive_max_mtime_seconds` | `db_id` | The modification time of the archived file of that segment, which is when it arrived in the repository. |

The max must be 24 hex digits; any other value gives no timeline, segment or time series. The
time is read from one directory listing,
`<repo_path>/archive/<stanza>/<archive id>/<first 16 digits>/`, taking the one entry whose name
starts with the max and a dash. The archive is never walked. When no file, or more than one,
matches, when the listing fails, or when the probe does not finish,
`bdbackup_metrics_error{source="pgbackrest",reason="wal_mtime_unavailable"}` is written and the
time series is absent.

## Staleness and alerting

If cron stops, the file stops changing, and Prometheus keeps serving its last values. Two
series say how old the file is:

- `bdbackup_metrics_last_run_timestamp_seconds`, written by `bdbackup metrics`;
- `node_textfile_mtime_seconds`, written by node_exporter for each file it reads.

Alert when either is older than a few cron intervals.

These expressions are examples. The thresholds are yours.

```promql
# The metrics run is stale (three missed 10 minute runs).
time() - bdbackup_metrics_last_run_timestamp_seconds > 30 * 60

# The metrics run could not read every source.
bdbackup_metrics_success == 0

# A job's last run failed. exit_code works for every job type.
bdbackup_job_last_run_exit_code != 0

# The last successful pgbackrest export is older than 26 hours.
time() - bdbackup_job_last_success_timestamp_seconds{job_type="pgbackrest"} > 26 * 3600

# An export or a GFS run has been running for more than 6 hours (a stuck or killed run).
time() - bdbackup_job_running_since_timestamp_seconds > 6 * 3600

# A destination is not ready.
bdbackup_destination_ready == 0

# Units that GFS did not delete are missing or replaced.
bdbackup_history_units{state="unavailable"} > 0

# pgBackRest reports a problem with the stanza.
bdbackup_pgbackrest_stanza_status != 0

# The newest archived WAL segment is more than 15 minutes old.
time() - bdbackup_pgbackrest_wal_archive_max_mtime_seconds > 15 * 60

# No WAL segment has been archived in an hour.
increase(bdbackup_pgbackrest_wal_archive_max_segment[1h]) == 0
```

An idle database writes no WAL, so the last two need a threshold that fits its traffic, or
`archive_timeout` in `postgresql.conf`. A `no_new_full` result has exit code 0; use the export
age, or `max_full_age_hours`, to notice fulls that stopped.

## Hangs and outages

A share that stops answering blocks any `stat` made against it. `bdbackup metrics` is built so
that it still ends. After the lock, a run has three phases, each limited by `timeout_seconds`,
so a run takes at most about three times that.

1. **Reading the configuration**, in a thread. The configuration is read without resolving any
   path, so loading it makes no filesystem call on a stage, a repository or any other share
   path. If it does not finish, the file holds only the three series of
   [the run itself](#the-run-itself) with `source="config", reason="timeout"`.
2. **The preparation step of each collector**, each in its own thread, all started together.
   The pgBackRest `info` call is here, with its own timeout. A collector that has not returned
   at the deadline is abandoned: it gets the timeout error and its figures are absent, and the
   others go on.
3. **The probe children.** Every check that touches a share runs in a child process: the
   destination markers, the units of the history and the WAL file listing. The `destination`
   and `repository` probes run at the same time under one deadline, so a hung destination does
   not hide the WAL figures, and the reverse. A child that does not finish is killed and not
   waited for.

Collecting, rendering and writing touch no share. A run that abandoned a thread ends with
`os._exit` once the file is written and stderr is flushed, so a thread stuck in the kernel
cannot hold the process. The lock is released with the process, so the next run is not blocked.

During an NFS outage the file shows it, as measured with a hard NFS mount whose server stopped
answering and `timeout_seconds = 3`:

- the run ended with exit 1 in about 3 seconds, and the next run did the same;
- `bdbackup_destination_ready` was 0 for the share's paths;
- `bdbackup_metrics_error{source="destination",job_name="",reason="timeout"}` was 1 and
  `bdbackup_metrics_success` was 0;
- `bdbackup_history_units` was absent, because it needs the destination probe;
- the job and GFS families, which come from the database, were present;
- once the share answered, the next run was exit 0 and the series were back.

Export and GFS runs still hang during an outage. They need the share, and this command does not
change that.

## Known limits

- **Symlinked stage paths.** The configuration is read without resolving paths, so a stage path
  written through a symlink does not match the real paths the history ledger holds, and its
  stage figures read 0. Write the real path in the configuration.
- **Local files.** The config file, the history database, the metrics file and the engines
  directory (`<config dir>/engines`) must be on local disk. They are read before the lock or
  outside the deadline, and a hung mount under them would block the run.
- **Abandoned pgbackrest children.** When `repo_path` itself is on a hung share, the pgbackrest
  process of an abandoned `info` call is sent `SIGKILL` and may stay until the I/O ends.
  The metrics process exits; one such child can remain for each run during the outage.
- **The history check stats every live unit.** `bdbackup_history_units` asks the probe for
  every place of every successful record on every run. On a large history over NFS that is many
  `stat` calls; if the deadline is hit, the whole family is absent and the run exits 1. There is
  no cap.
- **A render failure writes nothing.** If the samples cannot be rendered (a repeated series, for
  example), the run logs one error, exits 1 and leaves the old file, which then goes stale.
  Watch `bdbackup_metrics_last_run_timestamp_seconds`.
- **The first run creates the lock directory.** Before any backup exists, taking the lock
  creates the directory of the history database with the default mode, not 0700. The job locks
  of pgbackrest and gfs runs do the same.
- **The outage test is an emulation.** `scripts/integration_nfs_outage.py` drops the NFS packets
  of a kernel NFS server and a hard NFSv3 client mount over loopback, in a container. On that
  kernel a process waiting on the dead mount can be killed. A mount where it cannot would leave
  the probe child in uninterruptible sleep until the I/O ends, and a filer that accepts
  connections and never answers is not covered.
- **Unreported run outcomes.** File, mysqldump and xtrabackup runs record no outcome yet, so
  they have no `bdbackup_job_*` series.
