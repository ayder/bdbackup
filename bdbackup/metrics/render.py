"""Render samples as the Prometheus text exposition format."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from bdbackup.metrics.registry import Registry


@dataclass(frozen=True)
class Sample:
    """One series of a declared family: its label values and its value."""

    family: str
    value: int | float
    labels: dict[str, str] = field(default_factory=dict)


def render(registry: Registry, samples: Iterable[Sample]) -> str:
    """Not yet."""
    return ""
