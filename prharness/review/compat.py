"""Deterministic breaking-change signals in a diff, used as leads for ``compat_review``."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from .testmap import definition_name, is_source_file

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_CLASS = re.compile(r"^\s*(?:export\s+)?(?:public\s+)?(?:abstract\s+)?class\s+([A-Za-z_]\w*)")
_ROUTE = re.compile(
    r"@\w+\.(get|post|put|patch|delete|route|api_route)\(\s*[\"']([^\"']+)"
    r"|\b(?:app|router|server)\.(get|post|put|patch|delete|all)\(\s*[\"'`]([^\"'`]+)"
    r"|\bHandleFunc\(\s*\"([^\"]+)"
)
_MIGRATION_PATH = re.compile(
    r"(?:^|/)(?:migrations?|alembic/versions|db/migrate|prisma/migrations)/|\.sql$", re.IGNORECASE
)
_RISKY_SQL = re.compile(
    r"\bDROP\s+(?:TABLE|COLUMN|INDEX|CONSTRAINT)\b|\bRENAME\s+(?:COLUMN|TO)\b|\bTRUNCATE\b"
    r"|\bALTER\s+COLUMN\b.*\b(?:TYPE|SET\s+NOT\s+NULL)\b"
    r"|\bADD\s+COLUMN\b(?!.*\bDEFAULT\b).*\bNOT\s+NULL\b"
    r"|\bCREATE\s+(?:UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY)"
    r"|\bop\.(?:drop_column|drop_table|alter_column|rename_table|drop_index)\b"
    r"|\b(?:remove_column|rename_column|drop_table|change_column)\b",
    re.IGNORECASE,
)
_ENV = re.compile(
    r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]+)|os\.(?:environ\.get|getenv)\(\s*[\"']([A-Z][A-Z0-9_]+)"
    r"|process\.env\.([A-Z][A-Z0-9_]+)|process\.env\[\s*[\"']([A-Z][A-Z0-9_]+)"
    r"|os\.(?:Getenv|LookupEnv)\(\s*\"([A-Z][A-Z0-9_]+)"
)
_SCHEMA_PATH = re.compile(
    r"\.(?:proto|graphql|gql|avsc)$|(?:^|/)(?:openapi|swagger)[^/]*\.(?:ya?ml|json)$", re.IGNORECASE
)
DOC_FILES = (".env.example", ".env.sample", ".env.template", "README.md")


@dataclass(frozen=True)
class CompatSignal:
    kind: str
    path: str
    line: int  # new-side line, or old-side line when ``side`` is "old"
    side: str
    detail: str


def _file_lines(diff: str) -> dict[str, list[tuple[str, int, str]]]:
    """Per file (new path, or old path if deleted): ``(sign, line, text)`` for +/- lines."""
    files: dict[str, list[tuple[str, int, str]]] = {}
    old_path = path = None
    old = new = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            old_path = path = None
            in_hunk = False
        elif raw.startswith("--- ") and not in_hunk:
            old_path = raw[6:].strip() if raw.startswith("--- a/") else None
        elif raw.startswith("+++ ") and not in_hunk:
            path = raw[6:].strip() if raw.startswith("+++ b/") else old_path
            if path:
                files.setdefault(path, [])
        elif (m := _HUNK.match(raw)) and path:
            old, new, in_hunk = int(m.group(1)), int(m.group(2)), True
        elif in_hunk and path:
            if raw.startswith("+"):
                files[path].append(("+", new, raw[1:]))
                new += 1
            elif raw.startswith("-"):
                files[path].append(("-", old, raw[1:]))
                old += 1
            elif raw.startswith(" ") or raw == "":
                old, new = old + 1, new + 1
    return files


def changed_paths(diff: str) -> set[str]:
    return set(_file_lines(diff))


def is_compat_relevant(path: str) -> bool:
    return bool(is_source_file(path) or _MIGRATION_PATH.search(path) or _SCHEMA_PATH.search(path))


def _signature(text: str) -> str:
    return re.sub(r"\s+", "", text.split("#")[0]).rstrip(":{")


def compat_signals(diff: str, docs: str = "") -> list[CompatSignal]:
    signals: list[CompatSignal] = []
    files = _file_lines(diff)
    for path, lines in files.items():
        added = [(n, t) for s, n, t in lines if s == "+"]
        removed = [(n, t) for s, n, t in lines if s == "-"]
        if _MIGRATION_PATH.search(path):
            signals += [
                CompatSignal("risky-migration", path, n, "new", t.strip()[:160])
                for n, t in added
                if _RISKY_SQL.search(t)
            ]
        if _SCHEMA_PATH.search(path) and removed:
            signals.append(
                CompatSignal("schema-lines-removed", path, removed[0][0], "old",
                             f"{len(removed)} line(s) removed from an API schema")
            )  # fmt: skip
        if not is_source_file(path):
            continue
        old_defs = {
            name: (n, t)
            for n, t in removed
            if (name := definition_name(t) or _match(_CLASS, t)) and not name.startswith("_")
        }
        new_defs = {
            name: (n, t) for n, t in added if (name := definition_name(t) or _match(_CLASS, t))
        }
        for name, (n, t) in old_defs.items():
            if name not in new_defs:
                signals.append(CompatSignal("removed-public-symbol", path, n, "old", t.strip()))
            elif _signature(t) != _signature(new_defs[name][1]):
                signals.append(
                    CompatSignal("signature-changed", path, new_defs[name][0], "new",
                                 f"{t.strip()}  ->  {new_defs[name][1].strip()}")
                )  # fmt: skip
        old_routes = {r: (n, t) for n, t in removed if (r := _route(t))}
        new_routes = {r: (n, t) for n, t in added if (r := _route(t))}
        for route, (n, _t) in old_routes.items():
            if route not in new_routes:
                signals.append(CompatSignal("removed-endpoint", path, n, "old", " ".join(route)))
        for route, (n, _t) in new_routes.items():
            if route not in old_routes:
                signals.append(CompatSignal("new-endpoint", path, n, "new", " ".join(route)))
        old_env = {v for _n, t in removed for v in _env_vars(t)}
        seen: set[str] = set()
        for n, t in added:
            for var in _env_vars(t):
                if var in old_env or var in seen:
                    continue
                seen.add(var)
                documented = bool(re.search(rf"\b{re.escape(var)}\b", docs))
                kind = "new-env-var" if documented else "undocumented-env-var"
                signals.append(CompatSignal(kind, path, n, "new", var))
        new_env = {v for _n, t in added for v in _env_vars(t)}
        for var in sorted(old_env - new_env):
            line = next(n for n, t in removed if var in _env_vars(t))
            signals.append(CompatSignal("removed-env-var", path, line, "old", var))
    return signals


def _match(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def _route(text: str) -> tuple[str, str] | None:
    m = _ROUTE.search(text)
    if not m:
        return None
    if m.group(5):
        return ("ANY", m.group(5))
    method, path = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return (method.upper(), path)


def _env_vars(text: str) -> list[str]:
    return [next(g for g in m.groups() if g) for m in _ENV.finditer(text)]


def compat_context(signals: list[CompatSignal]) -> str:
    if not signals:
        return "<compat_signals>\n[]\n</compat_signals>"
    body = json.dumps([asdict(s) for s in signals], indent=1)
    return f"<compat_signals>\n{body}\n</compat_signals>"
