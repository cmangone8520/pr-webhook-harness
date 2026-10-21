"""Deterministic PR facts (size, file mix, linked issues, commits) for ``hygiene_review``."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .testmap import is_source_file, is_test_file

_CLOSING_REF = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:[\w.-]+/[\w.-]+)?#\d+", re.IGNORECASE
)
_ISSUE_REF = re.compile(
    r"(?:^|[\s(])(?:[\w.-]+/[\w.-]+)?#\d+\b|https?://\S+/issues/\d+|\b[A-Z][A-Z0-9]{1,9}-\d+\b"
)
_LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Pipfile.lock",
    "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
}  # fmt: skip
_GENERATED = re.compile(
    r"(?:^|/)(?:dist|build|vendor|node_modules|__snapshots__)/|\.min\.(?:js|css)$|\.pb\.go$"
    r"|_pb2\.py$|\.snap$|\.generated\.\w+$"
)
_DEPS = re.compile(r"(?:^|/)(?:requirements[^/]*\.txt|pyproject\.toml|package\.json|go\.mod)$")
_BAD_COMMIT = re.compile(r"^(?:wip\b|fixup!|squash!|temp\b|tmp\b|asdf|test$|update$|\.+$)", re.I)


@dataclass
class FileStat:
    path: str
    added: int = 0
    deleted: int = 0
    binary: bool = False
    kind: str = "other"


@dataclass
class PRFacts:
    files: list[FileStat] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def deleted(self) -> int:
        return sum(f.deleted for f in self.files)


def file_kind(path: str) -> str:
    name = PurePosixPath(path).name
    if name in _LOCKFILES or _GENERATED.search(path):
        return "generated"
    if is_test_file(path):
        return "test"
    if path.startswith(".github/") or name in {"Dockerfile", "Makefile", ".gitlab-ci.yml"}:
        return "ci-build"
    if _DEPS.search(path):
        return "dependencies"
    if re.search(r"(?:^|/)(?:migrations?|alembic)/|\.sql$", path):
        return "migration"
    if name.lower().endswith((".md", ".rst", ".txt", ".adoc")) or path.startswith("docs/"):
        return "docs"
    if is_source_file(path):
        return "source"
    if name.endswith((".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", ".env.example")):
        return "config"
    return "other"


def diff_stats(diff: str) -> PRFacts:
    facts = PRFacts()
    current: FileStat | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", raw)
            path = m.group(2) if m else raw.split()[-1]
            current = FileStat(path, kind=file_kind(path))
            facts.files.append(current)
        elif current is None:
            continue
        elif raw.startswith("Binary files "):
            current.binary = True
        elif raw.startswith("+") and not raw.startswith("+++ "):
            current.added += 1
        elif raw.startswith("-") and not raw.startswith("--- "):
            current.deleted += 1
    return facts


def linked_issues(text: str) -> dict[str, list[str]]:
    return {
        "closing": sorted({m.group(0).strip() for m in _CLOSING_REF.finditer(text)}),
        "references": sorted({m.group(0).strip(" (") for m in _ISSUE_REF.finditer(text)}),
    }


def hygiene_context(
    facts: PRFacts,
    *,
    body: str,
    commits: list[dict[str, Any]],
    draft: bool,
    max_lines: int,
    max_files: int,
) -> str:
    messages = [(c.get("commit") or {}).get("message", "").split("\n", 1)[0] for c in commits]
    top_dirs = Counter(f.path.split("/", 1)[0] if "/" in f.path else "(root)" for f in facts.files)
    lines_by_kind: dict[str, int] = {}
    for f in facts.files:
        lines_by_kind[f.kind] = lines_by_kind.get(f.kind, 0) + f.added + f.deleted
    reviewable = sum(f.added + f.deleted for f in facts.files if f.kind != "generated")
    payload = {
        "files_changed": len(facts.files),
        "lines_added": facts.added,
        "lines_deleted": facts.deleted,
        "lines_changed_excluding_generated": reviewable,
        "oversized": reviewable > max_lines or len(facts.files) > max_files,
        "size_limits": {"max_changed_lines": max_lines, "max_files": max_files},
        "lines_by_kind": lines_by_kind,
        "top_level_dirs": dict(top_dirs.most_common()),
        "files": [
            {"path": f.path, "kind": f.kind, "added": f.added, "deleted": f.deleted}
            | ({"binary": True} if f.binary else {})
            for f in sorted(facts.files, key=lambda f: -(f.added + f.deleted))[:50]
        ],
        "description_chars": len(body.strip()),
        "linked_issues": linked_issues(body),
        "draft": draft,
        "commits": len(messages),
        "commit_subjects": messages[:30],
        "suspicious_commit_subjects": [m for m in messages if _BAD_COMMIT.search(m.strip())],
    }
    return f"<pr_facts>\n{json.dumps(payload, indent=1)}\n</pr_facts>"
