# MySQL and MariaDB logical backups with mysqldump

A mysqldump job writes a gzip-compressed logical SQL dump of one MySQL or
MariaDB database, or of all of them. This page covers its keys, credentials,
default options, the dry run and what a restore produces.

Part of the [bdbackup documentation](../README.md).

## Configuration keys

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

## Dry run

A dry run of a mysqldump job names the database (or all databases) and the dump
directory.

## History restore

MySQL dumps are decompressed and checked into `<destination>/backup.sql`.
Importing SQL into a running server is a separate administrator action.
