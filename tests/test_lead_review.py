import json

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.lead_review import HEADER, LeadReviewHandler
from prharness.review.github import GitHubClient
from tests.test_code_review import GH, OPENAI, completion

REPO = f"{GH}/repos/octo-org/octo-repo"
DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n x\n+y\n"


@pytest.fixture
def handler(settings: Settings) -> LeadReviewHandler:
    settings.github_api_url = GH
    settings.openai_model = "test-model"
    settings.github_reviewer_login = "bot[bot]"
    return LeadReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
    )


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d8")


def review(id_, body, sha, login="bot[bot]"):
    return {"id": id_, "body": body, "commit_id": sha, "user": {"login": login}}


@respx.mock
async def test_consolidates_specialist_reviews(handler, event) -> None:
    sha = event.head_sha
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(url__regex=rf"{REPO}/pulls/42/reviews(\?.*)?$").mock(
        return_value=httpx.Response(
            200,
            json=[
                review(1, "## AI code quality review\n\nold", sha),
                review(2, "## AI code quality review\n\nSQL built by concatenation.", sha),
                review(3, "## AI security peer review\n\nSQL injection.", sha),
                review(4, "## AI security peer review\n\nstale", "0" * 40),
                review(5, "## AI security peer review\n\nsomeone else", sha, login="human"),
                review(6, "Looks good to me", sha),
            ],
        )
    )
    respx.get(url__regex=rf"{REPO}/pulls/42/comments(\?.*)?$").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"pull_request_review_id": 2, "path": "app.py", "line": 2, "body": "[high] bug"},
                {"pull_request_review_id": 3, "path": "app.py", "line": 2, "body": "CWE-89"},
                {"pull_request_review_id": 1, "path": "app.py", "line": 2, "body": "old one"},
            ],
        )
    )
    lead = {
        "risk": "critical",
        "summary": "SQL injection must be fixed.",
        "must_fix": [{"title": "SQL injection", "location": "app.py:2",
                      "reviewers": ["code quality review", "security peer review"],
                      "details": "Use parameters."}],
        "should_fix": [],
        "duplicates_merged": 1,
    }  # fmt: skip
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_lead_review", lead))
    )
    reviews = respx.post(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )

    await handler.handle(event)

    request = json.loads(llm.calls[0].request.content)
    assert request["tool_choice"]["function"]["name"] == "submit_lead_review"
    prompt = request["messages"][1]["content"]
    assert "SQL built by concatenation." in prompt and "SQL injection." in prompt
    assert "- inline `app.py:2`: CWE-89" in prompt
    assert "old one" not in prompt and "stale" not in prompt and "someone else" not in prompt
    posted = json.loads(reviews.calls.last.request.content)
    assert posted["event"] == "COMMENT" and posted["comments"] == []
    body = posted["body"]
    assert body.startswith(HEADER) and "**Overall risk: CRITICAL**" in body
    assert "### Must fix before merge (1)" in body
    assert "1. **SQL injection** `app.py:2` (code quality review, security peer review)" in body
    assert "Consolidated 2 review(s) (code quality review, security peer review)" in body
    assert "with 2 inline finding(s); 1 duplicate(s) merged." in body


@respx.mock
async def test_skips_without_specialist_reviews(handler, event) -> None:
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(url__regex=rf"{REPO}/pulls/42/reviews(\?.*)?$").mock(
        return_value=httpx.Response(200, json=[])
    )
    llm = respx.post(f"{OPENAI}/chat/completions")
    await handler.handle(event)
    assert not llm.called
