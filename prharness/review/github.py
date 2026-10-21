from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

API_VERSION = "2022-11-28"
_PAGE_SIZE = 100


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

    async def get_tree(self, repo: str, ref: str) -> list[str]:
        """Paths of every file in the repository at ``ref``."""
        response = await self._client.get(
            f"{self._api}/repos/{repo}/git/trees/{quote(ref)}",
            params={"recursive": "1"},
            headers=self._headers,
        )
        response.raise_for_status()
        return [e["path"] for e in response.json().get("tree", []) if e.get("type") == "blob"]

    async def get_compare(self, repo: str, base: str, head: str) -> dict[str, Any] | None:
        """Compare two commits; ``None`` if either is unknown (e.g. gone after a force-push)."""
        response = await self._client.get(
            f"{self._api}/repos/{repo}/compare/{base}...{head}",
            params={"per_page": 1},
            headers={**self._headers, "Accept": "application/vnd.github+json"},
        )
        if response.status_code in (404, 422):
            return None
        response.raise_for_status()
        return response.json()

    async def get_compare_diff(self, repo: str, base: str, head: str) -> str:
        response = await self._client.get(
            f"{self._api}/repos/{repo}/compare/{base}...{head}",
            headers={**self._headers, "Accept": "application/vnd.github.diff"},
        )
        response.raise_for_status()
        return response.text

    async def get_login(self) -> str | None:
        """Login of the token's user, or ``None`` when the token can't read it (App tokens)."""
        response = await self._client.get(
            f"{self._api}/user", headers={**self._headers, "Accept": "application/vnd.github+json"}
        )
        if response.status_code in (401, 403, 404):
            return None
        response.raise_for_status()
        return response.json().get("login")

    async def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._paginate(f"{self._api}/repos/{repo}/pulls/{number}/reviews")

    async def list_review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._paginate(f"{self._api}/repos/{repo}/pulls/{number}/comments")

    async def list_commits(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._paginate(f"{self._api}/repos/{repo}/pulls/{number}/commits")

    async def _paginate(self, url: str, max_pages: int = 10) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            response = await self._client.get(
                url,
                params={"per_page": _PAGE_SIZE, "page": page},
                headers={**self._headers, "Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            batch = response.json()
            items.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
        return items

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
