"""Deterministic security scanners whose hits are triaged by the security agent."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .diff import added_lines

log = logging.getLogger("prharness.review.scanners")

_CWE = re.compile(r"CWE-\d+")
_SEMGREP_SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


@dataclass(frozen=True)
class ScanFinding:
    source: str
    rule: str
    path: str
    line: int
    severity: str
    message: str
    cwe: str = ""


class ScannerUnavailableError(RuntimeError):
    """The scanner binary is not installed."""


# Vendor-published token shapes (ported from appSecPeerReviewer's secret scanner).
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws-secret-access-key",
        re.compile(r"(?i)aws(.{0,20})?(secret|sk)[^A-Za-z0-9]{0,5}([A-Za-z0-9/+=]{40})"),
    ),
    ("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github-fine-grained-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("github-app-token", re.compile(r"\b(ghs|ghu)_[A-Za-z0-9]{36}\b")),
    ("openai-api-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("stripe-secret", re.compile(r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
)


def scan_secrets(diff: str) -> list[ScanFinding]:
    """Flag added lines matching a known credential format. Matched values are never echoed."""
    findings = []
    for path, line, text in added_lines(diff):
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(
                    ScanFinding(
                        source="secret-scan",
                        rule=name,
                        path=path,
                        line=line,
                        severity="critical",
                        message=f"Added line matches the {name} credential format.",
                        cwe="CWE-798",
                    )
                )
                break
    return findings


class SemgrepScanner:
    """Runs the Semgrep CLI over PR files written to a temporary directory."""

    def __init__(self, binary: str, config: str, timeout_seconds: float) -> None:
        self._binary = binary
        self._configs = [c.strip() for c in config.split(",") if c.strip()]
        self._timeout = timeout_seconds

    @property
    def label(self) -> str:
        return ", ".join(self._configs)

    async def scan(self, files: dict[str, str]) -> list[ScanFinding]:
        executable = shutil.which(self._binary)
        if executable is None:
            raise ScannerUnavailableError(f"semgrep binary {self._binary!r} not found")
        if not files:
            return []
        with tempfile.TemporaryDirectory(prefix="prharness-semgrep-") as tmp:
            root = Path(tmp)
            targets = []
            for path, content in files.items():
                target = safe_target(root, path)
                if target is None:
                    log.warning("semgrep skipped unsafe path=%r", path)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                targets.append(str(target.relative_to(root)))
            if not targets:
                return []
            args = [executable, "scan", "--json", "--metrics=off", "--disable-version-check"]
            for config in self._configs:
                args += ["--config", config]
            proc = await asyncio.create_subprocess_exec(
                *args,
                "--quiet",
                "--",
                *targets,
                cwd=tmp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), self._timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
        if proc.returncode not in (0, 1):
            tail = stderr.decode(errors="replace")[-500:]
            raise RuntimeError(f"semgrep exited with {proc.returncode}: {tail}")
        return parse_semgrep(stdout)


def safe_target(root: Path, path: str) -> Path | None:
    """Resolve a repo-relative path under ``root``, rejecting absolute or escaping paths."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    return root / relative


def parse_semgrep(output: bytes | str) -> list[ScanFinding]:
    data = json.loads(output or "{}")
    findings = []
    for item in data.get("results") or []:
        try:
            extra: dict[str, Any] = item.get("extra") or {}
            cwe_field = (extra.get("metadata") or {}).get("cwe") or ""
            cwe_text = " ".join(cwe_field) if isinstance(cwe_field, list) else str(cwe_field)
            cwe_match = _CWE.search(cwe_text)
            findings.append(
                ScanFinding(
                    source="semgrep",
                    rule=str(item["check_id"]),
                    path=str(item["path"]),
                    line=int(item["start"]["line"]),
                    severity=_SEMGREP_SEVERITY.get(str(extra.get("severity")).upper(), "low"),
                    message=str(extra.get("message", "")).strip(),
                    cwe=cwe_match.group(0) if cwe_match else "",
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return findings


def scanner_context(findings: list[ScanFinding]) -> str:
    """Render scanner hits as a prompt block for the agent to triage."""
    payload = json.dumps([asdict(f) for f in findings], indent=1)
    return f"<scanner_findings>\n{payload}\n</scanner_findings>"
