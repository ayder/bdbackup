"""The family registry: every metric family is declared once, with its HELP text and labels."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
# Prometheus sets these on the scrape target; a bdbackup job is ``job_name``.
_RESERVED = {"job", "instance"}


@dataclass(frozen=True)
class Family:
    """One metric family: its name, HELP text and label names, all gauges."""

    name: str
    help: str
    labels: tuple[str, ...] = ()


class Registry:
    """The declared families; rendering accepts only these."""

    def __init__(self) -> None:
        self._families: dict[str, Family] = {}

    def declare(self, name: str, help: str, labels: Iterable[str] = ()) -> Family:
        """Declare a family; raises ValueError for a bad name, a bad label or a second time."""
        labels = tuple(labels)
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"not a valid metric name: {name!r}")
        if name in self._families:
            raise ValueError(f"metric family already declared: {name}")
        for label in labels:
            if not _LABEL_NAME.fullmatch(label) or label.startswith("__"):
                raise ValueError(f"{name}: not a valid label name: {label!r}")
            if label in _RESERVED:
                raise ValueError(f"{name}: the label {label!r} is set by Prometheus, not by us")
        if len(set(labels)) != len(labels):
            raise ValueError(f"{name}: a label is named twice: {list(labels)}")
        family = Family(name, help, labels)
        self._families[name] = family
        return family

    def get(self, name: str) -> Family | None:
        """The family declared as ``name``, or ``None``."""
        return self._families.get(name)


def engine_prefix(engine: str) -> str:
    """The prefix of an engine's families: ``mysql-shell`` gives ``bdbackup_mysql_shell_``."""
    return "bdbackup_" + re.sub(r"[^a-zA-Z0-9_]", "_", engine) + "_"
