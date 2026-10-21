import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.dependency_review import HEADER, DependencyReviewHandler
from prharness.review.deps import (
    DependencyChange,
    DependencyScanner,
    changed_dependencies,
    parse_manifest,
    possible_typosquat,
)
from prharness.review.github import GitHubClient
from tests.test_code_review import DIFF as CODE_DIFF
from tests.test_code_review import GH, OPENAI, completion

REPO = f"{GH}/repos/octo-org/octo-repo"
BASE, HEAD = "1" * 40, "2" * 40
DIFF = """\
diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,2 +1,3 @@
 flask==2.0.0
-requests==2.31.0
+requests==2.19.0
+reqeusts>=1.0
"""
FILES = {
    BASE: "flask==2.0.0\nrequests==2.31.0\n",
    HEAD: "flask==2.0.0\nrequests==2.19.0\nreqeusts>=1.0\n",
}
VULN = {
    "id": "GHSA-x84v-xcm2-53pg",
    "summary": "Insufficiently Protected Credentials in Requests",
    "aliases": ["CVE-2018-18074"],
    "database_specific": {"severity": "HIGH", "cwe_ids": ["CWE-522"]},
    "affected": [
        {
            "package": {"name": "requests", "ecosystem": "PyPI"},
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.20.0"}]}],
        }
    ],
}


def test_parse_manifests() -> None:
    reqs = parse_manifest("requirements-dev.txt", "Flask_Login==0.6.3  # auth\n-r base.txt\nrich\n")
    assert [(d.name, d.spec, d.version, d.line) for d in reqs] == [
        ("flask-login", "==0.6.3", "0.6.3", 1),
        ("rich", "", "", 3),
    ]
    pyproject = """\
[project]
dependencies = ["httpx>=0.27", "pydantic==2.8.2"]
[project.optional-dependencies]
dev = ["pytest>=8"]
[tool.poetry.dependencies]
python = "^3.11"
django = "4.2.1"
"""
    assert [(d.name, d.version, d.line) for d in parse_manifest("pyproject.toml", pyproject)] == [
        ("httpx", "", 2),
        ("pydantic", "2.8.2", 2),
        ("pytest", "", 4),
        ("django", "4.2.1", 7),
    ]
    pkg = '{\n  "dependencies": {\n    "lodash": "4.17.20",\n    "react": "^18.2.0"\n  }\n}'
    assert [(d.name, d.version, d.line) for d in parse_manifest("web/package.json", pkg)] == [
        ("lodash", "4.17.20", 3),
        ("react", "", 4),
    ]
    lock = json.dumps(
        {"packages": {"": {}, "node_modules/a/node_modules/@s/b": {"version": "1.0.0"}}}, indent=1
    )
    [b] = parse_manifest("package-lock.json", lock)
    assert (b.name, b.version, b.lockfile) == ("@s/b", "1.0.0", True)
    uv = '[[package]]\nname = "Jinja2"\nversion = "3.1.2"\n'
    assert [(d.name, d.version, d.line) for d in parse_manifest("uv.lock", uv)] == [
        ("jinja2", "3.1.2", 2)
    ]
    gomod = (
        "module x\n\nrequire github.com/a/b v1.2.3\n"
        "require (\n\tgolang.org/x/net v0.1.0 // indirect\n)\n"
    )
    assert [(d.name, d.version, d.line) for d in parse_manifest("go.mod", gomod)] == [
        ("github.com/a/b", "1.2.3", 3),
        ("golang.org/x/net", "0.1.0", 5),
    ]
    assert parse_manifest("package.json", "{not json") == []


def test_changed_dependencies_and_typosquats() -> None:
    base = parse_manifest("requirements.txt", FILES[BASE])
    head = parse_manifest("requirements.txt", FILES[HEAD])
    changes = changed_dependencies(base, head)
    assert [(c.dep.name, c.previous) for c in changes] == [
        ("requests", "==2.31.0"),
        ("reqeusts", None),
    ]
    assert possible_typosquat("PyPI", "reqeusts") == "requests"
    assert possible_typosquat("PyPI", "requests") is None
    assert possible_typosquat("npm", "totally-unrelated-name") is None


@respx.mock
async def test_npm_registry_flags() -> None:
    created = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    respx.get("https://npm.test/@evil%2Fpad").mock(
        return_value=httpx.Response(
            200,
            json={
                "dist-tags": {"latest": "1.0.0"},
                "time": {"created": created},
                "versions": {
                    "1.0.0": {
                        "deprecated": "use String.prototype.padStart",
                        "scripts": {"postinstall": "node steal.js"},
                        "license": "AGPL-3.0",
                    }
                },
            },
        )
    )
    respx.post("https://osv.test/v1/querybatch").mock(
        return_value=httpx.Response(200, json={"results": [{}]})
    )
    [dep] = parse_manifest("package.json", '{"dependencies": {"@evil/pad": "1.0.0"}}')
    async with httpx.AsyncClient() as client:
        scanner = DependencyScanner(client, osv_url="https://osv.test", npm_url="https://npm.test")
        findings, facts, notes = await scanner.scan([DependencyChange(dep, None)])
    assert sorted(f.rule for f in findings) == [
        "deprecated",
        "install-script",
        "license",
        "new-package",
    ]
    assert facts[0]["checked_version_basis"] == "exact pin" and notes == []


@pytest.fixture
def handler(settings: Settings) -> DependencyReviewHandler:
    settings.github_api_url = GH
    settings.security_model = "test-model"
    settings.osv_api_url = "https://osv.test"
    settings.pypi_url = "https://pypi.test"
    return DependencyReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
        http_client=httpx.AsyncClient(),
    )


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d3")


@respx.mock
async def test_checks_changed_packages_and_posts_comment_only_review(handler, event) -> None:
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(f"{REPO}/contents/requirements.txt").mock(
        side_effect=lambda r: httpx.Response(200, text=FILES[r.url.params["ref"]])
    )
    respx.get("https://pypi.test/pypi/requests/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "info": {"version": "2.32.3", "license": "Apache-2.0"},
                "releases": {"0.1": [{"upload_time_iso_8601": "2011-02-14T00:00:00Z"}]},
            },
        )
    )
    respx.get("https://pypi.test/pypi/reqeusts/json").mock(return_value=httpx.Response(404))
    osv = respx.post("https://osv.test/v1/querybatch").mock(
        return_value=httpx.Response(200, json={"results": [{"vulns": [{"id": VULN["id"]}]}, {}]})
    )
    respx.get(f"https://osv.test/v1/vulns/{VULN['id']}").mock(
        return_value=httpx.Response(200, json=VULN)
    )
    review = {
        "summary": "Downgrade to a vulnerable requests and a typosquat.",
        "comments": [
            {"path": "requirements.txt", "line": 2, "severity": "high",
             "category": "vulnerable-dependency", "cwe": "CWE-522", "source": "osv",
             "body": "Upgrade to >=2.20.0."},
            {"path": "requirements.txt", "line": 3, "severity": "high", "category": "typosquat",
             "cwe": "CWE-829", "source": "registry", "body": "Did you mean `requests`?"},
        ],
    }  # fmt: skip
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_dependency_review", review))
    )
    reviews = respx.post(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    await handler.handle(event)

    assert [q["version"] for q in json.loads(osv.calls[0].request.content)["queries"]] == [
        "2.19.0",
        "1.0",
    ]
    prompt = json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    for text in ("CVE-2018-18074", "Fixed in: 2.20.0", "package-not-found", "possible-typosquat"):
        assert text in prompt
    assert '"package": "flask"' not in prompt
    posted = json.loads(reviews.calls.last.request.content)
    assert posted["event"] == "COMMENT" and posted["body"].startswith(HEADER)
    assert [c["line"] for c in posted["comments"]] == [2, 3]
    assert "2 changed package(s)" in posted["body"] and "1 advisory hit(s)" in posted["body"]


@respx.mock
async def test_skips_prs_without_manifest_changes(handler, event) -> None:
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=CODE_DIFF))
    llm = respx.post(f"{OPENAI}/chat/completions")
    reviews = respx.post(f"{REPO}/pulls/42/reviews")
    await handler.handle(event)
    assert not llm.called and not reviews.called
