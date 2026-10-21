from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import ReviewAgent
from ..review.diff import added_lines, truncate_diff
from ..review.github import GitHubClient
from ..review.infra import SOURCE, infra_kind, is_infra_candidate, scan_infra
from ..review.infra_prompt import INFRA_SYSTEM_PROMPT, SUBMIT_INFRA_REVIEW_TOOL
from ..review.scanners import ScannerUnavailableError, SemgrepScanner, scanner_context
from .llm_review import LLMReviewHandler, ReviewScope
from .security_review import attribute_sources

log = logging.getLogger("prharness.handlers.infra_review")

HEADER = "## AI CI/CD & infrastructure security review"
_FETCH_CONCURRENCY = 8


class InfraReviewHandler(LLMReviewHandler):
    """CI/CD and IaC security reviewer for GitHub Actions, Dockerfiles, compose, Terraform and
    Kubernetes files: Semgrep IaC packs + built-in checks on added lines, triaged by an OpenAI
    agent. Does nothing on PRs that don't touch such files."""

    name = "infra_review"
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
                settings.semgrep_bin,
                settings.infra_semgrep_config,
                settings.semgrep_timeout_seconds,
            )
        self._agent = ReviewAgent(
            self._openai,
            self._model,
            max_turns=settings.review_max_turns,
            max_file_reads=settings.review_max_file_reads,
            max_file_chars=settings.review_max_file_chars,
            system_prompt=INFRA_SYSTEM_PROMPT,
            submit_tool=SUBMIT_INFRA_REVIEW_TOOL,
        )

    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None:
        repo, number = event.repo_full_name, event.number
        added: dict[str, set[int]] = {}
        for path, line, _ in added_lines(scope.diff):
            added.setdefault(path, set()).add(line)
        candidates = sorted(p for p in added if is_infra_candidate(p))
        files = await self._fetch(repo, candidates[: self.settings.security_max_scan_files], event)
        files = {p: c for p, c in files.items() if infra_kind(p, c)}
        if not files:
            log.info("%s skipped: no CI/CD or IaC files changed pr=%s#%d", self.name, repo, number)
            return

        builtin = scan_infra(files, added)
        semgrep, semgrep_note = await self._run_semgrep(files, added, event)
        findings = builtin + semgrep
        notes = [semgrep_note, f"Built-in CI/IaC checks: {len(builtin)} hit(s) on added lines."]

        shown, truncated = truncate_diff(scope.diff, self.settings.review_max_diff_chars)

        async def read_file(path: str) -> str | None:
            return await self._github.get_file(repo, path, event.head_sha)

        result = await self._agent.review(
            event,
            shown,
            read_file,
            diff_truncated=truncated,
            context="\n\n".join(
                c for c in (scope.context(), scanner_context(findings) if findings else "") if c
            ),
        )
        result = attribute_sources(result, findings, sources=("semgrep", SOURCE))
        await self._post(event, scope, result, self._model, notes)

    async def _fetch(self, repo: str, paths: list[str], event: PullRequestEvent) -> dict[str, str]:
        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch(path: str) -> tuple[str, str | None]:
            async with semaphore:
                return path, await self._github.get_file(repo, path, event.head_sha)

        fetched = await asyncio.gather(*(fetch(p) for p in paths))
        return {path: content for path, content in fetched if content is not None}

    async def _run_semgrep(self, files, added, event):
        if self._semgrep is None:
            return [], "Semgrep: disabled."
        try:
            results = await self._semgrep.scan(files)
        except (ScannerUnavailableError, RuntimeError, TimeoutError, OSError) as exc:
            log.warning("semgrep failed pr=%s#%d error=%s", event.repo_full_name, event.number, exc)
            return [], f"Semgrep: did not run ({type(exc).__name__}); built-in checks + LLM only."
        on_changed = [f for f in results if f.line in added.get(f.path, ())]
        note = (
            f"Semgrep (`{self._semgrep.label}`): {len(on_changed)} hit(s) on added lines "
            f"across {len(files)} file(s)."
        )
        return on_changed, note
