# MySQL and MariaDB physical backups with Percona XtraBackup and MariaDB Backup

An xtrabackup job takes full and incremental physical (hot) backups of MySQL,
Percona Server or MariaDB with Percona XtraBackup or MariaDB Backup
(`mariabackup`, `mariadb-backup`), compressed and optionally encrypted, and
prepares a recovery copy with `restore --backup`. This page covers its keys,
full and incremental backups, the dry run, preparation, engine pruning,
encryption and what a restore produces.

Part of the [bdbackup documentation](../README.md).

## Configuration keys

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

## Full and incremental backups

Take a full with `run -j JOB` and an incremental, which chains to the latest
successful full, with `run -j JOB --incremental`, using the job's own
`backup_root`, credentials, binary and encryption settings:

```bash
bdbackup run -c ~/.config/bdbackup/config.toml -j mysql-prod --incremental
```

`--full` is the default; an incremental requires a successful full in that job's
root and chains to the latest successful incremental, if present. `--full` and
`--incremental` together exit 2, and so does `--incremental` on any other job type.
`--verify/--no-verify` applies to both backup kinds.

## Dry run

A dry run of an xtrabackup job names the backup kind and its root, and an
incremental also names the backup it would chain from.

## Preparing a recovery copy

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

## Compatibility and chain layout

Use XtraBackup matching your MySQL/Percona server series (8.0 with 8.0, 8.4 with
8.4); use `mariabackup` or `mariadb-backup` matching your MariaDB installation.
MariaDB preparation omits XtraBackup's `--apply-log-only` option. Compression is
**off by default**. For a compatible recent XtraBackup, set `compress = "zstd"`;
MariaDB's deprecated built-in compression accepts only `quicklz` and requires
`qpress` for decompression. Compatibility must be established with an actual
recovery test for the exact server and backup binary versions in use.

New physical backups record parent/full identities and LSNs in `bdbackup.json`.
Incrementals live under `DATE/Incremental/FULL_ID/UNIQUE_ID`, and cannot attach to
another full taken on the same day. A full without `bdbackup.json` (taken by
bdbackup before 0.4.0) cannot be the base of an incremental; take a new full.
Such a full can still be prepared as a recovery copy. Engine pruning removes
complete dated chains, preserves the newest successful full's date, and refuses
to prune without a valid full.

A process lock prevents two backup runs from corrupting the same backup root.
Failed backups are written to a temporary directory first and cleaned up on
error, so a partial backup can never be mistaken for a complete one.

## Engine pruning

Pruning after each full deletes whole dated chains older than `retention_days`.
Set `retention_days` longer than the interval between fulls to preserve the previous
full's incrementals. `retention_days = 0` disables engine deletion for the job;
use it when GFS or another tool rotates the backups. History records physical
backups as `xtrabackup-full` / `xtrabackup-incremental`.

## Encrypted physical backups

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
path for recovery, never the key itself. A history database at an older
schema is upgraded on the next write; rows written before the `encrypted`
column existed read as unencrypted.

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

## History restore

XtraBackup/MariaDB backups produce a prepared recovery directory, including
prerequisite increments. The destination must be outside the backup root.
Server ownership, copy-back, and startup remain administrator actions.
