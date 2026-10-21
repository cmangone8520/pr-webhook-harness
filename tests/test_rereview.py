import json

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.code_review import HEADER, CodeReviewHandler
from prharness.review.diff import filter_files
from prharness.review.github import GitHubClient
from tests.test_code_review import DIFF, GH, OPENAI, completion

REPO = f"{GH}/repos/octo-org/octo-repo"
HEAD = "2222222222222222222222222222222222222222"
INCREMENTAL = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -2,1 +2,2 @@
     result = 0
+    return result
diff --git a/vendor.py b/vendor.py
--- a/vendor.py
+++ b/vendor.py
@@ -0,0 +1 @@
+x = 1
"""


def finding(line: int, body: str) -> dict:
    return {"path": "app.py", "line": line, "severity": "high", "category": "bug", "body": body}


@pytest.fixture
def handler(settings: Settings) -> CodeReviewHandler:
    settings.github_api_url = GH
    settings.openai_model = "test-model"
    return CodeReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
    )


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    sample_payload["action"] = "synchronize"
    return PullRequestEvent.from_payload(sample_payload, "d2")


def mock_history(*, reviewed_sha: str = "a" * 40, status: str = "ahead", login: int = 200):
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(f"{GH}/user").mock(return_value=httpx.Response(login, json={"login": "bot"}))
    respx.get(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "user": {"login": "bot"},
                    "body": f"{HEADER}\n\nx",
                    "commit_id": reviewed_sha,
                },
                {"id": 8, "user": {"login": "mallory"}, "body": HEADER, "commit_id": HEAD},
            ],
        )
    )
    respx.get(f"{REPO}/pulls/42/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "pull_request_review_id": 7,
                    "path": "app.py",
                    "line": 2,
                    "body": "**[high] bug**\n\nOld finding.",
                },
                {
                    "pull_request_review_id": 8,
                    "path": "app.py",
                    "line": 3,
                    "body": "**[high] bug**\n\nNot ours.",
                },
            ],
        )
    )

    def compare(request: httpx.Request) -> httpx.Response:
        if request.headers["Accept"] == "application/vnd.github.diff":
            return httpx.Response(200, text=INCREMENTAL)
        return httpx.Response(200, json={"status": status})

    respx.get(f"{REPO}/compare/{reviewed_sha}...{HEAD}").mock(side_effect=compare)
    return respx.post(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )


@respx.mock
async def test_push_reviews_only_new_commits_without_repeats(handler, event) -> None:
    reviews = mock_history()
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(
            (
                "submit_review",
                {
                    "summary": "s",
                    "comments": [
                        finding(3, "New finding."),
                        finding(2, "Same spot again."),
                        finding(1, "Old code."),
                    ],
                },
            )
        )
    )

    await handler.handle(event)

    prompt = json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    assert "re-review" in prompt and "Old finding." in prompt
    assert "Not ours." not in prompt and "vendor.py" not in prompt
    posted = json.loads(reviews.calls.last.request.content)
    assert [(c["line"], c["body"].split("\n")[-1]) for c in posted["comments"]] == [
        (3, "New finding.")
    ]
    assert "`app.py:1`" in posted["body"] and "Same spot again." not in posted["body"]
    assert f"Re-review of commits since `{'a' * 12}`." in posted["body"]


@respx.mock
async def test_already_reviewed_head_is_skipped(handler, event) -> None:
    reviews = mock_history(reviewed_sha=HEAD)
    llm = respx.post(f"{OPENAI}/chat/completions")
    await handler.handle(event)
    assert not llm.called and not reviews.called


@respx.mock
async def test_force_push_falls_back_to_full_review(handler, event) -> None:
    reviews = mock_history(status="diverged")
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_review", {"summary": "s", "comments": [finding(1, "x")]}))
    )
    await handler.handle(event)
    prompt = json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    assert "-    return sum(items)" in prompt and "Old finding." in prompt
    assert "re-review after new commits" not in prompt
    assert [c["line"] for c in json.loads(reviews.calls.last.request.content)["comments"]] == [1]


@respx.mock
async def test_unknown_login_reviews_whole_pr(handler, event) -> None:
    reviews = mock_history(login=403)
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_review", {"summary": "s", "comments": []}))
    )
    await handler.handle(event)
    assert (
        "-    return sum(items)"
        in json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    )
    listed = [
        c for c in respx.calls if c.request.method == "GET" and "reviews" in str(c.request.url)
    ]
    assert not listed
    assert reviews.called


def test_filter_files_keeps_only_pr_files() -> None:
    assert filter_files(INCREMENTAL, {"app.py"}) == INCREMENTAL.split("diff --git a/vendor")[0]


def test_pushes_are_dispatched_by_default(settings: Settings) -> None:
    assert {"opened", "synchronize"} <= settings.pr_action_set
