"""GFS filesystem actions: contents check, verified copy and removal (spec 2 §4.3, §4.4)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from bdbackup.gfs.store import ManagedUnit


class Refusal(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Transport(Protocol):
    def copy(self, source: Path, temp: Path, on_file: Callable[[Path, str], None]) -> None: ...
    def rename(self, temp: Path, final: Path) -> None: ...
    def remove(self, path: Path) -> None: ...
    def exists(self, path: Path) -> bool: ...


class LocalTransport:
    def copy(self, source: Path, temp: Path, on_file: Callable[[Path, str], None]) -> None:
        return None

    def rename(self, temp: Path, final: Path) -> None:
        return None

    def remove(self, path: Path) -> None:
        return None

    def exists(self, path: Path) -> bool:
        return False


def check_contents(unit: ManagedUnit, at: Path) -> None:
    return None


def verify(unit: ManagedUnit, at: Path) -> None:
    return None


def copy_verified(unit: ManagedUnit, source: Path, final: Path, transport: Transport) -> Path:
    return final
