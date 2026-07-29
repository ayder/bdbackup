"""MySQL/MariaDB-specific helpers shared by the mysql family's engines."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def mysql_cnf_file(
    *,
    user: str,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
):
    """Create a temporary mysql client options file with safe permissions.

    Yields the Path to the file. The file is deleted when the context exits.
    Using a defaults-extra-file keeps credentials out of argv and process lists.
    """
    fd, raw_path = tempfile.mkstemp(prefix="bdbackup-cnf-", suffix=".cnf")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("[client]\n")
            f.write(f"user={user}\n")
            if password:
                f.write(f"password={password}\n")
            if host:
                f.write(f"host={host}\n")
            if port is not None:
                f.write(f"port={port}\n")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
