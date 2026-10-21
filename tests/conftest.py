from __future__ import annotations

import json
import pathlib
from collections.abc import Callable

import pytest

from prharness import Handler, PullRequestEvent
from prharness.config import Settings
from prharness.signature import sign

SECRET = "test-secret"
SAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples/pull_request_opened.json"


class RecordingHandler(Handler):
    name = "recording"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.events: list[PullRequestEvent] = []

    async def handle(self, event: PullRequestEvent) -> None:
        self.events.append(event)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, github_webhook_secret=SECRET, handlers="log")


@pytest.fixture
def sample_payload() -> dict:
    return json.loads(SAMPLE.read_text())


@pytest.fixture
def signed_headers() -> Callable[..., dict[str, str]]:
    counter = iter(range(1_000_000))

    def make(body: bytes, event: str = "pull_request", delivery: str | None = None) -> dict:
        return {
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or f"delivery-{next(counter)}",
            "X-Hub-Signature-256": sign(SECRET, body),
        }

    return make
