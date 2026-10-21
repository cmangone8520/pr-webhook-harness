import json

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.test_review import HEADER, TestReviewHandler
from prharness.review.github import GitHubClient
from prharness.review.testmap import (
    candidate_tests,
    changed_sources,
    is_source_file,
    is_test_file,
)
from tests.test_code_review import GH, OPENAI, completion

REPO = f"{GH}/repos/octo-org/octo-repo"
DIFF = """\
diff --git a/app/payments.py b/app/payments.py
--- a/app/payments.py
+++ b/app/payments.py
@@ -10,3 +10,4 @@ def charge(amount):
     if amount <= 0:
         raise ValueError("amount")
+    amount = round(amount, 2)
     return gateway.charge(amount)
@@ -20,0 +21,3 @@
+def refund(charge_id, amount=None):
+    return gateway.refund(charge_id, amount)
+
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # app
+docs
"""
TREE = [
    "app/payments.py",
    "app/__init__.py",
    "tests/test_payments.py",
    "tests/test_users.py",
    "web/src/payments.test.ts",
]


def test_classifies_source_and_test_files() -> None:
    for path in ("tests/test_x.py", "pkg/x_test.go", "web/a.spec.ts", "src/__tests__/a.js",
                 "conftest.py", "src/test/java/FooTest.java"):  # fmt: skip
        assert is_test_file(path) and not is_source_file(path), path
    for path in ("app/x.py", "web/src/a.tsx", "cmd/main.go"):
        assert is_source_file(path) and not is_test_file(path), path
    assert not is_source_file("README.md")
    assert not is_source_file("vendor/lib/x.go")


def test_changed_sources_finds_added_and_modified_functions() -> None:
    sources = changed_sources(DIFF)
    assert list(sources) == ["app/payments.py"]
    unit = sources["app/payments.py"]
    assert unit.added_functions == {"refund": 21}
    assert unit.modified_functions == {"charge": 12}
    js = """\
diff --git a/web/a.ts b/web/a.ts
--- a/web/a.ts
+++ b/web/a.ts
@@ -1,0 +1,2 @@
+export const total = (items) => items.length;
+export async function load(id) { return id; }
"""
    assert changed_sources(js)["web/a.ts"].added_functions == {"total": 1, "load": 2}


def test_candidate_tests() -> None:
    assert candidate_tests("app/payments.py", [t for t in TREE if is_test_file(t)]) == [
        "tests/test_payments.py",
        "web/src/payments.test.ts",
    ]
    assert candidate_tests("app/__init__.py", ["tests/test_app.py"]) == ["tests/test_app.py"]


@pytest.fixture
def handler(settings: Settings) -> TestReviewHandler:
    settings.github_api_url = GH
    settings.openai_model = "test-model"
    return TestReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
    )


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d5")


@respx.mock
async def test_maps_changes_to_tests_and_posts_comment_only_review(handler, event) -> None:
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    tree = respx.get(url__regex=rf"{REPO}/git/trees/.*").mock(
        return_value=httpx.Response(200, json={"tree": [{"path": p, "type": "blob"} for p in TREE]})
    )
    respx.get(f"{REPO}/contents/tests/test_payments.py").mock(
        return_value=httpx.Response(200, text="def test_charge():\n    assert charge(1)\n")
    )
    review = {
        "summary": "`refund` has no test.",
        "comments": [
            {"path": "app/payments.py", "line": 21, "severity": "high",
             "category": "missing-test", "body": "Add `test_refund_partial_amount`."},
        ],
    }  # fmt: skip
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        side_effect=[
            completion(("read_file", {"path": "tests/test_payments.py"})),
            completion(("submit_test_review", review)),
        ]
    )
    reviews = respx.post(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    await handler.handle(event)

    assert tree.calls[0].request.url.params["recursive"] == "1"
    prompt = json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    test_map = json.loads(prompt.split("<test_map>")[1].split("</test_map>")[0])
    [entry] = test_map["changed_source_files"]
    assert entry["added_functions"] == [{"name": "refund", "line": 21}]
    assert entry["modified_functions"] == [{"name": "charge", "first_changed_line": 12}]
    assert entry["candidate_tests"] == ["tests/test_payments.py", "web/src/payments.test.ts"]
    assert test_map["test_files_changed_in_pr"] == [] and test_map["test_files_in_repo"] == 3
    posted = json.loads(reviews.calls.last.request.content)
    assert posted["event"] == "COMMENT" and posted["body"].startswith(HEADER)
    assert [c["line"] for c in posted["comments"]] == [21]
    assert "no PR code executed: 2 changed function(s) in 1 source file(s)" in posted["body"]


@respx.mock
async def test_skips_prs_without_source_changes(handler, event) -> None:
    docs = DIFF[DIFF.index("diff --git a/README.md") :]
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=docs))
    llm = respx.post(f"{OPENAI}/chat/completions")
    await handler.handle(event)
    assert not llm.called
