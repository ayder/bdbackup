"""Write the textfile atomically, for node_exporter's textfile collector to read."""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, or raise OSError and change nothing.

    The text goes to a hidden temporary file in the same directory, whose name does not end in
    ``.prom``, so the collector never reads it. It is synced, given mode 0644 whatever the
    umask, and renamed over ``path``; a failure removes it and leaves the old file.
    """
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            view = memoryview(text.encode())
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
