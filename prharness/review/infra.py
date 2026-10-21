"""Deterministic CI/CD and infrastructure-as-code checks for ``infra_review``.

These complement Semgrep's GitHub Actions / Dockerfile / Terraform / Kubernetes packs with
line-level checks for the highest-impact misconfigurations, so the agent still gets concrete
leads when Semgrep is unavailable or its rules don't match."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import PurePosixPath

from .scanners import ScanFinding

SOURCE = "infra-scan"

_K8S = re.compile(r"^apiVersion:\s*\S+.*^kind:\s*\S+|^kind:\s*\S+.*^apiVersion:", re.M | re.S)
_GHA_PRIVILEGED_TRIGGER = re.compile(r"\b(?:pull_request_target|workflow_run)\b")
_GHA_PR_HEAD_REF = re.compile(
    r"\bref:\s*\$\{\{\s*(?:github\.event\.pull_request\.head\.(?:sha|ref)|github\.head_ref"
    r"|github\.event\.workflow_run\.head_(?:sha|branch))"
)
_GHA_UNTRUSTED_EXPR = re.compile(
    r"\$\{\{\s*(?:github\.head_ref|github\.event\.(?:issue|pull_request|comment|review"
    r"|review_comment|discussion|discussion_comment|pages|head_commit|commits|workflow_run)"
    r"[^}]*?(?:title|body|message|name|label|ref|email|default_branch|head_branch))[^}]*\}\}"
)
_GHA_SCRIPT_KEY = re.compile(r"^(\s*)(?:-\s+)?(run|script):\s*(.*)$")
_GHA_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?([^'\"\s@]+)@([^'\"\s#]+)")
_GHA_WRITE_PERMISSION = re.compile(r"^\s*[a-z-]+:\s*write\b")
_GHA_ON = re.compile(r"^['\"]?on['\"]?\s*:")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SECRET_NAME = re.compile(r"(?i)(?:key|secret|token|passw(?:or)?d|pwd|credentials?)$")
_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.I)
_ENV_ARG = re.compile(r"^\s*(ENV|ARG)\s+([A-Za-z_][A-Za-z0-9_]*)[=\s]+(\S+)", re.I)
_PIPE_TO_SHELL = re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")
_ADD_REMOTE = re.compile(r"^\s*ADD\s+(?:--\S+\s+)*https?://", re.I)
_USER = re.compile(r"^\s*USER\s+(\S+)", re.I)

Rule = tuple[str, re.Pattern[str], str, str, str]  # id, pattern, severity, cwe, message
_LINE_RULES: dict[str, tuple[Rule, ...]] = {
    "terraform": (
        ("tf-open-cidr", re.compile(r"""["'](?:0\.0\.0\.0/0|::/0)["']"""), "medium", "CWE-284",
         "Rule open to the whole internet (0.0.0.0/0 or ::/0)."),
        ("tf-public-acl", re.compile(r"""\bacl\s*=\s*["']public-read(?:-write)?["']"""), "high",
         "CWE-732", "Bucket ACL grants public access."),
        ("tf-encryption-disabled",
         re.compile(r"\b(?:encrypted|storage_encrypted|enable_encryption)\s*=\s*false\b"),
         "medium", "CWE-311", "Encryption at rest is disabled."),
        ("tf-public-access", re.compile(r"\bpublicly_accessible\s*=\s*true\b"), "high", "CWE-284",
         "Resource is publicly accessible."),
    ),
    "kubernetes": (
        ("k8s-privileged", re.compile(r"^\s*privileged:\s*true\b"), "high", "CWE-250",
         "Privileged container: full access to the host."),
        ("k8s-host-namespace", re.compile(r"^\s*host(?:Network|PID|IPC):\s*true\b"), "high",
         "CWE-250", "Pod shares a host namespace."),
        ("k8s-privilege-escalation", re.compile(r"^\s*allowPrivilegeEscalation:\s*true\b"),
         "medium", "CWE-250", "Container allows privilege escalation."),
        ("k8s-run-as-root", re.compile(r"^\s*runAsUser:\s*0\b"), "medium", "CWE-250",
         "Container runs as root (UID 0)."),
    ),
}  # fmt: skip
_LINE_RULES["compose"] = _LINE_RULES["kubernetes"][:1]


def infra_kind(path: str, content: str = "") -> str | None:
    """Classify a file as workflow, dockerfile, compose, terraform or kubernetes (else None)."""
    p = PurePosixPath(path)
    name = p.name.lower()
    yaml = name.endswith((".yml", ".yaml"))
    if yaml and p.parent.as_posix().endswith(".github/workflows"):
        return "workflow"
    if name in ("action.yml", "action.yaml"):
        return "workflow"
    if (
        name in ("dockerfile", "containerfile")
        or name.startswith("dockerfile.")
        or (name.endswith(".dockerfile"))
    ):
        return "dockerfile"
    if yaml and re.match(r"(?:docker-)?compose\b", name):
        return "compose"
    if name.endswith((".tf", ".hcl")):
        return "terraform"
    if yaml and _K8S.search(content):
        return "kubernetes"
    return None


def is_infra_candidate(path: str) -> bool:
    """Whether a file might be infra (YAML needs its content to tell)."""
    name = PurePosixPath(path).name.lower()
    return name.endswith((".yml", ".yaml")) or infra_kind(path) is not None


def scan_infra(files: dict[str, str], added: dict[str, set[int]]) -> list[ScanFinding]:
    """Run the built-in checks on each infra file, keeping hits on lines the PR adds."""
    findings = []
    for path, content in files.items():
        kind = infra_kind(path, content)
        checker = _CHECKERS.get(kind or "", _line_rules)
        lines = content.splitlines()
        for line, rule, severity, cwe, message in checker(kind or "", lines):
            if line in added.get(path, ()):
                findings.append(ScanFinding(SOURCE, rule, path, line, severity, message, cwe))
    return findings


Hit = tuple[int, str, str, str, str]  # line, rule, severity, cwe, message


def _line_rules(kind: str, lines: list[str]) -> Iterator[Hit]:
    for i, text in enumerate(lines, 1):
        for rule, pattern, severity, cwe, message in _LINE_RULES.get(kind, ()):
            if pattern.search(text):
                yield i, rule, severity, cwe, message


def _workflow(_: str, lines: list[str]) -> Iterator[Hit]:
    code = "\n".join(t.split(" #", 1)[0] for t in lines if not t.lstrip().startswith("#"))
    privileged = bool(_GHA_PRIVILEGED_TRIGGER.search(code))
    has_permissions = re.search(r"^\s*permissions:", code, re.M) is not None
    script_indent: int | None = None
    for i, text in enumerate(lines, 1):
        stripped = text.lstrip()
        if stripped.startswith("#"):
            continue
        indent = len(text) - len(stripped)
        if script_indent is not None and stripped and indent <= script_indent:
            script_indent = None
        if m := _GHA_SCRIPT_KEY.match(text):
            script_indent = len(m.group(1))
            body = m.group(3)
        else:
            body = text if script_indent is not None else ""
        if body and _GHA_UNTRUSTED_EXPR.search(body):
            yield (
                i,
                "gha-script-injection",
                "high",
                "CWE-78",
                (
                    "Attacker-controlled `${{ github.* }}` expression expanded inside a script; "
                    "pass it through `env:` and quote it instead."
                ),
            )
        if privileged and _GHA_PR_HEAD_REF.search(text):
            yield (
                i,
                "gha-pwn-request",
                "critical",
                "CWE-829",
                (
                    "Checks out untrusted PR code in a `pull_request_target`/`workflow_run` "
                    "workflow, which runs with secrets and a write token."
                ),
            )
        if re.match(r"^\s*permissions:\s*write-all\b", text):
            yield i, "gha-write-all", "high", "CWE-250", "`permissions: write-all` on GITHUB_TOKEN."
        elif privileged and _GHA_WRITE_PERMISSION.match(text):
            yield (
                i,
                "gha-privileged-write",
                "medium",
                "CWE-250",
                ("Write-scoped GITHUB_TOKEN in a workflow triggered by untrusted events."),
            )
        if (m := _GHA_USES.match(text)) and not m.group(1).startswith(("./", "docker://")):
            if not _SHA.match(m.group(2)):
                first_party = m.group(1).split("/")[0] in ("actions", "github")
                yield (
                    i,
                    "gha-unpinned-action",
                    "low" if first_party else "medium",
                    "CWE-1357",
                    (f"`{m.group(1)}@{m.group(2)}` is a mutable ref; pin to a full commit SHA."),
                )
        if not has_permissions and _GHA_ON.match(text):
            yield (
                i,
                "gha-no-permissions",
                "low",
                "CWE-250",
                (
                    "No `permissions:` block; GITHUB_TOKEN gets the repository default, which "
                    "may be read-write."
                ),
            )


def _dockerfile(_: str, lines: list[str]) -> Iterator[Hit]:
    last_from, user = None, None
    for i, text in enumerate(lines, 1):
        if m := _FROM.match(text):
            last_from, user = i, None
            image = m.group(1)
            tag = image.rsplit("/", 1)[-1]
            if (
                "@sha256:" not in image
                and image.lower() != "scratch"
                and (":" not in tag or tag.endswith(":latest"))
            ):
                yield (
                    i,
                    "docker-unpinned-base",
                    "low",
                    "CWE-1357",
                    (f"Base image `{image}` is not pinned to a version or digest."),
                )
        if (m := _ENV_ARG.match(text)) and _SECRET_NAME.search(m.group(2)):
            if not m.group(3).startswith("$"):
                yield (
                    i,
                    "docker-secret-in-image",
                    "high",
                    "CWE-798",
                    (
                        f"{m.group(1).upper()} `{m.group(2)}` bakes a secret into the image layers "
                        "and history; use build secrets or runtime env instead."
                    ),
                )
        if _PIPE_TO_SHELL.search(text):
            yield (
                i,
                "docker-pipe-to-shell",
                "medium",
                "CWE-494",
                ("Downloads and executes a script without integrity verification."),
            )
        if _ADD_REMOTE.match(text):
            yield (
                i,
                "docker-add-remote",
                "medium",
                "CWE-494",
                ("`ADD <url>` fetches remote content without a checksum."),
            )
        if m := _USER.match(text):
            user = m.group(1)
    if last_from is not None and (user is None or user.split(":")[0] in ("root", "0")):
        yield (
            last_from,
            "docker-runs-as-root",
            "medium",
            "CWE-250",
            ("The final stage runs as root; add a non-root `USER`."),
        )


_CHECKERS: dict[str, Callable[[str, list[str]], Iterator[Hit]]] = {
    "workflow": _workflow,
    "dockerfile": _dockerfile,
}
