# Copy a published backup to an SSH server

File, mysqldump and pgbackrest jobs can send each published archive to a
dedicated directory on an SSH server. The copy starts after bdbackup records
the successful local backup and its SHA-256 in SQLite.

## Configure the job

This file job reads `files.template` beside the config. List paths relative
to the source directory, one per line.

```toml
[history]
database = "state/history.sqlite3"

[files-daily]
type = "file"
template_filename = "files.template"
chdir = "source"
backup_dst = "archives/daily"
format = "tar.gz"
timestamp = true

[files-daily.copy]
destination = "ssh://backup@vault.example.com:2222/srv/bdbackup/files-daily"
method = "rsync"
# ssh_config = "backup-ssh.conf"
# bwlimit = 20000
```

| Key | Required | Meaning |
|---|---|---|
| `destination` | Yes | Existing remote directory as `ssh://[user@]host[:port]/absolute/path`. SSH configuration supplies any omitted user or port. |
| `method` | Yes | `rsync` or `scp`. |
| `ssh_config` | No | OpenSSH client configuration passed as `-F`. A relative path resolves beside the TOML file. If omitted, OpenSSH uses the OS account's normal SSH configuration. |
| `bwlimit` | No | Positive whole number in KiB/s. bdbackup converts the value for the selected tool. |

Use OpenSSH on the client and rsync 3 or newer for the rsync method. SCP
uses the protocol selected by the installed OpenSSH client, including legacy
SCP or SFTP as its default.

A copy requires `[history]`. File jobs also require `timestamp = true`.
Give each job a distinct destination. bdbackup compares the written
host, port and path, so an SSH user change alone still shares the directory.

Use ASCII letters, digits and `. _ - + = @ , % ~` in remote paths and
artifact names. Paths also allow `/`. Use an absolute path with no `.`
or `..` segment. A file job's `backup_dst` basename and a pgbackrest job's
`stanza` must follow the same name rules. A percent sign in a path is
literal. Leave passwords, queries and fragments out of destination URLs.

## Prepare the server and keys

- Use an SSH account with a POSIX shell and key login that works without a
  prompt. Install the backup account's public key on the server using your
  normal authorized-key setup. Keep the private key on the client.
- Configure the client account's SSH identity and trusted host keys in its
  SSH configuration. Obtain the server's public host key or verify its
  fingerprint through a trusted channel before adding it to `known_hosts`.
  For the example port, use the host entry `[vault.example.com]:2222`.
  bdbackup passes `BatchMode=yes` and `StrictHostKeyChecking=yes`.
  Strict host key checking overrides `accept-new`, so add the key before
  the first run.
- Provide GNU coreutils or BusyBox for `ln -T`, `stat -c`, and `sha256sum`.
  The shell also needs `test`, `rm`, and `chmod`. Install rsync on the
  server when using that method. BSD and macOS SSH servers are unsupported.
- Create the destination directory on a filesystem that supports hard
  links. Give the SSH account write and search access to it. bdbackup
  uses the directory you provide.

Check the setup under the OS account that will run the backup:

```sh
bdbackup run -c config.toml -j files-daily --dry-run
bdbackup run -c config.toml -j files-daily --validate
bdbackup run -c config.toml -j files-daily
bdbackup history -c config.toml --job files-daily
```

The dry run prints the destination and method without contacting SSH.
Validation checks local executables and the configured SSH file, then runs
read-only checks for connection, directory access and remote tools. A backup
run reports its copy outcome, and history adds `copy pending`, `copy copied`,
`copy failed (<class>)` or `copy abandoned (<class>)` to the backup line.

## Publication and retries

bdbackup sends the archive to a unique `.bdbackup-tmp-…` name in the
destination. The server computes its SHA-256, which must match the recorded
checksum even when the run uses `--no-verify`. bdbackup checks the staged
file's permission bits and caps them to the source's mode when needed.
It publishes the verified file by creating a hard link at the final name
and removing the temporary name.

Publication is create-only. A final name with the recorded SHA-256 counts
as copied without a transfer or any change to that file. A different
file under the final name is left intact and the delivery fails.
bdbackup removes only its own temporary names.

The next successful run of the same job tries pending and failed deliveries
for its configured destination, newest first and once per run. Unfinished
transfers restart from the beginning. A pgbackrest run also retries the
queue when it reports `no new full` and the newest full meets its age limit.
Deliveries for an earlier destination and backups made before copy was
configured stay as recorded.

The local backup remains successful if a copy fails. The run exits 1
when an attempt fails or is abandoned. If another run holds the job's
copy lock, it reports the deferral and exits 3. A later copy phase can
take the pending delivery. GFS can continue rotating the local archive
while a copy is in progress.

### Abandoned deliveries

A run marks a delivery `abandoned (source_unavailable)` if it finds no local
source with a matching recorded identity. For a GFS-managed backup, that
includes a temporarily unreachable stage when no other recorded location
matches. An abandoned delivery contributes exit 1 and is skipped on later
runs. Recover the artifact for a manual copy or run a new backup.

A killed run may leave a temporary name on the server. A copy that publishes
but cannot remove its temporary name stays `copied` and logs a warning with
that path. Remove the stale temporary name by hand after confirming that
no run is using it.

SSH protects the archive in transit. bdbackup sends the archive as-is and
relies on the server for encryption at rest. Remote retention, deletion of
published copies, and recovery from the SSH server are operator-managed.
GFS stage paths refer to local or mounted filesystems.

## Failure classes

A failed tool call logs its last diagnostic line at ERROR on stderr when
available. History stores the failure class.

| Class | Remedy |
|---|---|
| `connect` | Check network access, key-based login and the trusted host key as the OS account running the job. |
| `destination_not_ready` | Create the configured directory or restore write and search access for the SSH account. |
| `tool_missing` | Check the local and remote tool requirements above. Put the required executables in the corresponding accounts' PATH. |
| `name_taken` | Investigate the existing final file before moving it aside. Retry once the destination can accept the recorded artifact. |
| `transfer` | Read the tool diagnostic and check network stability, permissions and free space. Retry the job. |
| `checksum_mismatch` | Check for a changed source or storage errors on the server. Retry after addressing the cause. |
| `source_unavailable` | See [Abandoned deliveries](#abandoned-deliveries). |
| `other` | Read the last diagnostic and check server command compatibility, mode changes and hard-link support. |
