"""PostgreSQL backup engine family (pgBackRest).

Importing this package imports every engine module inside it, which runs its engine
registration in :mod:`bdbackup.engines`, as :mod:`bdbackup.mysql` does for its family.
Reading a pgBackRest repository lives in :mod:`bdbackup.postgres.repository`.
"""

from __future__ import annotations

import importlib
import pkgutil

__all__ = ["PgBackRest"]

# Import every module so an engine's ENGINE registration runs (bdbackup.engines discovery
# imports this package too).
for _info in pkgutil.iter_modules(__path__):
    if _info.name.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{_info.name}")

from bdbackup.postgres.pgbackrest import PgBackRest  # noqa: E402
