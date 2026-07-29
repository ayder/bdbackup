"""MySQL/MariaDB backup engine family.

Importing this package imports every engine module inside it
(``mysqldump.py``, ``xtrabackup.py``, and any future variant dropped in
such as ``xtrabackup_80.py`` or ``mysql_shell.py``). Importing an engine
module runs its ``register_engine()`` call, populating the central registry
in :mod:`bdbackup.engines` — no wiring changes are ever needed to add an
engine here. MySQL-specific helpers live in :mod:`bdbackup.mysql.helpers`.
"""

from __future__ import annotations

import importlib
import pkgutil

__all__ = ["MySQLBackup", "XtraBackup"]

# Import every engine module so its ENGINE registration runs (drives
# bdbackup.engines discovery too, which imports this package).
for _info in pkgutil.iter_modules(__path__):
    if _info.name.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{_info.name}")

from bdbackup.mysql.mysqldump import MySQLBackup  # noqa: E402
from bdbackup.mysql.xtrabackup import XtraBackup  # noqa: E402
