from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from ..config import Settings
from ..events import PullRequestEvent
from ..review.github import GitHubClient
from ..review.lead_prompt import LEAD_SYSTEM_PROMPT, SUBMIT_LEAD_REVIEW_TOOL
from .llm_review import LLMReviewHandler, ReviewScope

log = logging.getLogger("prharness.handlers.lead_review")

HEADER = "## AI lead review"
SPECIALIST_PREFIX = "## AI "
MAX_INPUT_CHARS = 120_000


class LeadReviewHandler(LLMReviewHandler):
    """Runs after the specialist reviewers (``HANDLERS=(code_review,security_review)>lead_review``)
    and posts one comment: overall risk, must-fix items and should-fix items, with duplicates
    merged across reviewers. It reads only the specialist reviews posted on the current head."""

    name = "lead_review"
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

    async def _review(self, event: PullRequestEvent, scope: ReviewScope) -> None:
        repo, number = event.repo_full_name, event.number
        login = self.settings.github_reviewer_login or await self._reviewer_login()
        latest: dict[str, dict[str, Any]] = {}
        for review in await self._github.list_reviews(repo, number):
            body = review.get("body") or ""
            if (
                review.get("commit_id") == event.head_sha
                and body.startswith(SPECIALIST_PREFIX)
                and not body.startswith(HEADER)
                and (login is None or (review.get("user") or {}).get("login") == login)
            ):
                latest[body.split("\n", 1)[0]] = review  # keep each reviewer's newest review
        if not latest:
            log.info("%s skipped: no specialist reviews on %s pr=%s#%d",
                     self.name, event.head_sha[:12], repo, number)  # fmt: skip
            return
        by_review: dict[int, list[dict[str, Any]]] = {r["id"]: [] for r in latest.values()}
        for comment in await self._github.list_review_comments(repo, number):
            if comment.get("pull_request_review_id") in by_review:
                by_review[comment["pull_request_review_id"]].append(comment)

        reviewers = [_reviewer_name(h) for h in latest]
        blocks = [
            _review_block(_reviewer_name(h), r, by_review[r["id"]]) for h, r in latest.items()
        ]
        reviews_text = "\n\n".join(blocks)[:MAX_INPUT_CHARS]
        pr = event.payload.get("pull_request", {})
        user = (
            f"Repository: {repo}\nPull request #{number}: {event.title}\n\n"
            f"<description>\n{(pr.get('body') or '').strip() or '(none)'}\n</description>\n\n"
            + (f"Scope: re-review of commits since `{scope.base_sha[:12]}`.\n\n"
               if scope.incremental and scope.base_sha else "")
            + f"<specialist_reviews>\n{reviews_text}\n</specialist_reviews>"
        )  # fmt: skip
        response = await self._openai.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": LEAD_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            tools=[SUBMIT_LEAD_REVIEW_TOOL],
            tool_choice={"type": "function", "function": {"name": "submit_lead_review"}},
        )
        calls = response.choices[0].message.tool_calls or []
        args = json.loads(calls[0].function.arguments) if calls else {}
        findings = sum(len(c) for c in by_review.values())
        body = _render(args, reviewers, findings, self._model)
        result = await self._github.create_review(
            repo, number, commit_id=event.head_sha, body=body, comments=[]
        )
        result.raise_for_status()
        log.info("%s posted pr=%s#%d risk=%s must_fix=%d", self.name, repo, number,
                 args.get("risk"), len(args.get("must_fix") or []))  # fmt: skip


def _reviewer_name(header: str) -> str:
    return header.removeprefix(SPECIALIST_PREFIX).strip()


def _review_block(name: str, review: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    lines = [f'<review reviewer="{name}">', (review.get("body") or "").strip()]
    for c in comments:
        line = c.get("line") or c.get("original_line") or 0
        lines.append(f"- inline `{c.get('path')}:{line}`: {c.get('body', '').strip()}")
    lines.append("</review>")
    return "\n".join(lines)


def _render(args: dict[str, Any], reviewers: list[str], findings: int, model: str) -> str:
    risk = str(args.get("risk") or "unknown").upper()
    parts = [HEADER, "", f"**Overall risk: {risk}**", "", args.get("summary") or ""]
    for title, key in (("Must fix before merge", "must_fix"), ("Should fix", "should_fix")):
        items = args.get(key) or []
        if not items:
            continue
        parts += ["", f"### {title} ({len(items)})"]
        for i, item in enumerate(items, 1):
            where = f" `{item['location']}`" if item.get("location") else ""
            who = ", ".join(item.get("reviewers") or [])
            parts.append(f"{i}. **{item.get('title', '')}**{where}" + (f" ({who})" if who else ""))
            if item.get("details"):
                parts.append(f"   {item['details']}")
    parts += [
        "",
        f"Consolidated {len(reviewers)} review(s) ({', '.join(reviewers)}) with {findings} "
        f"inline finding(s); {int(args.get('duplicates_merged') or 0)} duplicate(s) merged.",
        "",
        f"<sub>Generated by pr-webhook-harness using `{model}`.</sub>",
    ]
    return "\n".join(parts)
