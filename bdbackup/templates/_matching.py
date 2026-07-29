"""Gitignore-style pattern matching for exclusion templates.

This is a deliberately small subset of gitignore semantics ("gitignore-lite"),
sufficient for backup exclusions without pulling in an external dependency:

- ``*.pyc``                basename glob, matches at any depth
- ``__pycache__/``         trailing slash: directories only, matches at any depth
- ``tests/containers/*``   contains a slash: matched against the path relative
                           to the candidate root(s)
- ``/build/``              leading slash: additionally anchored to the root
- ``!important.log``       negation: re-includes a previously excluded path

Unlike gitignore, a directory match does not automatically prune its contents
from the walk; FileBackup's traversal tests every path individually, so
templates should list contents explicitly (e.g. ``node_modules/**``) when the
directory itself is only an intermediate component of backup roots.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from pathlib import Path


def _translate(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob to a regex.

    Unlike :func:`fnmatch.translate`, ``*`` and ``?`` never match ``/``;
    ``**`` matches any number of characters including ``/``.
    """
    regex = ""
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                regex += ".*"
                i += 2
            else:
                regex += "[^/]*"
                i += 1
        elif char == "?":
            regex += "[^/]"
            i += 1
        elif char == "[":
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                regex += re.escape("[")
                i += 1
            else:
                stuff = pattern[i + 1 : j].replace("\\", "\\\\")
                if stuff.startswith("!"):
                    stuff = "^" + stuff[1:]
                regex += f"[{stuff}]"
                i = j + 1
        else:
            regex += re.escape(char)
            i += 1
    return re.compile(f"(?s:{regex})\\Z")


class _Rule:
    """A single compiled gitignore-style pattern."""

    __slots__ = ("pattern", "negated", "dir_only", "anchored", "regex")

    def __init__(self, raw: str):
        pattern = raw.strip()
        self.negated = pattern.startswith("!")
        if self.negated:
            pattern = pattern[1:]
        self.dir_only = pattern.endswith("/")
        if self.dir_only:
            pattern = pattern.rstrip("/")
        self.anchored = pattern.startswith("/")
        if self.anchored:
            pattern = pattern.lstrip("/")
        if not pattern:
            raise ValueError(f"Empty exclusion pattern: {raw!r}")
        self.pattern = pattern
        self.regex = _translate(pattern)

    def matches(self, path: Path, is_dir: bool, roots: Iterable[Path]) -> bool:
        if self.dir_only and not is_dir:
            return False
        if "/" in self.pattern:
            if self.anchored:
                # Anchored: match the full path relative to a root only.
                for root in roots:
                    try:
                        candidate = path.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if self.regex.match(candidate):
                        return True
                return False
            # Unanchored: the pattern may match at any depth, so try the
            # root-relative path, the absolute path, and every trailing
            # suffix of them.
            candidates = []
            for root in roots:
                try:
                    candidates.append(path.relative_to(root).as_posix())
                except ValueError:
                    continue
            candidates.append(path.as_posix())
            for candidate in candidates:
                parts = candidate.split("/")
                for start in range(len(parts)):
                    if self.regex.match("/".join(parts[start:])):
                        return True
            return False
        # Basename pattern: matches at any depth, unless anchored to a root.
        if self.anchored:
            for root in roots:
                try:
                    candidate = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                if fnmatch.fnmatchcase(candidate, self.pattern):
                    return True
            return False
        return fnmatch.fnmatchcase(path.name, self.pattern)


class GitignoreMatcher:
    """Ordered gitignore-style rules; the last matching rule wins."""

    def __init__(self, patterns: Iterable[str]):
        self.rules = [_Rule(p) for p in patterns if p and p.strip()]

    def matches(self, path: Path, is_dir: bool, roots: Iterable[Path]) -> bool:
        excluded = False
        for rule in self.rules:
            if rule.matches(path, is_dir, roots):
                excluded = not rule.negated
        return excluded


def build_matcher(patterns: Iterable[str]) -> GitignoreMatcher:
    """Compile gitignore-style *patterns* into a matcher."""
    return GitignoreMatcher(patterns)
