import json
from datetime import UTC

import httpx
import pytest
import respx

from prharness.precision import (
    ACKNOWLEDGED,
    DISMISSED,
    FIXED,
    IGNORED,
    OPEN,
    RESOLVED,
    CommentOutcome,
    classify_thread,
    parse_label,
    pr_outcomes,
    render_markdown,
    reviewer_name,
    select_prs,
    summarize,
)
from prharness.review.github import GitHubClient, graphql_url
from tests.test_code_review import GH

BOT = "my-app[bot]"


def comment(body="**[high] bug**\n\nBoom.", author="my-app", review=1, up=0, down=0):
    return {
        "url": f"https://github.test/c/{review}",
        "body": body,
        "author": {"login": author},
        "reactionGroups": [
            {"content": "THUMBS_UP", "users": {"totalCount": up}},
            {"content": "THUMBS_DOWN", "users": {"totalCount": down}},
        ],
        "pullRequestReview": {"databaseId": review},
    }


def thread(*comments, resolved=False, outdated=False, path="app.py"):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "comments": {"nodes": list(comments) or [comment()]},
    }


def reply(body, author="alice"):
    return comment(body=body, author=author)


@pytest.mark.parametrize(
    ("t", "closed", "expected"),
    [
        (thread(comment(down=1), outdated=True), False, DISMISSED),
        (thread(comment(), reply("False positive, input is validated upstream")), True, DISMISSED),
        (thread(comment(), reply("Won't fix"), resolved=True), True, DISMISSED),
        (thread(outdated=True), False, FIXED),
        (thread(resolved=True), True, RESOLVED),
        (thread(comment(up=1)), False, ACKNOWLEDGED),
        (thread(comment(), reply("Good catch, thanks")), False, ACKNOWLEDGED),
        (thread(), True, IGNORED),
        (thread(), False, OPEN),
    ],
)
def test_classify_thread(t, closed, expected):
    assert classify_thread(t, pr_closed=closed) == expected


def test_bot_follow_up_is_not_a_human_reply():
    t = thread(comment(), comment(body="This is not an issue for the bot", author="my-app"))
    assert classify_thread(t, pr_closed=False) == OPEN


def test_parse_label_and_reviewer_name():
    assert parse_label("**[High] security** (CWE-89, source: semgrep)\n\nx") == (
        "high",
        "security",
    )
    assert parse_label("plain comment") == ("unknown", "unknown")
    assert reviewer_name("## AI security peer review\n\nbody") == "security peer review"
    assert reviewer_name("LGTM") is None
    assert reviewer_name("") is None


def outcome(reviewer, result, severity="high"):
    return CommentOutcome("o/r", 1, reviewer, severity, "bug", result, "a.py", "u", "x")


def test_summarize_precision():
    outcomes = [
        outcome("code", FIXED),
        outcome("code", RESOLVED),
        outcome("code", DISMISSED),
        outcome("code", IGNORED),
        outcome("code", OPEN),
        outcome("security", OPEN, severity="low"),
    ]
    stats = summarize(outcomes)
    assert stats["code"].total == 5
    assert stats["code"].precision == pytest.approx(2 / 3)
    assert stats["code"].strict_precision == pytest.approx(2 / 4)
    assert stats["security"].precision is None
    assert set(summarize(outcomes, by="severity")) == {"code / high", "security / low"}

    report = render_markdown(outcomes, prs=1)
    assert "| code | 5 | 1 | 1 | 0 | 1 | 1 | 1 | 67% | 50% |" in report
    assert "| security | 1 | 0 | 0 | 0 | 0 | 0 | 1 | n/a | n/a |" in report
    assert "| **All** | 6 |" in report
    assert "### Dismissed comments" in report


def test_graphql_url():
    assert graphql_url("https://api.github.com") == "https://api.github.com/graphql"
    assert graphql_url("https://ghe.test/api/v3/") == "https://ghe.test/api/graphql"


def page(nodes, key):
    conn = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
    return {"data": {"repository": {key: conn}}}


def pr_page(nodes, key, cursor=None, **fields):
    conn = {"pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor}, "nodes": nodes}
    return {"data": {"repository": {"pullRequest": {**fields, key: conn}}}}


@respx.mock
async def test_pr_outcomes_maps_threads_to_reviewers():
    reviews = [
        {"databaseId": 1, "body": "## AI code quality review\n\nx", "author": {"login": BOT}},
        {"databaseId": 2, "body": "## AI lead review\n\nx", "author": {"login": "someone"}},
        {"databaseId": 3, "body": "Looks fine", "author": {"login": "alice"}},
    ]
    threads = [
        thread(comment(review=1), outdated=True),
        thread(comment(review=2)),
        thread(comment(review=3, author="alice")),
    ]
    queries = []

    def handler(request):
        body = json.loads(request.content)
        queries.append(body["variables"])
        if "reviewThreads" not in body["query"]:
            return httpx.Response(200, json=pr_page(reviews, "reviews", state="MERGED"))
        if body["variables"]["after"] is None:
            return httpx.Response(200, json=pr_page(threads[:1], "reviewThreads", cursor="c1"))
        return httpx.Response(200, json=pr_page(threads[1:], "reviewThreads"))

    respx.post(f"{GH}/graphql").mock(side_effect=handler)
    github = GitHubClient("t", GH)
    outcomes = await pr_outcomes(github, "o/r", 7, BOT)
    await github.aclose()

    assert [(o.reviewer, o.outcome, o.severity) for o in outcomes] == [
        ("code quality review", FIXED, "high")
    ]
    assert outcomes[0].excerpt == "Boom."
    assert [q["after"] for q in queries] == [None, None, "c1"]

    respx.post(f"{GH}/graphql").mock(side_effect=handler)
    github = GitHubClient("t", GH)
    everyone = await pr_outcomes(github, "o/r", 7)
    await github.aclose()
    assert sorted(o.reviewer for o in everyone) == ["code quality review", "lead review"]


@respx.mock
async def test_select_prs_honours_limit_and_since():
    from datetime import datetime

    nodes = [
        {"number": 9, "updatedAt": "2026-10-20T00:00:00Z"},
        {"number": 8, "updatedAt": "2026-10-10T00:00:00Z"},
        {"number": 7, "updatedAt": "2026-09-01T00:00:00Z"},
    ]
    respx.post(f"{GH}/graphql").mock(
        return_value=httpx.Response(200, json=page(nodes, "pullRequests"))
    )
    github = GitHubClient("t", GH)
    assert await select_prs(github, "o/r", states=["merged"], limit=2) == [9, 8]
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert await select_prs(github, "o/r", states=["merged"], limit=10, since=since) == [9, 8]
    await github.aclose()


@respx.mock
async def test_graphql_errors_raise():
    respx.post(f"{GH}/graphql").mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "bad"}]})
    )
    github = GitHubClient("t", GH)
    with pytest.raises(RuntimeError, match="bad"):
        await select_prs(github, "o/r", states=["open"], limit=1)
    await github.aclose()
