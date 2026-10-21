from __future__ import annotations

import asyncio
import dataclasses
import logging

from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import ReviewAgent, ReviewResult
from ..review.diff import added_lines, truncate_diff
from ..review.github import GitHubClient
from ..review.scanners import (
    ScanFinding,
    ScannerUnavailableError,
    SemgrepScanner,
    scan_secrets,
    scanner_context,
)
from ..review.security_prompt import SECURITY_SYSTEM_PROMPT, SUBMIT_SECURITY_REVIEW_TOOL
from .llm_review import LLMReviewHandler

log = logging.getLogger("prharness.handlers.security_review")

HEADER = "## AI security peer review"
_FETCH_CONCURRENCY = 8


def attribute_sources(result: ReviewResult, findings: list[ScanFinding]) -> ReviewResult:
    """Relabel a scanner-sourced comment as ``llm`` unless that scanner hit the same line with a
    matching CWE (when both sides name one)."""
    hits: dict[tuple[str, str, int], set[str]] = {}
    for f in findings:
        hits.setdefault((f.source, f.path, f.line), set()).add(f.cwe)

    def backed(source: str, path: str, line: int, cwe: str) -> bool:
        cwes = hits.get((source, path, line))
        if cwes is None:
            return False
        return not cwe or "" in cwes or cwe in cwes

    comments = []
    for c in result.comments:
        if c.source in ("semgrep", "secret-scan") and not backed(c.source, c.path, c.line, c.cwe):
            log.info("relabel source=%s->llm path=%s line=%d", c.source, c.path, c.line)
            c = dataclasses.replace(c, source="llm")
        comments.append(c)
    return dataclasses.replace(result, comments=comments)


class SecurityReviewHandler(LLMReviewHandler):
    """Application-security reviewer: Semgrep + secret scan hits triaged by an OpenAI agent,
    plus the agent's own findings, posted as a comment-only PR review."""

    name = "security_review"
    header = HEADER

    def __init__(
        self,
        settings: Settings,
        *,
        github: GitHubClient | None = None,
        openai_client: AsyncOpenAI | None = None,
        semgrep: SemgrepScanner | None = None,
    ) -> None:
        super().__init__(settings, github=github, openai_client=openai_client)
        self.timeout_seconds = settings.security_timeout_seconds
        self._model = settings.security_model or settings.openai_model
        self._semgrep = semgrep
        if semgrep is None and settings.semgrep_enabled:
            self._semgrep = SemgrepScanner(
                settings.semgrep_bin, settings.semgrep_config, settings.semgrep_timeout_seconds
            )
        self._agent = ReviewAgent(
            self._openai,
            self._model,
            max_turns=settings.review_max_turns,
            max_file_reads=settings.review_max_file_reads,
            max_file_chars=settings.review_max_file_chars,
            system_prompt=SECURITY_SYSTEM_PROMPT,
            submit_tool=SUBMIT_SECURITY_REVIEW_TOOL,
        )

    async def handle(self, event: PullRequestEvent) -> None:
        diff = await self._fetch_diff(event)
        if diff is None:
            return
        added: dict[str, set[int]] = {}
        for path, line, _ in added_lines(diff):
            added.setdefault(path, set()).add(line)

        secrets = scan_secrets(diff)
        semgrep, semgrep_note = await self._run_semgrep(event, added)
        findings = secrets + semgrep
        notes = [semgrep_note, f"Secret scan: {len(secrets)} hit(s) on added lines."]

        shown, truncated = truncate_diff(diff, self.settings.review_max_diff_chars)

        async def read_file(path: str) -> str | None:
            return await self._github.get_file(event.repo_full_name, path, event.head_sha)

        result = await self._agent.review(
            event,
            shown,
            read_file,
            diff_truncated=truncated,
            context=scanner_context(findings) if findings else "",
        )
        await self._post(event, diff, attribute_sources(result, findings), self._model, notes)

    async def _run_semgrep(
        self, event: PullRequestEvent, added: dict[str, set[int]]
    ) -> tuple[list[ScanFinding], str]:
        if self._semgrep is None:
            return [], "Semgrep: disabled."
        paths = sorted(added)[: self.settings.security_max_scan_files]
        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch(path: str) -> tuple[str, str | None]:
            async with semaphore:
                return path, await self._github.get_file(event.repo_full_name, path, event.head_sha)

        fetched = await asyncio.gather(*(fetch(p) for p in paths))
        files = {path: content for path, content in fetched if content is not None}
        try:
            results = await self._semgrep.scan(files)
        except (ScannerUnavailableError, RuntimeError, TimeoutError, OSError) as exc:
            log.warning("semgrep failed pr=%s#%d error=%s", event.repo_full_name, event.number, exc)
            return [], f"Semgrep: did not run ({type(exc).__name__}); LLM review only."
        on_changed = [f for f in results if f.line in added.get(f.path, ())]
        note = (
            f"Semgrep (`{self._semgrep.label}`): {len(on_changed)} hit(s) on added lines "
            f"across {len(files)} file(s)."
        )
        return on_changed, note
