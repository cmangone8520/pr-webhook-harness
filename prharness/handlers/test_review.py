from __future__ import annotations

import logging

import httpx
from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import ReviewAgent
from ..review.diff import commentable_lines, truncate_diff
from ..review.github import GitHubClient
from ..review.test_prompt import SUBMIT_TEST_REVIEW_TOOL, TEST_SYSTEM_PROMPT
from ..review.testmap import changed_sources, is_test_file, test_map_context
from .llm_review import LLMReviewHandler, ReviewScope

log = logging.getLogger("prharness.handlers.test_review")

HEADER = "## AI test coverage review"


class TestReviewHandler(LLMReviewHandler):
    """Static test-coverage reviewer: maps changed functions to candidate tests and has an OpenAI
    agent comment on behavior with no test, proposing concrete test cases. It never runs the
    PR's code, so untrusted code can't reach the harness's secrets."""

    __test__ = False  # not a pytest test class
    name = "test_review"
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
            system_prompt=TEST_SYSTEM_PROMPT,
            submit_tool=SUBMIT_TEST_REVIEW_TOOL,
        )

    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None:
        repo = event.repo_full_name
        sources = changed_sources(scope.diff)
        if not sources:
            log.info("%s skipped: no source code changed pr=%s#%d", self.name, repo, event.number)
            return
        pr_tests = sorted(p for p in commentable_lines(scope.pr_diff) if is_test_file(p))
        try:
            repo_tests = [
                p for p in await self._github.get_tree(repo, event.head_sha) if is_test_file(p)
            ]
        except httpx.HTTPError as exc:
            log.warning("tree lookup failed pr=%s#%d error=%s", repo, event.number, exc)
            repo_tests = []

        shown, truncated = truncate_diff(scope.diff, self.settings.review_max_diff_chars)

        async def read_file(path: str) -> str | None:
            return await self._github.get_file(repo, path, event.head_sha)

        context = [scope.context(), test_map_context(sources, repo_tests, pr_tests)]
        result = await self._agent.review(
            event,
            shown,
            read_file,
            diff_truncated=truncated,
            context="\n\n".join(c for c in context if c),
        )
        functions = sum(
            len(s.added_functions) + len(s.modified_functions) for s in sources.values()
        )
        note = (
            f"Static review, no PR code executed: {functions} changed function(s) in "
            f"{len(sources)} source file(s); {len(pr_tests)} test file(s) changed in this PR."
        )
        await self._post(event, scope, result, self._model, [note])
