# Custom backup engines (plugins)

Database engines are pluggable: each maps a config `type` to a backend class.
This page covers the built-in engines, writing your own engine as a plugin in
`~/.config/bdbackup/engines/`, and what validation and restore do for it.

Part of the [bdbackup documentation](../README.md).

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

## Validation

`--validate` of a job whose type is a custom engine reports
`[FAIL] Preflight checks are not implemented for engine '<type>'.` and exits 1.

## History restore

Backups of a custom engine are recorded in the history, but history restore
refuses them (exit 1, `History restore is not supported for engine '<type>'`)
unless the engine's backend subclasses `FileBackup`, `MySQLBackup` or
`XtraBackup`.
