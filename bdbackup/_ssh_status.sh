#!/bin/sh
# The transfer tool owns the SSH streams. Record only the SSH process status.
status_file=${BDBACKUP_SSH_STATUS:?missing SSH status path}
if ! command -v ssh >/dev/null 2>&1; then
    printf '%s\n' 'missing' > "$status_file"
    printf '%s\n' 'ssh: executable not found' >&2
    exit 127
fi
ssh "$@"
status=$?
printf '%s\n' "$status" > "$status_file"
exit "$status"
