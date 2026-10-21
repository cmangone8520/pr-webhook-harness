from __future__ import annotations

import asyncio
import logging

from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import ReviewAgent
from ..review.compat import (
    DOC_FILES,
    changed_paths,
    compat_context,
    compat_signals,
    is_compat_relevant,
)
from ..review.compat_prompt import COMPAT_SYSTEM_PROMPT, SUBMIT_COMPAT_REVIEW_TOOL
from ..review.diff import truncate_diff
from ..review.github import GitHubClient
from .llm_review import LLMReviewHandler, ReviewScope

log = logging.getLogger("prharness.handlers.compat_review")

HEADER = "## AI breaking-change review"


class CompatReviewHandler(LLMReviewHandler):
    """Breaking-change reviewer: flags API, endpoint, schema, migration and configuration changes
    that can break existing callers or deployments. Comment-only."""

    name = "compat_review"
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
            system_prompt=COMPAT_SYSTEM_PROMPT,
            submit_tool=SUBMIT_COMPAT_REVIEW_TOOL,
        )

    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None:
        repo = event.repo_full_name
        if not any(is_compat_relevant(p) for p in changed_paths(scope.diff)):
            log.info("%s skipped: no code, schema or migration changes pr=%s#%d",
                     self.name, repo, event.number)  # fmt: skip
            return

        async def read_file(path: str) -> str | None:
            return await self._github.get_file(repo, path, event.head_sha)

        docs = await asyncio.gather(*(read_file(p) for p in DOC_FILES), return_exceptions=True)
        signals = compat_signals(scope.diff, "\n".join(d for d in docs if isinstance(d, str)))
        shown, truncated = truncate_diff(scope.diff, self.settings.review_max_diff_chars)
        context = [scope.context(), compat_context(signals)]
        result = await self._agent.review(
            event,
            shown,
            read_file,
            diff_truncated=truncated,
            context="\n\n".join(c for c in context if c),
        )
        kinds = sorted({s.kind for s in signals})
        note = f"Breaking-change leads: {len(signals)}" + (
            f" ({', '.join(kinds)})." if kinds else "."
        )
        await self._post(event, scope, result, self._model, [note])
