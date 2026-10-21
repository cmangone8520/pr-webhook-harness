from __future__ import annotations

import asyncio
import logging

import httpx
from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import ReviewAgent
from ..review.dependency_prompt import DEPENDENCY_SYSTEM_PROMPT, SUBMIT_DEPENDENCY_REVIEW_TOOL
from ..review.deps import (
    Dependency,
    DependencyScanner,
    changed_dependencies,
    dependency_context,
    manifest_kind,
    parse_manifest,
)
from ..review.diff import commentable_lines, truncate_diff
from ..review.github import GitHubClient
from ..review.scanners import scanner_context
from .llm_review import LLMReviewHandler, ReviewScope

log = logging.getLogger("prharness.handlers.dependency_review")

HEADER = "## AI dependency review"


class DependencyReviewHandler(LLMReviewHandler):
    """Supply-chain reviewer: diffs dependency manifests/lockfiles between base and head, checks
    each added or changed package against OSV.dev and its public registry (existence, age,
    install scripts, deprecation, license, typosquats), and has an OpenAI agent triage the hits.
    Does nothing on PRs that don't touch a manifest."""

    name = "dependency_review"
    header = HEADER

    def __init__(
        self,
        settings: Settings,
        *,
        github: GitHubClient | None = None,
        openai_client: AsyncOpenAI | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(settings, github=github, openai_client=openai_client)
        self.timeout_seconds = settings.security_timeout_seconds
        self._model = settings.security_model or settings.openai_model
        self._http = http_client or httpx.AsyncClient(
            timeout=20, headers={"User-Agent": "pr-webhook-harness"}
        )
        self._scanner = DependencyScanner(
            self._http,
            osv_url=settings.osv_api_url,
            pypi_url=settings.pypi_url,
            npm_url=settings.npm_registry_url,
            go_proxy_url=settings.go_proxy_url,
        )
        self._agent = ReviewAgent(
            self._openai,
            self._model,
            max_turns=settings.review_max_turns,
            max_file_reads=settings.review_max_file_reads,
            max_file_chars=settings.review_max_file_chars,
            system_prompt=DEPENDENCY_SYSTEM_PROMPT,
            submit_tool=SUBMIT_DEPENDENCY_REVIEW_TOOL,
        )

    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None:
        repo, number = event.repo_full_name, event.number
        manifests = sorted(p for p in commentable_lines(scope.diff) if manifest_kind(p))
        if not manifests:
            log.info(
                "%s skipped: no dependency manifests changed pr=%s#%d", self.name, repo, number
            )
            return
        base = scope.base_sha or event.payload["pull_request"]["base"]["sha"]

        async def deps_at(path: str, ref: str) -> list[Dependency]:
            content = await self._github.get_file(repo, path, ref)
            return parse_manifest(path, content) if content else []

        pairs = await asyncio.gather(
            *(asyncio.gather(deps_at(p, base), deps_at(p, event.head_sha)) for p in manifests)
        )
        changes = [c for before, after in pairs for c in changed_dependencies(before, after)]
        if not changes:
            log.info("%s skipped: no dependency changes pr=%s#%d", self.name, repo, number)
            return
        changes = sorted(changes, key=lambda c: c.dep.lockfile)
        changes = changes[: self.settings.dependency_max_packages]
        findings, facts, notes = await self._scanner.scan(changes)

        shown, truncated = truncate_diff(scope.diff, self.settings.review_max_diff_chars)

        async def read_file(path: str) -> str | None:
            return await self._github.get_file(repo, path, event.head_sha)

        context = [scope.context(), dependency_context(facts)]
        if findings:
            context.append(scanner_context(findings))
        result = await self._agent.review(
            event,
            shown,
            read_file,
            diff_truncated=truncated,
            context="\n\n".join(c for c in context if c),
        )
        advisories = sum(f.source == "osv" for f in findings)
        summary = (
            f"Checked {len(changes)} changed package(s) against OSV.dev and their registry: "
            f"{advisories} advisory hit(s), {len(findings) - advisories} registry flag(s)."
        )
        await self._post(event, scope, result, self._model, [summary, *notes])

    async def aclose(self) -> None:
        await super().aclose()
        await self._http.aclose()
