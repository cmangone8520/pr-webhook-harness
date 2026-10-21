from __future__ import annotations

import json

import httpx

from ..config import Settings
from ..events import PullRequestEvent
from ..signature import sign
from .base import Handler

SIGNATURE_HEADER = "X-PR-Harness-Signature-256"


class ForwardHandler(Handler):
    """POSTs the event summary and raw payload as JSON to ``FORWARD_URL``.

    When ``FORWARD_SECRET`` is set the body is signed with HMAC-SHA256 in the
    ``X-PR-Harness-Signature-256`` header, using the same scheme as GitHub.
    """

    name = "forward"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(settings)
        if not settings.forward_url:
            raise ValueError("forward handler requires FORWARD_URL")
        self._url = settings.forward_url
        self._secret = settings.forward_secret
        self._client = client or httpx.AsyncClient(timeout=settings.forward_timeout_seconds)

    async def handle(self, event: PullRequestEvent) -> None:
        body = json.dumps({"event": event.summary(), "payload": event.payload}).encode()
        headers = {"Content-Type": "application/json", "X-GitHub-Delivery": event.delivery_id}
        if self._secret:
            headers[SIGNATURE_HEADER] = sign(self._secret, body)
        response = await self._client.post(self._url, content=body, headers=headers)
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
