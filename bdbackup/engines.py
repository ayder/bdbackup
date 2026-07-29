"""Registry of backup engines, pluggable per database family.

An engine maps a config ``type`` name (e.g. ``mysqldump``, ``xtrabackup``,
``xtrabackup80``, ``mysql-shell``) to a backend class implementing the
:class:`~bdbackup.backends.BackupBackend` protocol. Engines are discovered
from two locations and merged by name:

1. Built-in engines shipped inside family packages (see FAMILIES below),
   so they work with ``uv tool install``. Each family package self-discovers
   its engine modules.
2. User engines: any ``*.py`` file in ``<config dir>/engines/`` where the
   config dir resolves via :func:`bdbackup.utils.config_dir`
   (``$BDBACKUP_CONFIG_DIR`` -> ``$XDG_CONFIG_HOME/bdbackup`` ->
   ``~/.config/bdbackup``).

An engine module self-registers by defining either::

    ENGINE = EngineInfo(name="my-engine", backend=MyEngine, description="...")

or::

    def register() -> EngineInfo: ...

Adding an engine (built-in or user) never requires editing existing code:
drop in a new module and it is picked up on the next run. A whole new
database family (e.g. postgres) requires adding its package import path to
FAMILIES below — the single deliberate modification point.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from collections.abc import Iterable
from dataclasses import dataclass

from bdbackup.utils import config_dir

__all__ = [
    "EngineError",
    "EngineInfo",
    "get_engine",
    "list_engines",
    "register_engine",
]

logger = logging.getLogger("bdbackup.engines")

# Import paths of family packages that self-discover their engine modules.
# To add a database family (e.g. postgres): create bdbackup/postgres/ and
# append "bdbackup.postgres" here — nothing else needs to change.
FAMILIES: tuple[str, ...] = (
    "bdbackup.mysql",
)


@dataclass(frozen=True)
class EngineInfo:
    """A named backup engine bound to a backend class."""

    name: str
    backend: type
    description: str = ""
    family: str = "mysql"


class EngineError(Exception):
    """Raised when an engine cannot be found or loaded."""


_REGISTRY: dict[str, EngineInfo] = {}
_LOADED = False


def register_engine(engine: EngineInfo) -> None:
    """Register *engine*, replacing any existing engine with the same name."""
    _REGISTRY[engine.name] = engine


def _collect(module) -> None:
    """Register whatever an engine module exposes (ENGINE or register())."""
    engine = getattr(module, "ENGINE", None)
    if isinstance(engine, EngineInfo):
        register_engine(engine)
    hook = getattr(module, "register", None)
    if callable(hook):
        produced = hook()
        if isinstance(produced, EngineInfo):
            register_engine(produced)
        elif isinstance(produced, Iterable):
            for item in produced:
                if isinstance(item, EngineInfo):
                    register_engine(item)


def _load_builtin() -> None:
    """Import every engine module shipped inside each family package."""
    for family in FAMILIES:
        pkg = importlib.import_module(family)
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"{family}.{info.name}")
                _collect(module)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to load built-in engine %r.%r: %s", family, info.name, exc
                )


def _load_user() -> None:
    """Import user engine modules from <config dir>/engines/*.py."""
    user_dir = config_dir() / "engines"
    if not user_dir.is_dir():
        return
    for file in sorted(user_dir.glob("*.py")):
        if file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"bdbackup_user_engine_{file.stem}", file
            )
            if spec is None or spec.loader is None:  # pragma: no cover - defensive
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _collect(module)
        except Exception as exc:
            logger.warning("Failed to load user engine %s: %s", file, exc)


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    _load_builtin()
    _load_user()
    _LOADED = True


def list_engines() -> dict[str, EngineInfo]:
    """Return all registered engines, keyed by name."""
    _ensure_loaded()
    return dict(_REGISTRY)


def get_engine(name: str) -> EngineInfo:
    """Return the engine called *name* or raise :class:`EngineError`."""
    _ensure_loaded()
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise EngineError(
            f"Unknown engine {name!r}. Available: {available}. "
            f"User engines are loaded from {config_dir() / 'engines'}/*.py"
        ) from None


def _reset_for_tests() -> None:
    """Clear the registry and loaded flag (test isolation only)."""
    global _LOADED
    _REGISTRY.clear()
    _LOADED = False
