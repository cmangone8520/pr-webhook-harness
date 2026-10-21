"""Dependency manifest parsing plus OSV.dev / package-registry checks for ``dependency_review``."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from .scanners import ScanFinding

log = logging.getLogger("prharness.review.deps")

_PY_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;#]*)")
_EXACT_PY = re.compile(r"^===?\s*([0-9][^\s,*]*)$")
_EXACT_PLAIN = re.compile(r"^=?=?\s*v?(\d+(?:\.\d+)*(?:[-+.]?[0-9A-Za-z.+-]+)?)$")
_LOWER_BOUND = re.compile(r"(?:>=|~=|\^|~|>)\s*v?(\d[0-9A-Za-z.+-]*)")
_GO_REQUIRE_BLOCK = re.compile(r"^require\s*\($")
_OSV_SEVERITY = {"critical": "critical", "high": "high", "moderate": "medium", "medium": "medium"}
_COPYLEFT = re.compile(r"\b(?:A?GPL|SSPL)", re.IGNORECASE)
_LOCKFILES = {"package-lock.json", "poetry.lock", "uv.lock"}
_FETCH_CONCURRENCY = 8

POPULAR: dict[str, tuple[str, ...]] = {
    "PyPI": (
        "requests", "numpy", "pandas", "django", "flask", "fastapi", "urllib3", "boto3",
        "botocore", "setuptools", "pyyaml", "cryptography", "pillow", "jinja2", "sqlalchemy",
        "pydantic", "httpx", "aiohttp", "scipy", "matplotlib", "pytest", "click", "six",
        "python-dateutil", "certifi", "idna", "charset-normalizer", "attrs", "packaging",
        "wheel", "openai", "torch", "tensorflow", "scikit-learn", "beautifulsoup4", "lxml",
        "psycopg2", "redis", "celery", "gunicorn", "uvicorn", "werkzeug", "colorama", "rich",
        "tqdm", "paramiko", "pyjwt", "selenium", "pymongo", "protobuf",
    ),
    "npm": (
        "react", "react-dom", "lodash", "express", "axios", "chalk", "commander", "moment",
        "request", "debug", "typescript", "webpack", "vue", "next", "jquery", "underscore",
        "async", "uuid", "dotenv", "yargs", "body-parser", "cors", "mongoose", "eslint",
        "prettier", "jest", "mocha", "minimist", "glob", "rimraf", "semver", "ws", "socket.io",
        "node-fetch", "cross-env", "colors", "bluebird", "classnames", "redux", "rxjs",
    ),
    "Go": (
        "github.com/gin-gonic/gin", "github.com/stretchr/testify", "github.com/spf13/cobra",
        "github.com/sirupsen/logrus", "github.com/gorilla/mux", "golang.org/x/crypto",
        "github.com/google/uuid", "go.uber.org/zap", "github.com/pkg/errors", "gopkg.in/yaml.v3",
    ),
}  # fmt: skip


@dataclass(frozen=True)
class Dependency:
    ecosystem: str  # OSV ecosystem name: "PyPI", "npm" or "Go"
    name: str
    spec: str  # version constraint as written, e.g. "==2.0", "^1.2.0", "v1.4.0"
    version: str  # exact version if the spec pins one, else ""
    path: str
    line: int
    lockfile: bool = False


@dataclass(frozen=True)
class DependencyChange:
    dep: Dependency
    previous: str | None  # spec (or locked version) at the base commit; None = newly added


def manifest_kind(path: str) -> str | None:
    p = PurePosixPath(path)
    if re.fullmatch(r"requirements[\w.-]*\.(?:txt|in)", p.name) or (
        "requirements" in p.parent.parts and p.name.endswith(".txt")
    ):
        return "requirements"
    return {
        "pyproject.toml": "pyproject",
        "package.json": "package.json",
        "package-lock.json": "package-lock",
        "poetry.lock": "toml-lock",
        "uv.lock": "toml-lock",
        "go.mod": "go.mod",
    }.get(p.name)


def parse_manifest(path: str, content: str) -> list[Dependency]:
    """Dependencies declared in one manifest or lockfile; unparseable files yield none."""
    kind = manifest_kind(path)
    try:
        if kind == "requirements":
            return _parse_requirements(path, content)
        if kind == "pyproject":
            return _parse_pyproject(path, content)
        if kind == "package.json":
            return _parse_package_json(path, content)
        if kind == "package-lock":
            return _parse_package_lock(path, content)
        if kind == "toml-lock":
            return _parse_toml_lock(path, content)
        if kind == "go.mod":
            return _parse_go_mod(path, content)
    except (ValueError, TypeError, AttributeError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not parse %s: %s", path, exc)
    return []


def changed_dependencies(base: list[Dependency], head: list[Dependency]) -> list[DependencyChange]:
    """Head dependencies that are new or whose spec/locked version differs from the base."""
    before: dict[tuple[str, str], set[str]] = {}
    for d in base:
        before.setdefault((d.ecosystem, d.name), set()).add(d.version if d.lockfile else d.spec)
    changes = []
    for d in head:
        old = before.get((d.ecosystem, d.name))
        current = d.version if d.lockfile else d.spec
        if old is None or current not in old:
            changes.append(DependencyChange(d, ", ".join(sorted(old)) if old else None))
    return changes


def possible_typosquat(ecosystem: str, name: str) -> str | None:
    popular = POPULAR.get(ecosystem, ())
    if name in popular:
        return None
    match = difflib.get_close_matches(name, popular, n=1, cutoff=0.85)
    return match[0] if match else None


def _py_dep(name: str, spec: str, path: str, line: int, *, plain: bool = False) -> Dependency:
    exact = (_EXACT_PLAIN if plain else _EXACT_PY).match(spec.strip())
    return Dependency(
        "PyPI", _normalize_py(name), spec.strip(), exact.group(1) if exact else "", path, line
    )


def _normalize_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _line_of(content: str, needle: str) -> int:
    for i, text in enumerate(content.splitlines(), 1):
        if needle in text:
            return i
    return 1


def _parse_requirements(path: str, content: str) -> list[Dependency]:
    deps = []
    for i, raw in enumerate(content.splitlines(), 1):
        text = re.split(r"\s#", raw, maxsplit=1)[0].strip()
        if not text or text.startswith(("#", "-")) or "://" in text:
            continue
        if m := _PY_REQ.match(text):
            deps.append(_py_dep(m.group(1), m.group(2), path, i))
    return deps


def _parse_pyproject(path: str, content: str) -> list[Dependency]:
    data = tomllib.loads(content)
    project = data.get("project") or {}
    reqs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        reqs += group
    for group in (data.get("dependency-groups") or {}).values():
        reqs += [r for r in group if isinstance(r, str)]
    deps = []
    for req in reqs:
        if m := _PY_REQ.match(req):
            deps.append(_py_dep(m.group(1), m.group(2), path, _line_of(content, f'"{req}')))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies") or {}]
    tables += [g.get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
    for table in tables:
        for name, value in table.items():
            if name.lower() == "python":
                continue
            spec = value if isinstance(value, str) else str(value.get("version", ""))
            deps.append(_py_dep(name, spec, path, _line_of(content, name), plain=True))
    return deps


def _parse_package_json(path: str, content: str) -> list[Dependency]:
    data = json.loads(content)
    deps = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            spec = str(spec)
            exact = _EXACT_PLAIN.match(spec)
            line = _line_of(content, f'"{name}"')
            deps.append(Dependency("npm", name, spec, exact.group(1) if exact else "", path, line))
    return deps


def _parse_package_lock(path: str, content: str) -> list[Dependency]:
    data = json.loads(content)
    seen, deps = set(), []
    entries = data.get("packages")
    if entries is None:  # lockfileVersion 1
        entries = {f"node_modules/{k}": v for k, v in (data.get("dependencies") or {}).items()}
    for key, info in entries.items():
        if not key or info.get("link") or not info.get("version"):
            continue
        name = key.rsplit("node_modules/", 1)[-1]
        if (name, info["version"]) in seen:
            continue
        seen.add((name, info["version"]))
        line = _line_of(content, f'"{key}"')
        deps.append(Dependency("npm", name, info["version"], info["version"], path, line, True))
    return deps


def _parse_toml_lock(path: str, content: str) -> list[Dependency]:
    deps = []
    for pkg in tomllib.loads(content).get("package") or []:
        name, version = pkg.get("name"), pkg.get("version")
        if name and version:
            line = _line_of(content, f'name = "{name}"')
            deps.append(Dependency("PyPI", _normalize_py(name), version, version, path, line, True))
    return deps


def _parse_go_mod(path: str, content: str) -> list[Dependency]:
    deps, in_block = [], False
    for i, raw in enumerate(content.splitlines(), 1):
        text = raw.split("//", 1)[0].strip()
        if _GO_REQUIRE_BLOCK.match(text):
            in_block = True
            continue
        if in_block and text == ")":
            in_block = False
            continue
        if text.startswith("require "):
            text = text[len("require ") :].strip()
        elif not in_block:
            continue
        parts = text.split()
        if len(parts) >= 2:
            deps.append(Dependency("Go", parts[0], parts[1], parts[1].lstrip("v"), path, i))
    return deps


@dataclass
class _Metadata:
    exists: bool
    created: datetime | None = None
    latest: str = ""
    license: str = ""
    deprecated: str = ""
    install_scripts: tuple[str, ...] = ()


class DependencyScanner:
    """Checks changed dependencies against OSV.dev and their public package registry."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        osv_url: str = "https://api.osv.dev",
        pypi_url: str = "https://pypi.org",
        npm_url: str = "https://registry.npmjs.org",
        go_proxy_url: str = "https://proxy.golang.org",
        new_package_days: int = 90,
        max_advisories: int = 50,
    ) -> None:
        self._client = client
        self._osv = osv_url.rstrip("/")
        self._pypi = pypi_url.rstrip("/")
        self._npm = npm_url.rstrip("/")
        self._go = go_proxy_url.rstrip("/")
        self._new_days = new_package_days
        self._max_advisories = max_advisories
        self._semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def scan(
        self, changes: list[DependencyChange]
    ) -> tuple[list[ScanFinding], list[dict[str, Any]], list[str]]:
        """Return (findings, per-package facts for the prompt, notes about failed lookups)."""
        notes: list[str] = []
        metas = await asyncio.gather(*(self._metadata(c.dep) for c in changes))
        facts, findings, queries = [], [], []
        for change, meta in zip(changes, metas, strict=True):
            dep = change.dep
            version, basis = dep.version, "exact pin"
            if not version and (m := _LOWER_BOUND.search(dep.spec)):
                version, basis = m.group(1), "lowest version the range allows"
            if not version and meta and meta.latest:
                version, basis = meta.latest, "latest release (unpinned)"
            facts.append(
                {
                    "package": dep.name,
                    "ecosystem": dep.ecosystem,
                    "path": dep.path,
                    "line": dep.line,
                    "spec": dep.spec,
                    "previous": change.previous,
                    "lockfile": dep.lockfile,
                    "checked_version": version,
                    "checked_version_basis": basis if version else "unknown",
                    "latest": meta.latest if meta else "",
                    "license": meta.license if meta else "",
                }
            )
            findings += self._registry_findings(change, meta)
            if version:
                queries.append((dep, version, basis))
        if any(m is None for m in metas):
            notes.append(f"Registry lookup failed for {sum(m is None for m in metas)} package(s).")
        try:
            findings += await self._advisories(queries)
        except httpx.HTTPError as exc:
            log.warning("osv lookup failed: %s", exc)
            notes.append(f"OSV.dev lookup failed ({type(exc).__name__}).")
        return findings, facts, notes

    def _registry_findings(self, change: DependencyChange, meta: _Metadata | None) -> list:
        dep = change.dep

        def finding(rule: str, severity: str, message: str, cwe: str) -> ScanFinding:
            return ScanFinding("registry", rule, dep.path, dep.line, severity, message, cwe)

        out = []
        if change.previous is None and (twin := possible_typosquat(dep.ecosystem, dep.name)):
            out.append(
                finding(
                    "possible-typosquat",
                    "high",
                    f"`{dep.name}` is a new dependency whose name is very close to the popular "
                    f"package `{twin}`.",
                    "CWE-829",
                )
            )
        if meta is None:
            return out
        if not meta.exists:
            out.append(
                finding(
                    "package-not-found",
                    "high",
                    f"`{dep.name}` does not exist on the public {dep.ecosystem} registry: a typo, "
                    "or a name an attacker could register (dependency confusion).",
                    "CWE-427",
                )
            )
            return out
        if change.previous is None and meta.created:
            age = (datetime.now(UTC) - meta.created).days
            if age < self._new_days:
                out.append(
                    finding(
                        "new-package",
                        "medium",
                        f"`{dep.name}` was first published {age} day(s) ago "
                        f"({meta.created.date()}).",
                        "CWE-829",
                    )
                )
        if meta.install_scripts:
            scripts = ", ".join(meta.install_scripts)
            out.append(
                finding(
                    "install-script",
                    "medium",
                    f"`{dep.name}` runs install-time scripts ({scripts}).",
                    "CWE-829",
                )
            )
        if meta.deprecated:
            out.append(
                finding(
                    "deprecated",
                    "low",
                    f"`{dep.name}` is deprecated: {meta.deprecated[:200]}",
                    "CWE-1104",
                )
            )
        if meta.license and _COPYLEFT.search(meta.license):
            out.append(finding("license", "low", f"`{dep.name}` is licensed {meta.license}.", ""))
        return out

    async def _advisories(self, queries: list[tuple[Dependency, str, str]]) -> list[ScanFinding]:
        if not queries:
            return []
        response = await self._client.post(
            f"{self._osv}/v1/querybatch",
            json={
                "queries": [
                    {"package": {"name": d.name, "ecosystem": d.ecosystem}, "version": v}
                    for d, v, _ in queries
                ]
            },
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        hits = [
            (q, vuln["id"])
            for q, r in zip(queries, results, strict=False)
            for vuln in (r.get("vulns") or [])
        ][: self._max_advisories]
        details = await asyncio.gather(*(self._vuln(vid) for _, vid in hits))
        findings = []
        for ((dep, version, basis), vid), vuln in zip(hits, details, strict=True):
            vuln = vuln or {"id": vid}
            aliases = ", ".join(a for a in vuln.get("aliases") or [] if a.startswith("CVE-"))
            fixed = _fixed_versions(vuln, dep.name)
            message = f"`{dep.name}` {version} ({basis}): {vuln.get('summary') or vid}."
            if aliases:
                message += f" Aliases: {aliases}."
            message += f" Fixed in: {', '.join(fixed)}." if fixed else " No fixed version listed."
            db = vuln.get("database_specific") or {}
            severity = _OSV_SEVERITY.get(str(db.get("severity", "")).lower(), "medium")
            cwe = next(iter(db.get("cwe_ids") or []), "CWE-1395")
            findings.append(
                ScanFinding("osv", vid, dep.path, dep.line, severity, message, str(cwe))
            )
        return findings

    async def _vuln(self, vuln_id: str) -> dict[str, Any] | None:
        try:
            async with self._semaphore:
                response = await self._client.get(f"{self._osv}/v1/vulns/{quote(vuln_id)}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            log.warning("osv vuln %s lookup failed: %s", vuln_id, exc)
            return None

    async def _metadata(self, dep: Dependency) -> _Metadata | None:
        try:
            async with self._semaphore:
                if dep.ecosystem == "PyPI":
                    return await self._pypi_metadata(dep)
                if dep.ecosystem == "npm":
                    return await self._npm_metadata(dep)
                return await self._go_metadata(dep)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("registry lookup failed %s %s: %s", dep.ecosystem, dep.name, exc)
            return None

    async def _pypi_metadata(self, dep: Dependency) -> _Metadata:
        response = await self._client.get(f"{self._pypi}/pypi/{quote(dep.name)}/json")
        if response.status_code == 404:
            return _Metadata(exists=False)
        response.raise_for_status()
        data = response.json()
        info = data.get("info") or {}
        uploads = [
            f["upload_time_iso_8601"]
            for files in (data.get("releases") or {}).values()
            for f in files
            if f.get("upload_time_iso_8601")
        ]
        license_ = info.get("license_expression") or info.get("license") or ""
        if not license_ or len(license_) > 80:
            classifiers = [c for c in info.get("classifiers") or [] if c.startswith("License ::")]
            license_ = classifiers[0].rsplit("::", 1)[-1].strip() if classifiers else ""
        return _Metadata(
            exists=True,
            created=min(map(_parse_time, uploads)) if uploads else None,
            latest=str(info.get("version") or ""),
            license=license_,
        )

    async def _npm_metadata(self, dep: Dependency) -> _Metadata:
        response = await self._client.get(f"{self._npm}/{quote(dep.name, safe='@')}")
        if response.status_code == 404:
            return _Metadata(exists=False)
        response.raise_for_status()
        data = response.json()
        latest = str((data.get("dist-tags") or {}).get("latest") or "")
        version = (data.get("versions") or {}).get(dep.version or latest) or {}
        license_ = version.get("license") or data.get("license") or ""
        if isinstance(license_, dict):
            license_ = license_.get("type", "")
        scripts = version.get("scripts") or {}
        created = (data.get("time") or {}).get("created")
        return _Metadata(
            exists=True,
            created=_parse_time(created) if created else None,
            latest=latest,
            license=str(license_),
            deprecated=str(version.get("deprecated") or ""),
            install_scripts=tuple(
                s for s in ("preinstall", "install", "postinstall") if s in scripts
            ),
        )

    async def _go_metadata(self, dep: Dependency) -> _Metadata:
        module = re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), dep.name)
        response = await self._client.get(f"{self._go}/{quote(module)}/@latest")
        if response.status_code in (404, 410):
            return _Metadata(exists=False)
        response.raise_for_status()
        return _Metadata(exists=True, latest=str(response.json().get("Version") or ""))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fixed_versions(vuln: dict[str, Any], name: str) -> list[str]:
    fixed = []
    for affected in vuln.get("affected") or []:
        pkg = (affected.get("package") or {}).get("name", "")
        if pkg.lower() != name.lower() and _normalize_py(pkg) != name:
            continue
        for r in affected.get("ranges") or []:
            fixed += [e["fixed"] for e in r.get("events") or [] if "fixed" in e]
    return sorted(set(fixed))


def dependency_context(facts: list[dict[str, Any]]) -> str:
    return f"<dependency_changes>\n{json.dumps(facts, indent=1)}\n</dependency_changes>"
