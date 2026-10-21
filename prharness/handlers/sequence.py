from __future__ import annotations

from ..config import Settings
from ..dispatcher import run_handler
from ..events import PullRequestEvent
from .base import Handler


class SequentialHandler(Handler):
    """Runs child handlers one after another (``HANDLERS=a>b``).

    Each child gets its own timeout, and a failing child does not stop later ones.
    """

    def __init__(self, settings: Settings, handlers: list[Handler]) -> None:
        super().__init__(settings)
        self.handlers = handlers
        self.name = ">".join(h.name or type(h).__name__ for h in handlers)
        default = settings.handler_timeout_seconds
        self.timeout_seconds = sum(h.timeout_seconds or default for h in handlers)

    def accepts(self, event: PullRequestEvent) -> bool:
        return any(h.accepts(event) for h in self.handlers)

    async def handle(self, event: PullRequestEvent) -> None:
        default = self.settings.handler_timeout_seconds
        failed = []
        for handler in self.handlers:
            if handler.accepts(event):
                result = await run_handler(handler, event, default)
                if not result.ok:
                    failed.append(result.handler)
        if failed:
            raise RuntimeError(f"failed: {', '.join(failed)}")

    async def aclose(self) -> None:
        for handler in self.handlers:
            await handler.aclose()
