import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.infra_review import HEADER, InfraReviewHandler
from prharness.review.github import GitHubClient
from prharness.review.infra import infra_kind, scan_infra
from prharness.review.scanners import ScanFinding, SemgrepScanner
from tests.test_code_review import DIFF as CODE_DIFF
from tests.test_code_review import GH, OPENAI, completion
from tests.test_security_review import FakeSemgrep

REPO = f"{GH}/repos/octo-org/octo-repo"
FIXTURES = Path(__file__).parent / "fixtures" / "infra"
FILES = {
    p.relative_to(FIXTURES).as_posix(): p.read_text() for p in FIXTURES.rglob("*") if p.is_file()
}
ALL_LINES = {path: set(range(1, 100)) for path in FILES}


def new_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    header = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n"
    return f"{header}@@ -0,0 +1,{len(lines)} @@\n{body}"


def rules(path: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for f in scan_infra({path: FILES[path]}, ALL_LINES):
        out.setdefault(f.rule, []).append(f.line)
    return out


def test_infra_kind() -> None:
    assert infra_kind(".github/workflows/ci.yml") == "workflow"
    assert infra_kind("sub/action.yaml") == "workflow"
    assert infra_kind("docker/Dockerfile.prod") == "dockerfile"
    assert infra_kind("docker-compose.override.yml") == "compose"
    assert infra_kind("infra/main.tf") == "terraform"
    assert infra_kind("k8s/deploy.yaml", FILES["k8s/deploy.yaml"]) == "kubernetes"
    assert infra_kind("config/settings.yaml", "debug: true\n") is None


def test_workflow_checks() -> None:
    assert rules(".github/workflows/pr.yml") == {
        "gha-write-all": [5],
        "gha-unpinned-action": [10, 13],
        "gha-pwn-request": [12],
        "gha-script-injection": [16],
    }
    safe = "on: push\njobs:\n  a:\n    steps:\n      - run: echo ${{ github.sha }}\n"
    found = scan_infra({".github/workflows/a.yml": safe}, {".github/workflows/a.yml": {1, 5}})
    assert [f.rule for f in found] == ["gha-no-permissions"]


def test_dockerfile_terraform_kubernetes_checks() -> None:
    assert rules("Dockerfile") == {
        "docker-unpinned-base": [1],
        "docker-runs-as-root": [1],
        "docker-secret-in-image": [2],
        "docker-add-remote": [3],
        "docker-pipe-to-shell": [4],
    }
    assert rules("tf/main.tf") == {"tf-open-cidr": [6], "tf-public-acl": [11]}
    assert rules("k8s/deploy.yaml") == {
        "k8s-host-namespace": [8],
        "k8s-privileged": [13],
        "k8s-privilege-escalation": [14],
    }


def test_only_added_lines_are_reported() -> None:
    assert scan_infra({"Dockerfile": FILES["Dockerfile"]}, {"Dockerfile": {4}}) == [
        ScanFinding(
            "infra-scan",
            "docker-pipe-to-shell",
            "Dockerfile",
            4,
            "medium",
            "Downloads and executes a script without integrity verification.",
            "CWE-494",
        )
    ]


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d4")


def make_handler(settings: Settings, semgrep) -> InfraReviewHandler:
    settings.github_api_url = GH
    settings.security_model = "test-model"
    return InfraReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(api_key="k", base_url=OPENAI, http_client=httpx.AsyncClient()),
        semgrep=semgrep,
    )


@respx.mock
async def test_reviews_infra_files_and_posts_comment_only_review(settings, event) -> None:
    diff = new_file_diff("Dockerfile", FILES["Dockerfile"]) + new_file_diff("notes.yaml", "a: 1\n")
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=diff))
    respx.get(f"{REPO}/contents/Dockerfile").mock(
        return_value=httpx.Response(200, text=FILES["Dockerfile"])
    )
    respx.get(f"{REPO}/contents/notes.yaml").mock(return_value=httpx.Response(200, text="a: 1\n"))
    semgrep = FakeSemgrep(
        [
            ScanFinding("semgrep", "missing-user", "Dockerfile", 6, "medium", "root", "CWE-250"),
            ScanFinding("semgrep", "other", "Dockerfile", 40, "low", "not added", ""),
        ]
    )
    review = {
        "summary": "Container runs as root and bakes a token into the image.",
        "comments": [
            {"path": "Dockerfile", "line": 2, "severity": "high", "category": "secrets",
             "cwe": "CWE-798", "source": "infra-scan", "body": "Use a build secret."},
            {"path": "Dockerfile", "line": 6, "severity": "medium",
             "category": "container-hardening", "cwe": "CWE-250", "source": "semgrep",
             "body": "Add `USER app`."},
        ],
    }  # fmt: skip
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_infra_review", review))
    )
    reviews = respx.post(f"{REPO}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    await make_handler(settings, semgrep).handle(event)

    assert set(semgrep.files) == {"Dockerfile"}
    prompt = json.loads(llm.calls[0].request.content)["messages"][1]["content"]
    assert "docker-secret-in-image" in prompt and "missing-user" in prompt
    assert '"line": 40' not in prompt
    posted = json.loads(reviews.calls.last.request.content)
    assert posted["event"] == "COMMENT" and posted["body"].startswith(HEADER)
    assert [(c["line"], "source: " + s in c["body"]) for c, s in
            zip(posted["comments"], ["infra-scan", "semgrep"], strict=True)] == [
        (2, True), (6, True)
    ]  # fmt: skip
    assert "Built-in CI/IaC checks: 5 hit(s)" in posted["body"]


@respx.mock
async def test_skips_prs_without_infra_files(settings, event) -> None:
    respx.get(f"{REPO}/pulls/42").mock(return_value=httpx.Response(200, text=CODE_DIFF))
    llm = respx.post(f"{OPENAI}/chat/completions")
    await make_handler(settings, FakeSemgrep()).handle(event)
    assert not llm.called


SEMGREP_BIN = os.environ.get("SEMGREP_BIN") or shutil.which("semgrep")


@pytest.mark.skipif(not SEMGREP_BIN, reason="semgrep not installed")
async def test_default_semgrep_packs_flag_workflow_issues(settings: Settings) -> None:
    scanner = SemgrepScanner(SEMGREP_BIN, settings.infra_semgrep_config, 180)
    path = ".github/workflows/pr.yml"
    found = {f.rule.rsplit(".", 1)[-1] for f in await scanner.scan({path: FILES[path]})}
    assert {"pull-request-target-code-checkout", "run-shell-injection"} <= found
