"""FastAPI app: verifies GitHub webhooks and dispatches PR events to handlers."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status

from .config import Settings, get_settings
from .dispatcher import Dispatcher
from .events import PullRequestEvent
from .handlers.base import Handler
from .registry import build_handlers
from .replay import ReplayCache
from .signature import verify_signature

log = logging.getLogger("prharness")


def create_app(settings: Settings | None = None, handlers: list[Handler] | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        if not settings.github_webhook_secret.strip():
            raise RuntimeError("GITHUB_WEBHOOK_SECRET must be set")
        dispatcher = Dispatcher(
            handlers if handlers is not None else build_handlers(settings),
            settings.handler_timeout_seconds,
        )
        app.state.dispatcher = dispatcher
        app.state.replay = ReplayCache(
            settings.webhook_replay_ttl_seconds, settings.webhook_replay_cache_size
        )
        log.info("pr-webhook-harness started handlers=%s", dispatcher.names())
        yield
        await dispatcher.aclose()

    app = FastAPI(title="PR Webhook Harness", version="0.1.0", lifespan=lifespan)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook", status_code=status.HTTP_202_ACCEPTED, tags=["webhook"])
    async def webhook(
        request: Request,
        background: BackgroundTasks,
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
        x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    ) -> dict[str, object]:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > settings.max_webhook_bytes:
                raise HTTPException(status_code=413, detail="payload too large")
        body = await request.body()
        if len(body) > settings.max_webhook_bytes:
            raise HTTPException(status_code=413, detail="payload too large")

        if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
            log.warning("webhook rejected: bad signature event=%s", x_github_event)
            raise HTTPException(status_code=401, detail="bad signature")

        replay: ReplayCache = request.app.state.replay
        if x_github_delivery and replay.seen(x_github_delivery, time.monotonic()):
            log.info("webhook ignored: duplicate delivery_id=%s", x_github_delivery)
            return {"status": "ignored", "reason": "duplicate delivery"}

        if x_github_event == "ping":
            return {"status": "pong"}
        if x_github_event != "pull_request":
            return {"status": "ignored", "reason": f"event={x_github_event}"}

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid json payload") from None

        action = payload.get("action")
        allowed = settings.pr_action_set
        if allowed is not None and action not in allowed:
            return {"status": "ignored", "reason": f"action={action}"}

        try:
            event = PullRequestEvent.from_payload(payload, x_github_delivery or "unknown")
        except (KeyError, TypeError):
            raise HTTPException(status_code=400, detail="malformed pull_request payload") from None

        if settings.ignore_drafts and event.draft:
            return {"status": "ignored", "reason": "draft"}

        dispatcher: Dispatcher = request.app.state.dispatcher
        log.info(
            "dispatching pull_request action=%s target=%s#%d delivery_id=%s",
            event.action,
            event.repo_full_name,
            event.number,
            event.delivery_id,
        )
        background.add_task(dispatcher.dispatch, event)
        return {"status": "dispatched", "handlers": dispatcher.names()}

    return app


def app() -> FastAPI:
    """Factory for ``uvicorn prharness.main:app --factory``."""
    return create_app()
