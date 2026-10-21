from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass

from .events import PullRequestEvent
from .handlers.base import Handler

log = logging.getLogger("prharness.dispatcher")


@dataclass(frozen=True)
class HandlerResult:
    handler: str
    ok: bool
    error: str | None = None


class Dispatcher:
    """Runs every accepting handler concurrently; one failure never affects others."""

    def __init__(self, handlers: list[Handler], timeout_seconds: float) -> None:
        self.handlers = handlers
        self._timeout = timeout_seconds

    def names(self) -> list[str]:
        return [h.name or type(h).__name__ for h in self.handlers]

    async def dispatch(self, event: PullRequestEvent) -> list[HandlerResult]:
        targets = [h for h in self.handlers if h.accepts(event)]
        return list(await asyncio.gather(*(run_handler(h, event, self._timeout) for h in targets)))

    async def aclose(self) -> None:
        for handler in self.handlers:
            await handler.aclose()


async def run_handler(
    handler: Handler, event: PullRequestEvent, default_timeout: float
) -> HandlerResult:
    """Run one handler under its timeout, converting any failure into a ``HandlerResult``."""
    name = handler.name or type(handler).__name__
    try:
        async with asyncio.timeout(handler.timeout_seconds or default_timeout):
            if inspect.iscoroutinefunction(handler.handle):
                await handler.handle(event)
            else:
                await asyncio.to_thread(handler.handle, event)
    except Exception as exc:
        log.exception("handler failed handler=%s delivery_id=%s", name, event.delivery_id)
        return HandlerResult(name, ok=False, error=repr(exc))
    log.info("handler ok handler=%s delivery_id=%s", name, event.delivery_id)
    return HandlerResult(name, ok=True)
