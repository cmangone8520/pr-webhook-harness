"""Example plugin: comments "Thanks for the PR!" on every new pull request.

Enable with ``HANDLERS=log,examples.comment_handler:CommentHandler`` and set
``GITHUB_TOKEN`` to a token that can write to the repo's issues.
"""

from __future__ import annotations

import os

import httpx

from prharness import Handler, PullRequestEvent


class CommentHandler(Handler):
    name = "comment"
    actions = frozenset({"opened"})

    async def handle(self, event: PullRequestEvent) -> None:
        url = f"https://api.github.com/repos/{event.repo_full_name}/issues/{event.number}/comments"
        headers = {
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url, headers=headers, json={"body": f"Thanks for the PR, @{event.author}!"}
            )
            response.raise_for_status()
