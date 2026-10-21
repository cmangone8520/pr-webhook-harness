"""Unified diff helpers: which (path, line) pairs GitHub accepts inline comments on."""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Map each changed file to the new-side line numbers visible in the diff."""
    result: dict[str, set[int]] = {}
    path: str | None = None
    line = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path, in_hunk = None, False
        elif raw.startswith("+++ ") and not in_hunk:
            target = raw[4:].strip()
            path = target[2:] if target.startswith("b/") else None
            if path:
                result.setdefault(path, set())
        elif (match := _HUNK.match(raw)) and path:
            line, in_hunk = int(match.group(1)), True
        elif in_hunk and path:
            if raw.startswith("+") or raw.startswith(" ") or raw == "":
                result[path].add(line)
                line += 1
            elif raw.startswith("\\"):
                continue
    return result


def truncate_diff(diff: str, max_chars: int) -> tuple[str, bool]:
    if len(diff) <= max_chars:
        return diff, False
    cut = diff.rfind("\n", 0, max_chars)
    return diff[: cut if cut > 0 else max_chars], True


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """Return ``(path, new_line, text)`` for every ``+`` line in the diff."""
    result: list[tuple[str, int, str]] = []
    path: str | None = None
    line = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path, in_hunk = None, False
        elif raw.startswith("+++ ") and not in_hunk:
            target = raw[4:].strip()
            path = target[2:] if target.startswith("b/") else None
        elif (match := _HUNK.match(raw)) and path:
            line, in_hunk = int(match.group(1)), True
        elif in_hunk and path:
            if raw.startswith("+"):
                result.append((path, line, raw[1:]))
                line += 1
            elif raw.startswith(" ") or raw == "":
                line += 1
    return result


def filter_files(diff: str, paths: set[str]) -> str:
    """Keep only the per-file sections of ``diff`` whose new path is in ``paths``."""
    sections = re.split(r"(?m)^(?=diff --git )", diff)
    return "".join(s for s in sections if s and set(commentable_lines(s)) & paths)
