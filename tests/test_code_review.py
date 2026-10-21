import json

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.code_review import HEADER, CodeReviewHandler
from prharness.registry import build_handlers
from prharness.review.github import GitHubClient

GH = "https://api.github.test"
OPENAI = "https://openai.test/v1"
DIFF = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def total(items):
-    return sum(items)
+    result = 0
+    return result
"""


def completion(*calls: tuple[str, dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{i}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                            for i, (name, args) in enumerate(calls)
                        ],
                    },
                }
            ],
        },
    )


REVIEW = {
    "summary": "`total` no longer sums its input.",
    "comments": [
        {
            "path": "app.py",
            "line": 3,
            "severity": "high",
            "category": "bug",
            "body": "Always returns 0.",
        },
        {
            "path": "other.py",
            "line": 9,
            "severity": "low",
            "category": "readability",
            "body": "Outside the diff.",
        },
    ],
}


@pytest.fixture
def review_settings(settings: Settings) -> Settings:
    settings.github_api_url = GH
    settings.openai_model = "test-model"
    return settings


@pytest.fixture
def handler(review_settings: Settings) -> CodeReviewHandler:
    return CodeReviewHandler(
        review_settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(
            api_key="sk-test", base_url=OPENAI, http_client=httpx.AsyncClient()
        ),
    )


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d1")


def mock_github(review_status: int = 200):
    repo = f"{GH}/repos/octo-org/octo-repo"
    respx.get(f"{repo}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(f"{repo}/contents/app.py").mock(
        return_value=httpx.Response(200, text="def total(items):\n    result = 0\n")
    )
    respx.get(f"{repo}/contents/missing.py").mock(return_value=httpx.Response(404))
    return respx.post(f"{repo}/pulls/42/reviews")


@respx.mock
async def test_agent_reads_files_and_posts_review(handler, event) -> None:
    reviews = mock_github()
    reviews.mock(return_value=httpx.Response(200, json={"id": 1}))
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        side_effect=[
            completion(("read_file", {"path": "app.py"}), ("read_file", {"path": "missing.py"})),
            completion(("submit_review", REVIEW)),
        ]
    )

    await handler.handle(event)

    first = json.loads(llm.calls[0].request.content)
    assert first["model"] == "test-model"
    assert first["tool_choice"] == "required"
    assert "+    return result" in first["messages"][1]["content"]
    assert llm.calls[0].request.headers["Authorization"] == "Bearer sk-test"
    tool_outputs = [
        m for m in json.loads(llm.calls[1].request.content)["messages"] if m["role"] == "tool"
    ]
    assert [m["content"] for m in tool_outputs] == [
        "def total(items):\n    result = 0\n",
        "File not found.",
    ]

    posted = json.loads(reviews.calls.last.request.content)
    assert posted["commit_id"] == event.head_sha
    assert posted["event"] == "COMMENT"
    assert posted["comments"] == [
        {
            "path": "app.py",
            "line": 3,
            "side": "RIGHT",
            "body": "**[high] bug**\n\nAlways returns 0.",
        }
    ]
    assert posted["body"].startswith(HEADER)
    assert "`other.py:9`" in posted["body"]
    assert reviews.calls.last.request.headers["Authorization"] == "Bearer gh-token"


@respx.mock
async def test_falls_back_to_summary_when_inline_rejected(handler, event) -> None:
    reviews = mock_github()
    reviews.mock(side_effect=[httpx.Response(422), httpx.Response(200, json={"id": 1})])
    respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_review", REVIEW))
    )

    await handler.handle(event)

    retry = json.loads(reviews.calls.last.request.content)
    assert retry["comments"] == []
    assert "`app.py:3`" in retry["body"] and "`other.py:9`" in retry["body"]


@respx.mock
async def test_last_turn_forces_submit(review_settings, event) -> None:
    review_settings.review_max_turns = 2
    handler = CodeReviewHandler(
        review_settings,
        github=GitHubClient("t", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
    )
    reviews = mock_github()
    reviews.mock(return_value=httpx.Response(200, json={"id": 1}))
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        side_effect=[
            completion(("read_file", {"path": "app.py"})),
            completion(("submit_review", {"summary": "LGTM", "comments": []})),
        ]
    )

    await handler.handle(event)

    last = json.loads(llm.calls[1].request.content)
    assert last["tool_choice"] == {"type": "function", "function": {"name": "submit_review"}}
    assert [t["function"]["name"] for t in last["tools"]] == ["submit_review"]
    assert json.loads(reviews.calls.last.request.content)["comments"] == []


@respx.mock
async def test_empty_diff_skips_review(handler, event) -> None:
    respx.get(f"{GH}/repos/octo-org/octo-repo/pulls/42").mock(
        return_value=httpx.Response(200, text="")
    )
    llm = respx.post(f"{OPENAI}/chat/completions")
    await handler.handle(event)
    assert not llm.called


def test_requires_secrets(settings: Settings) -> None:
    settings.handlers = "code_review"
    with pytest.raises(ValueError, match="OPENAI_API_KEY, GITHUB_TOKEN"):
        build_handlers(settings)


def test_secrets_not_in_repr(settings: Settings) -> None:
    settings.openai_api_key = "sk-very-secret"
    assert "sk-very-secret" not in repr(settings)
