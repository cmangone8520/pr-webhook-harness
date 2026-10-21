"""Static map from changed source code to the tests that might cover it (``test_review``)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java", ".kt", ".rb", ".rs",
    ".cs", ".php", ".swift", ".scala",
}  # fmt: skip
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?|testing)/"
    r"|(?:^|/)test_[^/]+\.py$|_test\.(?:py|go|rb|exs?)$|\.(?:test|spec)\.[cm]?[jt]sx?$"
    r"|Tests?\.(?:java|kt|cs|swift)$|_spec\.rb$|(?:^|/)conftest\.py$"
)
_NOT_SOURCE = re.compile(r"(?:^|/)(?:vendor|node_modules|third_party|migrations|dist|build)/")
_DEFINITIONS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
        r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    ),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$")
_GENERIC_STEMS = {"__init__", "index", "main", "mod", "lib", "app", "utils", "util", "types"}


def is_test_file(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def is_source_file(path: str) -> bool:
    return (
        PurePosixPath(path).suffix in _CODE_EXTENSIONS
        and not is_test_file(path)
        and not _NOT_SOURCE.search(path)
    )


def definition_name(text: str) -> str | None:
    for pattern in _DEFINITIONS:
        if m := pattern.search(text):
            return m.group(1)
    return None


@dataclass
class ChangedSource:
    path: str
    added_functions: dict[str, int] = field(default_factory=dict)  # name -> definition line
    modified_functions: dict[str, int] = field(default_factory=dict)  # name -> first added line
    added_lines: int = 0


def changed_sources(diff: str) -> dict[str, ChangedSource]:
    """Source files with added lines, the functions they define, and the existing functions
    they modify (from the enclosing-function context in each hunk header)."""
    result: dict[str, ChangedSource] = {}
    path: str | None = None
    line, context, hunk_has_added = 0, None, False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path = None
        elif raw.startswith("+++ ") and path is None:
            target = raw[4:].strip()
            path = target[2:] if target.startswith("b/") and is_source_file(target[2:]) else ""
        elif (m := _HUNK.match(raw)) and path:
            line, context, hunk_has_added = int(m.group(1)), definition_name(m.group(2)), False
        elif path and raw.startswith("+"):
            unit = result.setdefault(path, ChangedSource(path))
            unit.added_lines += 1
            if name := definition_name(raw[1:]):
                unit.added_functions.setdefault(name, line)
                context = None  # later lines in this hunk belong to the new function
            elif context and not hunk_has_added and context not in unit.added_functions:
                unit.modified_functions.setdefault(context, line)
            hunk_has_added = True
            line += 1
        elif path and (raw.startswith(" ") or raw == ""):
            if name := definition_name(raw[1:]):
                context, hunk_has_added = name, False
            line += 1
    return {p: u for p, u in result.items() if u.added_lines}


def candidate_tests(source: str, test_paths: list[str], limit: int = 5) -> list[str]:
    """Test files whose name mentions the source file's module name."""
    stem = PurePosixPath(source).stem
    if stem in _GENERIC_STEMS:
        stem = PurePosixPath(source).parent.name or stem
    if len(stem) < 3:
        return []
    pattern = re.compile(rf"(?:^|[/_.-]){re.escape(stem)}(?:[/_.-]|$)", re.IGNORECASE)
    return [t for t in test_paths if pattern.search(t)][:limit]


def test_map_context(
    sources: dict[str, ChangedSource], repo_tests: list[str], pr_tests: list[str]
) -> str:
    entries = [
        {
            "path": s.path,
            "added_lines": s.added_lines,
            "added_functions": [{"name": n, "line": ln} for n, ln in s.added_functions.items()],
            "modified_functions": [
                {"name": n, "first_changed_line": ln} for n, ln in s.modified_functions.items()
            ],
            "candidate_tests": candidate_tests(s.path, sorted(set(repo_tests) | set(pr_tests))),
        }
        for s in sources.values()
    ]
    payload = {
        "changed_source_files": entries,
        "test_files_changed_in_pr": pr_tests,
        "test_files_in_repo": len(repo_tests),
    }
    return f"<test_map>\n{json.dumps(payload, indent=1)}\n</test_map>"
