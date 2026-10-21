from __future__ import annotations

import logging

from ..events import PullRequestEvent
from .base import Handler

log = logging.getLogger("prharness.handlers.log")


class LogHandler(Handler):
    name = "log"

    async def handle(self, event: PullRequestEvent) -> None:
        log.info(
            "pull_request %s %s#%d by %s: %s (%s -> %s) %s",
            event.action,
            event.repo_full_name,
            event.number,
            event.author,
            event.title,
            event.head_ref,
            event.base_ref,
            event.html_url,
        )
