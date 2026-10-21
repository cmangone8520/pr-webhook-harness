from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PullRequestEvent:
    """Normalized view of a GitHub ``pull_request`` webhook delivery.

    ``payload`` holds the full raw webhook body for handlers that need more.
    """

    delivery_id: str
    action: str
    number: int
    title: str
    html_url: str
    author: str
    repo_full_name: str
    base_ref: str
    head_ref: str
    head_sha: str
    draft: bool
    installation_id: int | None
    payload: dict[str, Any] = field(repr=False, compare=False)

    @property
    def owner(self) -> str:
        return self.repo_full_name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repo_full_name.split("/", 1)[1]

    @classmethod
    def from_payload(cls, payload: dict[str, Any], delivery_id: str) -> PullRequestEvent:
        pr = payload["pull_request"]
        return cls(
            delivery_id=delivery_id,
            action=payload["action"],
            number=pr["number"],
            title=pr.get("title", ""),
            html_url=pr.get("html_url", ""),
            author=(pr.get("user") or {}).get("login", ""),
            repo_full_name=payload["repository"]["full_name"],
            base_ref=pr["base"]["ref"],
            head_ref=pr["head"]["ref"],
            head_sha=pr["head"]["sha"],
            draft=bool(pr.get("draft", False)),
            installation_id=(payload.get("installation") or {}).get("id"),
            payload=payload,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "action": self.action,
            "number": self.number,
            "title": self.title,
            "html_url": self.html_url,
            "author": self.author,
            "repository": self.repo_full_name,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "draft": self.draft,
            "installation_id": self.installation_id,
        }
