from __future__ import annotations

import asyncio

from ..config import Settings
from ..dispatcher import run_handler
from ..events import PullRequestEvent
from .base import Handler


class ParallelHandler(Handler):
    """Runs child handlers concurrently (``HANDLERS=(a,b)``), e.g. as one step of ``(a,b)>c``.

    Each child gets its own timeout; the group finishes when every child has, and fails if any
    child failed.
    """

    def __init__(self, settings: Settings, handlers: list[Handler]) -> None:
        super().__init__(settings)
        self.handlers = handlers
        self.name = "(" + ",".join(h.name or type(h).__name__ for h in handlers) + ")"
        default = settings.handler_timeout_seconds
        self.timeout_seconds = max(h.timeout_seconds or default for h in handlers)

    def accepts(self, event: PullRequestEvent) -> bool:
        return any(h.accepts(event) for h in self.handlers)

    async def handle(self, event: PullRequestEvent) -> None:
        default = self.settings.handler_timeout_seconds
        results = await asyncio.gather(
            *(run_handler(h, event, default) for h in self.handlers if h.accepts(event))
        )
        failed = [r.handler for r in results if not r.ok]
        if failed:
            raise RuntimeError(f"failed: {', '.join(failed)}")

    async def aclose(self) -> None:
        for handler in self.handlers:
            await handler.aclose()
