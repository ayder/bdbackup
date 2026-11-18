"""The family registry: every metric family is declared once, with its HELP text and labels."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    """One metric family: its name, HELP text and label names, all gauges."""

    name: str
    help: str
    labels: tuple[str, ...] = ()


class Registry:
    """The declared families; rendering accepts only these."""

    def declare(self, name: str, help: str, labels: Iterable[str] = ()) -> Family:
        """Not yet."""
        return Family(name, help, tuple(labels))

    def get(self, name: str) -> Family | None:
        """Not yet."""
        return None


def engine_prefix(engine: str) -> str:
    """Not yet."""
    return ""
