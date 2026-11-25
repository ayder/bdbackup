# Custom backup engines (plugins)

Database engines are pluggable: each maps a config `type` to a backend class.
This page covers the built-in engines, writing your own engine as a plugin in
`~/.config/bdbackup/engines/`, and what validation and restore do for it.

Part of the [bdbackup documentation](../README.md).

`active` and `restore_root` are job metadata and are not passed to an engine backend.

Database engines are pluggable and live in per-database family packages
(`bdbackup/mysql/`, `bdbackup/postgres/` and `bdbackup/mongodb/`). Each engine maps a
config `type` name to a backend class; adding one never requires editing
existing wiring code:

| Engine | Config `type` | Backend | Notes |
|--------|---------------|---------|-------|
| `mysqldump` | `mysqldump` | `bdbackup.mysql.MySQLBackup` | Logical, gzip-compressed SQL dumps |
| `xtrabackup` | `xtrabackup` | `bdbackup.mysql.XtraBackup` | Percona XtraBackup / MariaDB mariabackup |
| `pgbackrest` | `pgbackrest` | `bdbackup.postgres.PgBackRest` | pgBackRest full backups exported as one tar to a GFS first stage; see [README_PGBACKREST.md](README_PGBACKREST.md) |
| `pbm` | `pbm` | `bdbackup.mongodb.pbm.PBM` | Full logical and physical MongoDB backups exported as individual tars. See [README_PBM.md](README_PBM.md). |

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
`bdbackup/mysql/helpers.py`. A new database family means creating its package,
shaped like `bdbackup/postgres/`, and appending its import path to
`bdbackup.engines.FAMILIES` — the single deliberate modification point.
A dry run of a custom engine exits 2: only the built-in types describe their run.

## Metrics collectors

An engine can add its own metrics to `bdbackup metrics` through `EngineInfo.metrics`, an object
with these methods; see [Prometheus metrics](README_METRICS.md). Without one, the engine's jobs
have no series of their own, and their backups still count in `bdbackup_history_units`. The
request, answer and error types are in `bdbackup.metrics.collector`.

- `families()` returns the `Family(name, help, labels)` objects the collector writes. Every
  name must start with `bdbackup_<engine>_`, the engine name with each character outside
  `[a-zA-Z0-9_]` replaced by `_` (`mysql-shell` gives `bdbackup_mysql_shell_`). The labels may
  not include `job` or `instance`.
- `probes(job, ctx)` returns the filesystem checks it needs as requests:
  `StatRequest(path, probe="destination")` and
  `ListPrefixRequest(directory, prefix, probe="repository")`. It never touches the filesystem
  itself: the probe child processes run the requests, so a share that hangs cannot block the
  run. It may run a command (the pgbackrest collector makes its `info` call here), limited by
  `ctx.timeout_seconds`.
- `collect(job, ctx, results)` yields `Sample(family, value, labels)` from `ctx` (the clock,
  the jobs, the history records, `last_runs` and the GFS runs, read once per run) and from
  `results.stat(path)` and `results.listing(directory, prefix)`, the probes' answers. It may
  also yield `MetricError(engine name, job name, reason)` for a failure of its own source.
- `describes(facts, record)` is optional: it says whether the facts of the job's last run
  describe its newest successful record. Without it, they do when both name the same `label`.

```python
from bdbackup.engines import EngineInfo
from bdbackup.metrics.registry import Family
from bdbackup.metrics.render import Sample

class DumpMetrics:
    def families(self):
        return [Family("bdbackup_mysql_shell_dumps", "Dumps in the history.", ("job_name",))]

    def probes(self, job, ctx):
        return []

    def collect(self, job, ctx, results):
        count = sum(1 for record in ctx.data.records if record.job_name == job.name)
        yield Sample("bdbackup_mysql_shell_dumps", count, {"job_name": job.name})

ENGINE = EngineInfo(name="mysql-shell", backend=MySQLShellDump, metrics=DumpMetrics())
```

A collector cannot stop the run. When it raises, or yields a sample that does not fit its
declared family or whose name is outside its prefix, its samples are dropped and the file gets
`bdbackup_metrics_error{source="collector",job_name=<job>,reason="failed"}`. When `probes()`
does not return within `timeout_seconds`, it is abandoned and the file gets
`bdbackup_metrics_error{source=<engine name>,job_name=<job>,reason="timeout"}`. The other
collectors still run. A `MetricError` a collector yields must carry its own engine name as
`source` and its own job; anything else counts as a failure of the collector.

## Validation

`--validate` of a job whose type is a custom engine reports
`[FAIL] Preflight checks are not implemented for engine '<type>'.` and exits 1.

## History restore

Backups of a custom engine are recorded in the history, but history restore
refuses them (exit 1, `History restore is not supported for engine '<type>'`)
unless the engine's backend subclasses `FileBackup`, `MySQLBackup` or
`XtraBackup`.
