"""Report how often each AI reviewer's inline comments were fixed, resolved or dismissed.

Usage: python scripts/precision_report.py owner/repo [--pr N ...] [--state merged,closed]
       [--limit 50] [--since 2026-01-01] [--by reviewer|severity|category] [--json]

Reads GITHUB_TOKEN, GITHUB_API_URL and GITHUB_REVIEWER_LOGIN from the environment or .env.
Definitions are in prharness/precision.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import asdict
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from prharness.config import Settings  # noqa: E402
from prharness.precision import pr_outcomes, render_markdown, select_prs  # noqa: E402
from prharness.review.github import GitHubClient  # noqa: E402


def _since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    token = settings.github_token.get_secret_value()
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 1
    login = args.reviewer_login
    if login is None:
        login = settings.github_reviewer_login
    github = GitHubClient(token, settings.github_api_url)
    try:
        numbers = args.pr or await select_prs(
            github,
            args.repo,
            states=args.state.split(","),
            limit=args.limit,
            since=_since(args.since) if args.since else None,
        )
        outcomes = []
        for number in numbers:
            outcomes += await pr_outcomes(github, args.repo, number, login)
    finally:
        await github.aclose()
    if args.json:
        print(json.dumps([asdict(o) for o in outcomes], indent=2))
    else:
        print(render_markdown(outcomes, prs=len(numbers), by=args.by), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("--pr", type=int, action="append", help="PR number (repeatable)")
    parser.add_argument("--state", default="merged,closed,open", help="PR states to scan")
    parser.add_argument("--limit", type=int, default=50, help="max PRs, newest first")
    parser.add_argument("--since", help="only PRs updated on/after this ISO date")
    parser.add_argument("--by", choices=["reviewer", "severity", "category"], default="reviewer")
    parser.add_argument("--reviewer-login", help="bot login (default: GITHUB_REVIEWER_LOGIN)")
    parser.add_argument("--json", action="store_true", help="print one record per comment")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
