"""Render samples as the Prometheus text exposition format."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from bdbackup.metrics.registry import Family, Registry


@dataclass(frozen=True)
class Sample:
    """One series of a declared family: its label values and its value."""

    family: str
    value: int | float
    labels: dict[str, str] = field(default_factory=dict)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _number(family: str, value: object) -> str:
    """The text of a value: an integer without a decimal point, never NaN or infinity."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{family}: value is not a number: {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{family}: value is not finite: {value!r}")
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def _series(family: Family, sample: Sample) -> tuple[tuple[str, ...], str]:
    """The label values of a sample in the declared order, and the text of its value."""
    if set(sample.labels) != set(family.labels):
        raise ValueError(
            f"{family.name}: labels {sorted(sample.labels)} do not match "
            f"the declared {list(family.labels)}"
        )
    values = tuple(str(sample.labels[name]) for name in family.labels)
    return values, _number(family.name, sample.value)


def render(registry: Registry, samples: Iterable[Sample]) -> str:
    """The samples as text: a HELP and a TYPE line per family, then its series.

    Families come in name order and series in label-value order, so equal input gives equal
    text. A family with no sample is left out. Raises ValueError for a family that was not
    declared, a sample whose label names differ from the declaration, a repeated series (a
    textfile that repeats one is rejected whole) or a value that is not a finite number.
    """
    grouped: dict[str, dict[tuple[str, ...], str]] = {}
    for sample in samples:
        family = registry.get(sample.family)
        if family is None:
            raise ValueError(f"undeclared metric family: {sample.family}")
        values, text = _series(family, sample)
        series = grouped.setdefault(family.name, {})
        if values in series:
            raise ValueError(f"duplicate series in {family.name}: {list(values)}")
        series[values] = text
    lines = []
    for name in sorted(grouped):
        family = registry.get(name)
        lines.append(f"# HELP {name} {_escape_help(family.help)}")
        lines.append(f"# TYPE {name} gauge")
        for values, text in sorted(grouped[name].items()):
            labels = ",".join(
                f'{label}="{_escape_label(value)}"'
                for label, value in zip(family.labels, values, strict=True)
            )
            lines.append(f"{name}{{{labels}}} {text}" if labels else f"{name} {text}")
    return "".join(f"{line}\n" for line in lines)
