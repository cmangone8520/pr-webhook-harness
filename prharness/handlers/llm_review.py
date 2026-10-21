from __future__ import annotations

import asyncio
import logging
import weakref
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.agent import ReviewComment, ReviewResult
from ..review.diff import commentable_lines, filter_files
from ..review.github import GitHubClient
from .base import Handler

log = logging.getLogger("prharness.handlers.llm_review")

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_PREVIOUS_SNIPPET_CHARS = 200


@dataclass(frozen=True)
class PriorComment:
    path: str
    line: int
    body: str


@dataclass(frozen=True)
class ReviewScope:
    """What one run reviews: the whole PR, or only the commits since this handler's last review.

    ``pr_diff`` is always the whole PR (GitHub only accepts inline comments on it); ``diff`` is
    the part to review. ``previous`` holds this handler's earlier inline comments on the PR.
    """

    pr_diff: str
    diff: str
    base_sha: str | None = None
    previous: tuple[PriorComment, ...] = ()

    @property
    def incremental(self) -> bool:
        return self.base_sha is not None

    def context(self) -> str:
        parts = []
        if self.base_sha:
            parts.append(
                "NOTE: this is a re-review after new commits were pushed. The diff shows only the "
                f"changes since commit {self.base_sha[:12]}, which you already reviewed. Review "
                "only these changes; use `read_file` for surrounding code."
            )
        if self.previous:
            lines = [
                f"- {c.path}:{c.line}: {' '.join(c.body.split())[:_PREVIOUS_SNIPPET_CHARS]}"
                for c in self.previous
            ]
            parts.append(
                "<previous_comments>\nYou already posted these comments on this PR. Do not repeat "
                "them; report only issues they do not cover.\n"
                + "\n".join(lines)
                + "\n</previous_comments>"
            )
        return "\n\n".join(parts)


class LLMReviewHandler(Handler):
    """Shared plumbing for OpenAI review handlers that post one comment-only PR review.

    Read-only apart from that review: it never pushes commits or edits the PR, and the
    review is always a plain ``COMMENT`` (it never approves or requests changes).
    Requires ``OPENAI_API_KEY`` and ``GITHUB_TOKEN`` (with pull request write access).
    """

    header: str = ""

    def __init__(
        self,
        settings: Settings,
        *,
        github: GitHubClient | None = None,
        openai_client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(settings)
        missing = [
            env
            for env, value in (
                ("OPENAI_API_KEY", settings.openai_api_key),
                ("GITHUB_TOKEN", settings.github_token),
            )
            if not value.get_secret_value().strip()
        ]
        if missing and (github is None or openai_client is None):
            raise ValueError(f"{self.name} handler requires {', '.join(missing)}")
        self._github = github or GitHubClient(
            settings.github_token.get_secret_value(), settings.github_api_url
        )
        self._openai = openai_client or AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url or None,
            timeout=settings.openai_timeout_seconds,
        )
        self._login: str | None = settings.github_reviewer_login.strip() or None
        self._login_resolved = self._login is not None
        self._locks: weakref.WeakValueDictionary[tuple[str, int], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def handle(self, event: PullRequestEvent) -> None:
        key = (event.repo_full_name, event.number)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        async with lock:  # serialize runs per PR so a quick second push sees the first review
            scope = await self._scope(event)
            if scope is not None:
                await self._review(event, scope)

    @abstractmethod
    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None: ...

    async def _scope(self, event: PullRequestEvent) -> ReviewScope | None:
        """Review the whole PR on open; afterwards only commits since this handler's last review.

        Falls back to the whole PR when there is no earlier review, the token's login is
        unknown, or history was rewritten (force-push/rebase) so the old commit isn't an ancestor.
        """
        repo, number = event.repo_full_name, event.number
        pr_diff = await self._github.get_diff(repo, number)
        if not pr_diff.strip():
            log.info("%s skipped: empty diff pr=%s#%d", self.name, repo, number)
            return None
        full = ReviewScope(pr_diff, pr_diff)
        if event.action == "opened":
            return full
        login = await self._reviewer_login()
        if login is None:
            return full
        mine = [
            r
            for r in await self._github.list_reviews(repo, number)
            if (r.get("user") or {}).get("login") == login
            and (r.get("body") or "").startswith(self.header)
        ]
        if not mine:
            return full
        last = mine[-1]["commit_id"]
        if last == event.head_sha:
            log.info("%s skipped: head already reviewed pr=%s#%d", self.name, repo, number)
            return None
        previous = await self._previous_comments(event, {r["id"] for r in mine})
        compare = await self._github.get_compare(repo, last, event.head_sha)
        if compare is None or compare.get("status") != "ahead":
            log.info("%s full re-review (history rewritten) pr=%s#%d", self.name, repo, number)
            return ReviewScope(pr_diff, pr_diff, previous=previous)
        diff = await self._github.get_compare_diff(repo, last, event.head_sha)
        diff = filter_files(diff, set(commentable_lines(pr_diff)))
        if not diff.strip():
            log.info("%s skipped: no new PR changes pr=%s#%d", self.name, repo, number)
            return None
        log.info("%s incremental review since=%s pr=%s#%d", self.name, last[:12], repo, number)
        return ReviewScope(pr_diff, diff, base_sha=last, previous=previous)

    async def _reviewer_login(self) -> str | None:
        if not self._login_resolved:
            self._login = await self._github.get_login()
            self._login_resolved = True
            if self._login is None:
                log.warning(
                    "%s: can't resolve the token's login; set GITHUB_REVIEWER_LOGIN to enable "
                    "incremental re-reviews",
                    self.name,
                )
        return self._login

    async def _previous_comments(
        self, event: PullRequestEvent, review_ids: set[int]
    ) -> tuple[PriorComment, ...]:
        comments = await self._github.list_review_comments(event.repo_full_name, event.number)
        return tuple(
            PriorComment(c["path"], c.get("line") or c.get("original_line") or 0, c["body"])
            for c in comments
            if c.get("pull_request_review_id") in review_ids
        )

    async def _post(
        self,
        event: PullRequestEvent,
        scope: ReviewScope,
        result: ReviewResult,
        model: str,
        notes: Sequence[str] = (),
    ) -> None:
        repo, number = event.repo_full_name, event.number
        allowed = commentable_lines(scope.pr_diff)
        if scope.incremental:
            new = commentable_lines(scope.diff)
            allowed = {p: lines & new.get(p, set()) for p, lines in allowed.items()}
            notes = [f"Re-review of commits since `{scope.base_sha[:12]}`.", *notes]
        result = _drop_repeats(result, scope.previous)
        inline, other = _split_comments(result, allowed, self.settings.review_max_comments)
        comments = [
            {"path": c.path, "line": c.line, "side": "RIGHT", "body": _comment_body(c)}
            for c in inline
        ]
        response = await self._github.create_review(
            repo,
            number,
            commit_id=event.head_sha,
            body=self._review_body(result, other, model, notes),
            comments=comments,
        )
        if response.status_code == 422 and comments:
            log.warning("inline comments rejected; posting summary only pr=%s#%d", repo, number)
            response = await self._github.create_review(
                repo,
                number,
                commit_id=event.head_sha,
                body=self._review_body(result, inline + other, model, notes),
                comments=[],
            )
        response.raise_for_status()
        log.info(
            "%s posted pr=%s#%d inline=%d other=%d",
            self.name,
            repo,
            number,
            len(inline),
            len(other),
        )

    def _review_body(
        self, result: ReviewResult, extra: list[ReviewComment], model: str, notes: Sequence[str]
    ) -> str:
        parts = [self.header, "", result.summary or "No summary provided."]
        if extra:
            parts += ["", "### Additional notes"]
            parts += [f"- {_location(c)}{_label(c)}: {c.body}" for c in extra]
        if notes:
            parts += ["", *notes]
        parts += ["", f"<sub>Generated by pr-webhook-harness using `{model}`.</sub>"]
        return "\n".join(parts)

    async def aclose(self) -> None:
        await self._github.aclose()
        await self._openai.close()


def _drop_repeats(result: ReviewResult, previous: Sequence[PriorComment]) -> ReviewResult:
    """Drop findings already posted at the same location with the same severity/category label."""
    seen = {(c.path, c.line, c.body.split("\n", 1)[0]) for c in previous}
    kept = [c for c in result.comments if (c.path, c.line, _label(c)) not in seen]
    return ReviewResult(summary=result.summary, comments=kept)


def _split_comments(
    result: ReviewResult, allowed: dict[str, set[int]], limit: int
) -> tuple[list[ReviewComment], list[ReviewComment]]:
    ranked = sorted(result.comments, key=lambda c: _SEVERITY_ORDER.get(c.severity, 4))[:limit]
    inline = [c for c in ranked if c.line in allowed.get(c.path, ())]
    other = [c for c in ranked if c not in inline]
    return inline, other


def _label(comment: ReviewComment) -> str:
    label = f"**[{comment.severity}] {comment.category}**"
    meta = [comment.cwe, f"source: {comment.source}" if comment.source else ""]
    meta = [m for m in meta if m]
    return f"{label} ({', '.join(meta)})" if meta else label


def _location(comment: ReviewComment) -> str:
    if not comment.path:
        return ""
    return f"`{comment.path}:{comment.line}` " if comment.line > 0 else f"`{comment.path}` "


def _comment_body(comment: ReviewComment) -> str:
    return f"{_label(comment)}\n\n{comment.body}"
