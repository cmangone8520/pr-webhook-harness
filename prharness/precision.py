"""Measure how often each AI reviewer's inline comments are acted on or dismissed.

Every inline comment posted under an ``## AI ...`` review header gets one outcome:

- ``dismissed``: a 👎 reaction, or a human reply such as "false positive" or "won't fix".
- ``fixed``: GitHub marks the thread outdated because the commented line changed later.
- ``resolved``: the thread was resolved without the line changing.
- ``acknowledged``: a 👍 reaction, or a reply such as "fixed" or "good catch".
- ``ignored``: the PR is merged or closed with none of the above.
- ``open``: the PR is still open with none of the above.

Signals are checked in that order, so an explicit dismissal wins over a later code change.
``precision`` is accepted / (accepted + dismissed), where accepted is fixed, resolved or
acknowledged; ``strict`` also counts ignored comments as misses.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .review.github import GitHubClient

AI_HEADER_PREFIX = "## AI "

FIXED = "fixed"
RESOLVED = "resolved"
ACKNOWLEDGED = "acknowledged"
DISMISSED = "dismissed"
IGNORED = "ignored"
OPEN = "open"
OUTCOMES = (FIXED, RESOLVED, ACKNOWLEDGED, DISMISSED, IGNORED, OPEN)
ACCEPTED = frozenset({FIXED, RESOLVED, ACKNOWLEDGED})

_DISMISS = re.compile(
    r"\b(false[ -]?positive|won'?t fix|wontfix|not (an? )?(issue|problem|bug|concern)|by design"
    r"|intentional|not applicable|n/a|disagree|irrelevant|not relevant)\b",
    re.IGNORECASE,
)
_ACK = re.compile(
    r"\b(fixed|done|addressed|good catch|nice catch|thanks|thank you|updated|will fix)\b",
    re.IGNORECASE,
)
_LABEL = re.compile(r"^\*\*\[(?P<severity>[\w-]+)\] (?P<category>[^*]+)\*\*")

_PRS_QUERY = """
query($owner: String!, $name: String!, $states: [PullRequestState!], $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, states: $states, after: $after,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number updatedAt }
    }
  }
}
"""

_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      reviews(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId body author { login } }
      }
    }
  }
}
"""

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          path
          comments(first: 20) {
            nodes {
              url
              body
              author { login }
              reactionGroups { content users { totalCount } }
              pullRequestReview { databaseId }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class CommentOutcome:
    repo: str
    pr: int
    reviewer: str
    severity: str
    category: str
    outcome: str
    path: str
    url: str
    excerpt: str


@dataclass(frozen=True)
class Stats:
    counts: Counter[str]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def accepted(self) -> int:
        return sum(self.counts[o] for o in ACCEPTED)

    @property
    def precision(self) -> float | None:
        decided = self.accepted + self.counts[DISMISSED]
        return self.accepted / decided if decided else None

    @property
    def strict_precision(self) -> float | None:
        decided = self.accepted + self.counts[DISMISSED] + self.counts[IGNORED]
        return self.accepted / decided if decided else None


def classify_thread(thread: dict[str, Any], *, pr_closed: bool) -> str:
    """Outcome of one review thread whose first comment is the AI's."""
    comments = thread["comments"]["nodes"]
    first = comments[0]
    author = _login(first)
    replies = [c.get("body") or "" for c in comments[1:] if _login(c) != author]
    reactions = {g["content"]: g["users"]["totalCount"] for g in first.get("reactionGroups") or []}
    if reactions.get("THUMBS_DOWN") or any(_DISMISS.search(r) for r in replies):
        return DISMISSED
    if thread.get("isOutdated"):
        return FIXED
    if thread.get("isResolved"):
        return RESOLVED
    if reactions.get("THUMBS_UP") or any(_ACK.search(r) for r in replies):
        return ACKNOWLEDGED
    return IGNORED if pr_closed else OPEN


def parse_label(body: str) -> tuple[str, str]:
    """``(severity, category)`` from a ``**[high] bug**`` comment label."""
    match = _LABEL.match(body.strip())
    if not match:
        return "unknown", "unknown"
    return match["severity"].lower(), match["category"].strip().lower()


def reviewer_name(review_body: str) -> str | None:
    header = (review_body or "").lstrip().splitlines()[0] if review_body.strip() else ""
    if not header.startswith(AI_HEADER_PREFIX):
        return None
    return header.removeprefix(AI_HEADER_PREFIX).strip()


async def select_prs(
    github: GitHubClient,
    repo: str,
    *,
    states: Iterable[str],
    limit: int,
    since: datetime | None = None,
) -> list[int]:
    """Most recently updated PR numbers in ``states`` (e.g. ``MERGED``), newest first."""
    owner, name = repo.split("/", 1)
    variables = {"owner": owner, "name": name, "states": [s.upper() for s in states]}
    numbers: list[int] = []
    async for _, nodes in _pages(github, _PRS_QUERY, variables, ("pullRequests",)):
        for node in nodes:
            if since and datetime.fromisoformat(node["updatedAt"]) < since:
                return numbers
            numbers.append(node["number"])
            if len(numbers) >= limit:
                return numbers
    return numbers


async def pr_outcomes(
    github: GitHubClient, repo: str, number: int, reviewer_login: str = ""
) -> list[CommentOutcome]:
    """Outcomes of every AI inline comment on one PR.

    ``reviewer_login`` limits it to reviews by that account; empty accepts any author.
    """
    owner, name = repo.split("/", 1)
    variables = {"owner": owner, "name": name, "number": number}
    reviewers: dict[int, str] = {}
    state = "OPEN"
    async for pr, nodes in _pages(github, _REVIEWS_QUERY, variables, ("pullRequest", "reviews")):
        state = pr["state"]
        for review in nodes:
            reviewer = reviewer_name(review.get("body") or "")
            if reviewer and _matches(_login(review), reviewer_login):
                reviewers[review["databaseId"]] = reviewer

    outcomes: list[CommentOutcome] = []
    if not reviewers:
        return outcomes
    path = ("pullRequest", "reviewThreads")
    async for _, threads in _pages(github, _THREADS_QUERY, variables, path):
        for thread in threads:
            comments = thread["comments"]["nodes"]
            if not comments:
                continue
            first = comments[0]
            reviewer = reviewers.get((first.get("pullRequestReview") or {}).get("databaseId"))
            if reviewer is None:
                continue
            body = first.get("body") or ""
            severity, category = parse_label(body)
            outcomes.append(
                CommentOutcome(
                    repo=repo,
                    pr=number,
                    reviewer=reviewer,
                    severity=severity,
                    category=category,
                    outcome=classify_thread(thread, pr_closed=state != "OPEN"),
                    path=thread.get("path") or "",
                    url=first.get("url") or "",
                    excerpt=_excerpt(body),
                )
            )
    return outcomes


def summarize(outcomes: Iterable[CommentOutcome], by: str = "reviewer") -> dict[str, Stats]:
    """Stats keyed by reviewer, optionally split by ``severity`` or ``category``."""
    groups: dict[str, Counter[str]] = {}
    for o in outcomes:
        key = o.reviewer if by == "reviewer" else f"{o.reviewer} / {getattr(o, by)}"
        groups.setdefault(key, Counter())[o.outcome] += 1
    return {key: Stats(groups[key]) for key in sorted(groups)}


def render_markdown(
    outcomes: list[CommentOutcome], *, prs: int, by: str = "reviewer", dismissed: int = 10
) -> str:
    stats = summarize(outcomes, by)
    lines = [
        "## AI reviewer precision",
        "",
        f"{len(outcomes)} inline comments across {prs} PRs.",
        "",
        "| Reviewer | Comments | "
        + " | ".join(o.capitalize() for o in OUTCOMES)
        + " | Precision | Strict |",
        "|---" * (len(OUTCOMES) + 4) + "|",
    ]
    rows = list(stats.items())
    if len(rows) > 1:
        rows.append(("**All**", Stats(sum((s.counts for s in stats.values()), Counter()))))
    for key, s in rows:
        cells = [key, str(s.total), *(str(s.counts[o]) for o in OUTCOMES)]
        cells += [_pct(s.precision), _pct(s.strict_precision)]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "Precision = (fixed + resolved + acknowledged) / (those + dismissed). "
        "Strict also counts ignored comments as misses.",
    ]
    rejected = [o for o in outcomes if o.outcome == DISMISSED][:dismissed]
    if rejected:
        lines += ["", "### Dismissed comments", ""]
        lines += [
            f"- {o.reviewer}, [{o.severity}] {o.category}, `{o.path}`: "
            f"{o.excerpt} ([link]({o.url}))"
            for o in rejected
        ]
    return "\n".join(lines) + "\n"


async def _pages(
    github: GitHubClient, query: str, variables: dict[str, Any], path: tuple[str, ...]
) -> AsyncIterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Yield ``(parent, nodes)`` per page of the connection at ``repository.<path>``."""
    after: str | None = None
    while True:
        node: Any = (await github.graphql(query, {**variables, "after": after}))["repository"]
        parent = node
        for key in path:
            parent, node = node, node.get(key) if node else None
            if node is None:
                raise ValueError(f"{variables.get('owner')}/{variables.get('name')}: no {key}")
        yield parent, node["nodes"]
        if not node["pageInfo"]["hasNextPage"]:
            return
        after = node["pageInfo"]["endCursor"]


def _login(node: dict[str, Any]) -> str:
    return ((node.get("author") or {}).get("login") or "").lower().removesuffix("[bot]")


def _matches(login: str, wanted: str) -> bool:
    return not wanted or login == wanted.strip().lower().removesuffix("[bot]")


def _excerpt(body: str, limit: int = 120) -> str:
    text = " ".join(_LABEL.sub("", body.strip(), count=1).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"
