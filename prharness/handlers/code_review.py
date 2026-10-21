from __future__ import annotations

from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import CodeQualityAgent
from ..review.diff import truncate_diff
from ..review.github import GitHubClient
from .llm_review import LLMReviewHandler, ReviewScope

HEADER = "## AI code quality review"


class CodeReviewHandler(LLMReviewHandler):
    """Runs the OpenAI code-quality agent on a PR and posts the result as a comment-only review."""

    name = "code_review"
    header = HEADER

    def __init__(
        self,
        settings: Settings,
        *,
        github: GitHubClient | None = None,
        openai_client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(settings, github=github, openai_client=openai_client)
        self._agent = CodeQualityAgent(
            self._openai,
            settings.openai_model,
            max_turns=settings.review_max_turns,
            max_file_reads=settings.review_max_file_reads,
            max_file_chars=settings.review_max_file_chars,
        )

    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None:
        shown, truncated = truncate_diff(scope.diff, self.settings.review_max_diff_chars)

        async def read_file(path: str) -> str | None:
            return await self._github.get_file(event.repo_full_name, path, event.head_sha)

        result = await self._agent.review(
            event, shown, read_file, diff_truncated=truncated, context=scope.context()
        )
        await self._post(event, scope, result, self.settings.openai_model)
