import json

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.hygiene_review import HEADER, HygieneReviewHandler
from prharness.review.github import GitHubClient
from prharness.review.hygiene import diff_stats, file_kind, linked_issues
from tests.test_code_review import GH, OPENAI, completion

REPO = f"{GH}/repos/octo-org/octo-repo"
DIFF = """\
diff --git a/app/payments.py b/app/payments.py
--- a/app/payments.py
+++ b/app/payments.py
@@ -1,2 +1,3 @@
 def charge():
-    pass
+    return 1
+
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-on: push
+on: [push, pull_request]
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1,3 @@
-{}
+{
+}
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""


def test_diff_stats_and_kinds() -> None:
    facts = diff_stats(DIFF)
    assert [(f.path, f.kind, f.added, f.deleted, f.binary) for f in facts.files] == [
        ("app/payments.py", "source", 2, 1, False),
        (".github/workflows/ci.yml", "ci-build", 1, 1, False),
        ("package-lock.json", "generated", 2, 1, False),
        ("logo.png", "other", 0, 0, True),
    ]
    assert (facts.added, facts.deleted) == (5, 3)
    assert file_kind("tests/test_x.py") == "test"
    assert file_kind("docs/guide.md") == "docs"
    assert file_kind("requirements.txt") == "dependencies"
    assert file_kind("db/migrations/0001.sql") == "migration"


def test_linked_issues() -> None:
    refs = linked_issues("Fixes #12. See octo/repo#7, PROJ-42 and https://x.test/o/r/issues/9")
    assert refs["closing"] == ["Fixes #12"]
    assert set(refs["references"]) == {
        "#12",
        "octo/repo#7",
        "PROJ-42",
        "https://x.test/o/r/issues/9",
    }
    assert linked_issues("no refs here") == {"closing": [], "references": []}


@pytest.fixture
def handler(settings: Settings) -> HygieneReviewHandler:
    settings.github_api_url = GH
    settings.openai_model = "test-model"
    settings.hygiene_max_changed_lines = 4
    return HygieneReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
    )


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d7")


@respx.mock
async def test_posts_summary_only_review(handler, event) -> None:
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(f"{REPO}/pulls/42/commits").mock(
        return_value=httpx.Response(
            200, json=[{"commit": {"message": "wip"}}, {"commit": {"message": "Add charge\n\nx"}}]
        )
    )
    review = {
        "summary": "Mixes a payments change with a CI change.",
        "comments": [
            {"path": "app/payments.py", "line": 3, "severity": "medium",
             "category": "unrelated-changes", "body": "Split the CI change out."},
            {"path": "", "line": 0, "severity": "low", "category": "missing-issue-link",
             "body": "Link an issue."},
        ],
    }  # fmt: skip
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_hygiene_review", review))
    )
    reviews = respx.post(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    await handler.handle(event)

    prompt = json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    facts = json.loads(prompt.split("<pr_facts>")[1].split("</pr_facts>")[0])
    assert facts["files_changed"] == 4 and facts["lines_changed_excluding_generated"] == 5
    assert facts["oversized"] is True
    assert facts["suspicious_commit_subjects"] == ["wip"]
    assert facts["commit_subjects"] == ["wip", "Add charge"]
    posted = json.loads(reviews.calls.last.request.content)
    assert posted["event"] == "COMMENT" and posted["body"].startswith(HEADER)
    assert posted["comments"] == []
    assert (
        "- `app/payments.py` **[medium] unrelated-changes**: Split the CI change out."
        in (posted["body"])
    )
    assert "- **[low] missing-issue-link**: Link an issue." in posted["body"]
    assert "4 file(s), +5/-3 lines, 2 commit(s)." in posted["body"]
