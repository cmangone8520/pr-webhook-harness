import json

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.compat_review import HEADER, CompatReviewHandler
from prharness.review.compat import compat_signals
from prharness.review.github import GitHubClient
from tests.test_code_review import GH, OPENAI, completion

REPO = f"{GH}/repos/octo-org/octo-repo"
DIFF = """\
diff --git a/app/api.py b/app/api.py
--- a/app/api.py
+++ b/app/api.py
@@ -1,12 +1,10 @@
 import os
-def get_user(user_id):
+def get_user(user_id, tenant):
     return db.find(user_id)
-def delete_user(user_id):
-    db.delete(user_id)
-def _helper():
-    pass
-@app.get("/v1/users/{user_id}")
+@app.get("/v2/users/{user_id}")
 def read(user_id):
-    key = os.getenv("LEGACY_KEY")
+    region = os.environ["DEPLOY_REGION"]
+    url = os.getenv("DATABASE_URL")
     return get_user(user_id)
diff --git a/migrations/0002_users.sql b/migrations/0002_users.sql
--- /dev/null
+++ b/migrations/0002_users.sql
@@ -0,0 +1,3 @@
+ALTER TABLE users DROP COLUMN email;
+ALTER TABLE users ADD COLUMN tenant_id integer NOT NULL;
+CREATE INDEX idx_users_tenant ON users (tenant_id);
"""


def test_compat_signals() -> None:
    signals = {(s.kind, s.path, s.line, s.side, s.detail) for s in
               compat_signals(DIFF, docs="DATABASE_URL=postgres://...")}  # fmt: skip
    assert signals == {
        ("signature-changed", "app/api.py", 2, "new",
         "def get_user(user_id):  ->  def get_user(user_id, tenant):"),
        ("removed-public-symbol", "app/api.py", 4, "old", "def delete_user(user_id):"),
        ("removed-endpoint", "app/api.py", 8, "old", "GET /v1/users/{user_id}"),
        ("new-endpoint", "app/api.py", 4, "new", "GET /v2/users/{user_id}"),
        ("undocumented-env-var", "app/api.py", 6, "new", "DEPLOY_REGION"),
        ("new-env-var", "app/api.py", 7, "new", "DATABASE_URL"),
        ("removed-env-var", "app/api.py", 10, "old", "LEGACY_KEY"),
        ("risky-migration", "migrations/0002_users.sql", 1, "new",
         "ALTER TABLE users DROP COLUMN email;"),
        ("risky-migration", "migrations/0002_users.sql", 2, "new",
         "ALTER TABLE users ADD COLUMN tenant_id integer NOT NULL;"),
        ("risky-migration", "migrations/0002_users.sql", 3, "new",
         "CREATE INDEX idx_users_tenant ON users (tenant_id);"),
    }  # fmt: skip


def test_safe_changes_produce_no_signals() -> None:
    diff = """\
diff --git a/migrations/0003.sql b/migrations/0003.sql
--- /dev/null
+++ b/migrations/0003.sql
@@ -0,0 +1,2 @@
+ALTER TABLE users ADD COLUMN nickname text NOT NULL DEFAULT '';
+CREATE INDEX CONCURRENTLY idx_nick ON users (nickname);
"""
    assert compat_signals(diff) == []


@pytest.fixture
def handler(settings: Settings) -> CompatReviewHandler:
    settings.github_api_url = GH
    settings.openai_model = "test-model"
    return CompatReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
    )


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d6")


@respx.mock
async def test_reviews_breaking_changes_comment_only(handler, event) -> None:
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(f"{REPO}/contents/.env.example").mock(
        return_value=httpx.Response(200, text="DATABASE_URL=\n")
    )
    respx.get(url__regex=rf"{REPO}/contents/.*").mock(return_value=httpx.Response(404))
    review = {
        "summary": "Removes v1 endpoint and drops a column.",
        "comments": [
            {"path": "migrations/0002_users.sql", "line": 1, "severity": "high",
             "category": "unsafe-migration", "body": "Expand/contract instead."},
            {"path": "app/api.py", "line": 20, "severity": "high",
             "category": "endpoint-contract", "body": "Keep `/v1` as an alias."},
        ],
    }  # fmt: skip
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_compat_review", review))
    )
    reviews = respx.post(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    await handler.handle(event)

    prompt = json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    leads = json.loads(prompt.split("<compat_signals>")[1].split("</compat_signals>")[0])
    kinds = {(s["kind"], s["detail"]) for s in leads}
    assert ("undocumented-env-var", "DEPLOY_REGION") in kinds
    assert ("new-env-var", "DATABASE_URL") in kinds
    posted = json.loads(reviews.calls.last.request.content)
    assert posted["event"] == "COMMENT" and posted["body"].startswith(HEADER)
    assert [(c["path"], c["line"]) for c in posted["comments"]] == [
        ("migrations/0002_users.sql", 1)
    ]
    assert "Keep `/v1` as an alias." in posted["body"]  # old-side line goes in the summary
    assert "Breaking-change leads: 10" in posted["body"]


@respx.mock
async def test_skips_docs_only_prs(handler, event) -> None:
    docs = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
    docs += "@@ -1 +1,2 @@\n x\n+y\n"
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=docs))
    llm = respx.post(f"{OPENAI}/chat/completions")
    await handler.handle(event)
    assert not llm.called
