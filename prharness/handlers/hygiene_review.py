from __future__ import annotations

import dataclasses
import logging

import httpx
from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import ReviewAgent, ReviewResult
from ..review.diff import truncate_diff
from ..review.github import GitHubClient
from ..review.hygiene import diff_stats, hygiene_context
from ..review.hygiene_prompt import HYGIENE_SYSTEM_PROMPT, SUBMIT_HYGIENE_REVIEW_TOOL
from .llm_review import LLMReviewHandler, ReviewScope

log = logging.getLogger("prharness.handlers.hygiene_review")

HEADER = "## AI PR hygiene review"


class HygieneReviewHandler(LLMReviewHandler):
    """PR-level reviewer: description vs diff, bundled unrelated changes, size, linked issues and
    commit hygiene. Posts a single summary comment with no inline comments, once per PR (it does
    not re-review individual pushes)."""

    name = "hygiene_review"
    header = HEADER

    def __init__(
        self,
        settings: Settings,
        *,
        github: GitHubClient | None = None,
        openai_client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(settings, github=github, openai_client=openai_client)
        self._model = settings.openai_model
        self._agent = ReviewAgent(
            self._openai,
            self._model,
            max_turns=settings.review_max_turns,
            max_file_reads=settings.review_max_file_reads,
            max_file_chars=settings.review_max_file_chars,
            system_prompt=HYGIENE_SYSTEM_PROMPT,
            submit_tool=SUBMIT_HYGIENE_REVIEW_TOOL,
        )

    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None:
        repo = event.repo_full_name
        if scope.incremental:
            log.info("%s skipped: PR already reviewed pr=%s#%d", self.name, repo, event.number)
            return
        pr = event.payload.get("pull_request", {})
        try:
            commits = await self._github.list_commits(repo, event.number)
        except httpx.HTTPError as exc:
            log.warning("commit lookup failed pr=%s#%d error=%s", repo, event.number, exc)
            commits = []
        facts = diff_stats(scope.pr_diff)
        context = hygiene_context(
            facts,
            body=pr.get("body") or "",
            commits=commits,
            draft=bool(pr.get("draft")),
            max_lines=self.settings.hygiene_max_changed_lines,
            max_files=self.settings.hygiene_max_files,
        )
        shown, truncated = truncate_diff(scope.pr_diff, self.settings.review_max_diff_chars)

        async def read_file(path: str) -> str | None:
            return await self._github.get_file(repo, path, event.head_sha)

        result = await self._agent.review(
            event, shown, read_file, diff_truncated=truncated, context=context
        )
        # PR-level findings belong in the summary, never inline.
        result = ReviewResult(
            summary=result.summary,
            comments=[dataclasses.replace(c, line=0) for c in result.comments],
        )
        note = (
            f"{len(facts.files)} file(s), +{facts.added}/-{facts.deleted} lines, "
            f"{len(commits)} commit(s)."
        )
        await self._post(event, scope, result, self._model, [note])
