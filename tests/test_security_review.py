import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
import respx
from openai import AsyncOpenAI

from prharness import Handler, PullRequestEvent
from prharness.config import Settings
from prharness.dispatcher import Dispatcher
from prharness.handlers.security_review import HEADER, SecurityReviewHandler, attribute_sources
from prharness.handlers.sequence import SequentialHandler
from prharness.registry import build_handlers
from prharness.review.agent import ReviewComment, ReviewResult
from prharness.review.diff import added_lines
from prharness.review.github import GitHubClient
from prharness.review.scanners import (
    ScanFinding,
    ScannerUnavailableError,
    SemgrepScanner,
    parse_semgrep,
    safe_target,
    scan_secrets,
)
from tests.test_code_review import GH, OPENAI, completion

FAKE_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
DIFF = f"""\
diff --git a/cmd.py b/cmd.py
--- a/cmd.py
+++ b/cmd.py
@@ -1,2 +1,4 @@
 import subprocess
+KEY = "{FAKE_KEY}"
+def run(cmd):
+    return subprocess.call(cmd, shell=True)
"""
CMD_PY = (
    f'import subprocess\nKEY = "{FAKE_KEY}"\n'
    "def run(cmd):\n    return subprocess.call(cmd, shell=True)\n"
)
SEMGREP_HIT = ScanFinding(
    source="semgrep",
    rule="python.lang.security.audit.subprocess-shell-true",
    path="cmd.py",
    line=4,
    severity="high",
    message="subprocess with shell=True",
    cwe="CWE-78",
)
OLD_LINE_HIT = ScanFinding("semgrep", "r", "cmd.py", 1, "low", "pre-existing line")
SECURITY_REVIEW = {
    "summary": "Command injection via `run`. 0 scanner findings dropped.",
    "comments": [
        {
            "path": "cmd.py",
            "line": 4,
            "severity": "critical",
            "category": "injection",
            "cwe": "CWE-78",
            "source": "semgrep",
            "body": "Attacker-controlled `cmd` reaches a shell. Pass an argv list.",
        },
        {
            "path": "cmd.py",
            "line": 2,
            "severity": "critical",
            "category": "hardcoded-secret",
            "cwe": "CWE-798",
            "source": "secret-scan",
            "body": "Rotate the AWS key and load it from the environment.",
        },
    ],
}


class FakeSemgrep:
    label = "p/test"

    def __init__(self, results=(), error: Exception | None = None) -> None:
        self.results = list(results)
        self.error = error
        self.files: dict[str, str] | None = None

    async def scan(self, files: dict[str, str]) -> list[ScanFinding]:
        self.files = files
        if self.error:
            raise self.error
        return self.results


@pytest.fixture
def review_settings(settings: Settings) -> Settings:
    settings.github_api_url = GH
    settings.openai_model = "test-model"
    return settings


@pytest.fixture
def event(sample_payload) -> PullRequestEvent:
    return PullRequestEvent.from_payload(sample_payload, "d1")


def make_handler(settings: Settings, semgrep: FakeSemgrep) -> SecurityReviewHandler:
    return SecurityReviewHandler(
        settings,
        github=GitHubClient("gh-token", GH),
        openai_client=AsyncOpenAI(
            api_key="sk-test", base_url=OPENAI, http_client=httpx.AsyncClient()
        ),
        semgrep=semgrep,
    )


def mock_github():
    repo = f"{GH}/repos/octo-org/octo-repo"
    respx.get(f"{repo}/pulls/42").mock(return_value=httpx.Response(200, text=DIFF))
    respx.get(f"{repo}/contents/cmd.py").mock(return_value=httpx.Response(200, text=CMD_PY))
    return respx.post(f"{repo}/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )


def test_added_lines() -> None:
    assert [(p, n) for p, n, _ in added_lines(DIFF)] == [
        ("cmd.py", 2),
        ("cmd.py", 3),
        ("cmd.py", 4),
    ]


def test_scan_secrets_never_echoes_value() -> None:
    [finding] = scan_secrets(DIFF)
    assert (finding.path, finding.line, finding.rule) == ("cmd.py", 2, "aws-access-key-id")
    assert finding.severity == "critical" and finding.cwe == "CWE-798"
    assert FAKE_KEY not in json.dumps(finding.__dict__)


def test_parse_semgrep() -> None:
    output = {
        "results": [
            {
                "check_id": "rule.a",
                "path": "cmd.py",
                "start": {"line": 4},
                "extra": {
                    "severity": "ERROR",
                    "message": " shell=True ",
                    "metadata": {"cwe": ["CWE-78: OS Command Injection"]},
                },
            },
            {"check_id": "broken"},
        ]
    }
    assert parse_semgrep(json.dumps(output)) == [
        ScanFinding("semgrep", "rule.a", "cmd.py", 4, "high", "shell=True", "CWE-78")
    ]


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "a/../../b.py", ""])
def test_safe_target_rejects_escaping_paths(tmp_path: Path, path: str) -> None:
    assert safe_target(tmp_path, path) is None


def test_safe_target_keeps_nested_paths(tmp_path: Path) -> None:
    assert safe_target(tmp_path, "src/app.py") == tmp_path / "src/app.py"


@respx.mock
async def test_triages_scanner_hits_and_posts_comment_only_review(review_settings, event) -> None:
    reviews = mock_github()
    semgrep = FakeSemgrep([SEMGREP_HIT, OLD_LINE_HIT])
    llm = respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_security_review", SECURITY_REVIEW))
    )

    await make_handler(review_settings, semgrep).handle(event)

    assert semgrep.files == {"cmd.py": CMD_PY}
    request = json.loads(llm.calls[0].request.content)
    assert "application security" in request["messages"][0]["content"]
    user = request["messages"][1]["content"]
    context = json.loads(user.split("<scanner_findings>")[1].split("</scanner_findings>")[0])
    assert [(f["source"], f["line"]) for f in context] == [("secret-scan", 2), ("semgrep", 4)]
    assert [t["function"]["name"] for t in request["tools"]] == [
        "read_file",
        "submit_security_review",
    ]

    posted = json.loads(reviews.calls.last.request.content)
    assert posted["event"] == "COMMENT"
    assert posted["body"].startswith(HEADER)
    assert "Semgrep (`p/test`): 1 hit(s) on added lines across 1 file(s)." in posted["body"]
    assert "Secret scan: 1 hit(s)" in posted["body"]
    assert [c["line"] for c in posted["comments"]] == [4, 2]
    assert posted["comments"][0]["body"].startswith(
        "**[critical] injection** (CWE-78, source: semgrep)\n\n"
    )
    github_writes = [
        (c.request.method, c.request.url.path)
        for c in respx.calls
        if c.request.url.host == "api.github.test" and c.request.method != "GET"
    ]
    assert github_writes == [("POST", "/repos/octo-org/octo-repo/pulls/42/reviews")]


@respx.mock
async def test_semgrep_unavailable_falls_back_to_llm(review_settings, event) -> None:
    reviews = mock_github()
    semgrep = FakeSemgrep(error=ScannerUnavailableError("missing"))
    respx.post(f"{OPENAI}/chat/completions").mock(
        return_value=completion(("submit_security_review", {"summary": "ok", "comments": []}))
    )

    await make_handler(review_settings, semgrep).handle(event)

    body = json.loads(reviews.calls.last.request.content)["body"]
    assert "Semgrep: did not run (ScannerUnavailableError); LLM review only." in body


def test_security_review_requires_secrets(settings: Settings) -> None:
    settings.handlers = "security_review"
    with pytest.raises(ValueError, match="security_review handler requires OPENAI_API_KEY"):
        build_handlers(settings)


class OrderHandler(Handler):
    def __init__(self, settings: Settings, name: str, log: list[str], fail: bool = False):
        super().__init__(settings)
        self.name, self.log, self.fail = name, log, fail

    async def handle(self, event: PullRequestEvent) -> None:
        self.log.append(self.name)
        if self.fail:
            raise RuntimeError("boom")


async def test_sequence_runs_in_order_and_continues_after_failure(settings, event) -> None:
    calls: list[str] = []
    chain = SequentialHandler(
        settings,
        [
            OrderHandler(settings, "first", calls, fail=True),
            OrderHandler(settings, "second", calls),
        ],
    )
    [result] = await Dispatcher([chain], timeout_seconds=1).dispatch(event)
    assert calls == ["first", "second"]
    assert result.handler == "first>second" and not result.ok and "first" in result.error


def test_registry_builds_sequences(settings: Settings) -> None:
    settings.handlers = "log, log>log"
    single, chain = build_handlers(settings)
    assert single.name == "log"
    assert isinstance(chain, SequentialHandler) and chain.name == "log>log"
    assert chain.timeout_seconds == 2 * settings.handler_timeout_seconds


SEMGREP_BIN = os.environ.get("SEMGREP_BIN") or shutil.which("semgrep")
RULE = """\
rules:
  - id: shell-true
    languages: [python]
    severity: ERROR
    message: subprocess with shell=True
    metadata: {cwe: ["CWE-78: OS Command Injection"]}
    pattern: subprocess.call(..., shell=True)
"""


@pytest.mark.skipif(SEMGREP_BIN is None, reason="semgrep not installed")
async def test_real_semgrep_scan(tmp_path: Path) -> None:
    rule = tmp_path / "rule.yml"
    rule.write_text(RULE)
    scanner = SemgrepScanner(SEMGREP_BIN, str(rule), timeout_seconds=120)
    findings = await scanner.scan({"pkg/cmd.py": CMD_PY, "../evil.py": CMD_PY})
    assert [(f.path, f.line, f.cwe, f.severity) for f in findings] == [
        ("pkg/cmd.py", 4, "CWE-78", "high")
    ]


def test_attribute_sources_relabels_unbacked_scanner_claims() -> None:
    findings = [
        ScanFinding("semgrep", "nan-injection", "app.py", 64, "medium", "nan", "CWE-704"),
        ScanFinding("semgrep", "sqli", "app.py", 22, "high", "sqli", "CWE-89"),
        ScanFinding("secret-scan", "aws", "app.py", 15, "critical", "aws", "CWE-798"),
    ]

    def c(line: int, source: str, cwe: str) -> ReviewComment:
        return ReviewComment("app.py", line, "high", "x", "b", cwe=cwe, source=source)

    result = attribute_sources(
        ReviewResult(
            "s",
            [
                c(64, "semgrep", "CWE-362"),  # same line, different vulnerability
                c(22, "semgrep", "CWE-89"),
                c(15, "secret-scan", "CWE-798"),
                c(30, "semgrep", "CWE-78"),  # no scanner hit on this line
                c(15, "semgrep", "CWE-798"),  # wrong scanner
                c(50, "llm", "CWE-330"),
            ],
        ),
        findings,
    )
    assert [x.source for x in result.comments] == [
        "llm", "semgrep", "secret-scan", "llm", "llm", "llm",
    ]
