from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

API_VERSION = "2022-11-28"


class GitHubClient:
    """Minimal async GitHub REST client for reading PRs and posting reviews."""

    def __init__(self, token: str, api_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30)
        self._api = api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "pr-webhook-harness",
        }

    async def get_diff(self, repo: str, number: int) -> str:
        response = await self._client.get(
            f"{self._api}/repos/{repo}/pulls/{number}",
            headers={**self._headers, "Accept": "application/vnd.github.diff"},
        )
        response.raise_for_status()
        return response.text

    async def get_file(self, repo: str, path: str, ref: str) -> str | None:
        response = await self._client.get(
            f"{self._api}/repos/{repo}/contents/{quote(path)}",
            params={"ref": ref},
            headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def create_review(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> httpx.Response:
        """Post a comment-only review; ``event`` is fixed so the PR's review state never changes."""
        return await self._client.post(
            f"{self._api}/repos/{repo}/pulls/{number}/reviews",
            headers={**self._headers, "Accept": "application/vnd.github+json"},
            json={"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments},
        )

    async def aclose(self) -> None:
        await self._client.aclose()
