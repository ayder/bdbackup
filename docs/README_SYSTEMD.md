# Running xtrabackup jobs with systemd timers

bdbackup can run xtrabackup jobs as systemd units instead of cron lines. If
bdbackup dies, even from `kill -9`, systemd stops the backup tool with it, so no
orphan keeps loading the server. Each job can have its own CPU, I/O and memory
limits, and status and logs come from `systemctl` and `journalctl`.

Cron keeps working, and bdbackup does not install units. Other job types stay on
cron.

Part of the [bdbackup documentation](../README.md).

## Requirements

- Linux with systemd and cgroup v2. Installing the units needs root; backups run
  as the `mysql` account, which owns the datadir.
- Job names that need no systemd escaping. `mysql-prod` works; a name with a space
  does not.

## Install

Fetch the units for your installed version:

```bash
version=$(bdbackup --version | awk '{print $NF}')
for unit in bdbackup@.service bdbackup-incremental@.service \
            bdbackup@.timer bdbackup-incremental@.timer; do
  sudo curl -fsSLo "/etc/systemd/system/$unit" \
    "https://raw.githubusercontent.com/ayder/bdbackup/v$version/contrib/systemd/$unit"
done
```

systemd looks for `bdbackup` in its standard paths. If you installed it in a
virtual environment, or with pipx or uv, link it:

```bash
sudo ln -s "$(command -v bdbackup)" /usr/local/bin/bdbackup
```

The units read `/etc/bdbackup/config.toml`. It holds passwords, and `mysql` must
be able to read it:

```bash
sudo chown root:mysql /etc/bdbackup/config.toml
sudo chmod 640 /etc/bdbackup/config.toml
```

Give `mysql` the `backup_root`, the directory of the history database and any
encryption key file, then check its access:

```bash
sudo -u mysql bdbackup run -c /etc/bdbackup/config.toml --validate -j mysql-prod
```

For another config path or account, set `Environment=BDBACKUP_CONFIG=...`, `User=`
and `Group=` in a drop-in.

## Schedule

The timers have no default schedule, so set one before enabling them. For a weekly
full and daily incrementals of `mysql-prod`:

```ini
# /etc/systemd/system/bdbackup@mysql-prod.timer.d/schedule.conf
[Timer]
OnCalendar=Sun 02:00

# /etc/systemd/system/bdbackup-incremental@mysql-prod.timer.d/schedule.conf
[Timer]
OnCalendar=Mon..Sat 02:00
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bdbackup@mysql-prod.timer bdbackup-incremental@mysql-prod.timer
systemctl list-timers 'bdbackup*'
journalctl -u 'bdbackup*@mysql-prod.service'
```

Times use the host's timezone. A backup missed while the host was down does not
run at boot; add `Persistent=true` to the drop-in to change that.

Remove the job's cron lines when you enable its timers.
Never run one job from both.

## Limits

Limits go in a drop-in for each mode, so a full and an incremental can differ:

```ini
# /etc/systemd/system/bdbackup@mysql-prod.service.d/limits.conf
[Service]
MemoryHigh=1500M
MemorySwapMax=0
CPUQuota=200%
IOReadBandwidthMax=/dev/nvme0n1 200M
```

Run `systemctl daemon-reload` after a change. A limit covers the whole job,
including the checksum bdbackup computes after the tool exits.

- **Start with `MemoryHigh=`.** xtrabackup's reads skip the page cache, but its
  output and the checksum read fill it, and that memory is taken from mysqld. As a
  guide, 1500M for a full and 1024M for an incremental ran clean on a 7.75 GiB host.
- **Prefer `MemoryHigh=` to `MemoryMax=`.** `MemoryMax=` kills the job and leaves a
  `running` row in history.
- **`IOReadBandwidthMax=` and `IOWriteBandwidthMax=`** take a device path.
  `IOWeight=` needs I/O scheduler or `io.cost` support, which NVMe setups often lack.
- **Do not throttle too hard.** A slower backup has more redo to follow. If redo wraps
  before the tool reads it, the backup fails.
- **Limits do not help with waits on the backup lock,** which happen inside mysqld.

## Overlapping runs

A timer does not start a second copy of a unit that is still running. Full and
incremental are separate units, so bdbackup's lock decides between them: the second
exits 3 and shows as a failed unit, which keeps a stuck lock visible. If they overlap
on purpose, add `SuccessExitStatus=3` to the `[Service]` drop-in.

## What stays behind after a hard kill

After `kill -9` the tool stops, but bdbackup cannot clean up. The `.tmp` directory
stays in `backup_root`, the history row stays `running`, and the temporary option file
holding the password stays in the temp directory, readable only by `mysql`. The backup
chain is not affected, because a backup is committed only when its `.tmp` directory is
renamed. Remove these by hand.
