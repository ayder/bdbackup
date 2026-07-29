"""Named, reusable exclusion templates for file backups.

A template is a named bundle of gitignore-style exclusion patterns (see
:mod:`bdbackup.templates._matching` for the supported syntax). Templates are
discovered from two locations and merged by name:

1. Built-in presets shipped as modules inside this package (e.g.
   ``python_dev.py``), so they work with ``uv tool install``.
2. User presets: any ``*.py`` file in ``<config dir>/templates/`` where the
   config dir resolves via :func:`bdbackup.utils.config_dir`
   (``$BDBACKUP_CONFIG_DIR`` -> ``$XDG_CONFIG_HOME/bdbackup`` ->
   ``~/.config/bdbackup``).

A preset module self-registers by defining either::

    TEMPLATE = ExclusionTemplate(name="my-dev", patterns=(...), description="...")

or::

    def register() -> ExclusionTemplate: ...

Adding a new preset (built-in or user) never requires editing existing code:
drop in a new module and it is picked up on the next run.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from collections.abc import Iterable
from dataclasses import dataclass

from bdbackup.templates._matching import GitignoreMatcher, build_matcher
from bdbackup.utils import config_dir

__all__ = [
    "ExclusionTemplate",
    "GitignoreMatcher",
    "TemplateError",
    "build_matcher",
    "get_template",
    "list_templates",
    "register_template",
    "resolve_patterns",
]

logger = logging.getLogger("bdbackup.templates")


@dataclass(frozen=True)
class ExclusionTemplate:
    """A named bundle of gitignore-style exclusion patterns."""

    name: str
    patterns: tuple[str, ...]
    description: str = ""


class TemplateError(Exception):
    """Raised when an exclusion template cannot be found or loaded."""


_REGISTRY: dict[str, ExclusionTemplate] = {}
_LOADED = False


def register_template(template: ExclusionTemplate) -> None:
    """Register *template*, replacing any existing template with the same name."""
    _REGISTRY[template.name] = template


def _collect(module) -> None:
    """Register whatever a preset module exposes (TEMPLATE or register())."""
    template = getattr(module, "TEMPLATE", None)
    if isinstance(template, ExclusionTemplate):
        register_template(template)
    hook = getattr(module, "register", None)
    if callable(hook):
        produced = hook()
        if isinstance(produced, ExclusionTemplate):
            register_template(produced)
        elif isinstance(produced, Iterable):
            for item in produced:
                if isinstance(item, ExclusionTemplate):
                    register_template(item)


def _load_builtin() -> None:
    """Import every preset module shipped inside this package."""
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
            _collect(module)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to load built-in template %r: %s", info.name, exc)


def _load_user() -> None:
    """Import user preset modules from <config dir>/templates/*.py."""
    user_dir = config_dir() / "templates"
    if not user_dir.is_dir():
        return
    for file in sorted(user_dir.glob("*.py")):
        if file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"bdbackup_user_template_{file.stem}", file
            )
            if spec is None or spec.loader is None:  # pragma: no cover - defensive
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _collect(module)
        except Exception as exc:
            logger.warning("Failed to load user template %s: %s", file, exc)


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    _load_builtin()
    _load_user()
    _LOADED = True


def list_templates() -> dict[str, ExclusionTemplate]:
    """Return all registered templates, keyed by name."""
    _ensure_loaded()
    return dict(_REGISTRY)


def get_template(name: str) -> ExclusionTemplate:
    """Return the template called *name* or raise :class:`TemplateError`."""
    _ensure_loaded()
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise TemplateError(
            f"Unknown exclusion template {name!r}. Available: {available}. "
            f"User templates are loaded from {config_dir() / 'templates'}/*.py"
        ) from None


def resolve_patterns(names: Iterable[str]) -> list[str]:
    """Return the combined patterns of the named templates, in order."""
    patterns: list[str] = []
    for name in names:
        patterns.extend(get_template(name).patterns)
    return patterns


def _reset_for_tests() -> None:
    """Clear the registry and loaded flag (test isolation only)."""
    global _LOADED
    _REGISTRY.clear()
    _LOADED = False
