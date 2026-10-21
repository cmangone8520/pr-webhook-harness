import asyncio
import threading

from prharness import Handler, PullRequestEvent
from prharness.config import Settings
from prharness.dispatcher import Dispatcher
from tests.conftest import RecordingHandler


class FailingHandler(Handler):
    name = "failing"

    async def handle(self, event: PullRequestEvent) -> None:
        raise RuntimeError("boom")


class SlowHandler(Handler):
    name = "slow"

    async def handle(self, event: PullRequestEvent) -> None:
        await asyncio.sleep(5)


class SyncHandler(Handler):
    name = "sync"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.thread: str | None = None

    def handle(self, event: PullRequestEvent) -> None:
        self.thread = threading.current_thread().name


class SynchronizeOnly(RecordingHandler):
    name = "sync-only"
    actions = frozenset({"synchronize"})


async def test_failures_and_timeouts_are_isolated(settings, sample_payload) -> None:
    event = PullRequestEvent.from_payload(sample_payload, "d1")
    recorder = RecordingHandler(settings)
    dispatcher = Dispatcher(
        [FailingHandler(settings), SlowHandler(settings), recorder], timeout_seconds=0.1
    )
    results = {r.handler: r for r in await dispatcher.dispatch(event)}
    assert not results["failing"].ok and "boom" in results["failing"].error
    assert not results["slow"].ok
    assert results["recording"].ok
    assert recorder.events == [event]


async def test_sync_handler_runs_off_event_loop(settings, sample_payload) -> None:
    handler = SyncHandler(settings)
    await Dispatcher([handler], 1).dispatch(PullRequestEvent.from_payload(sample_payload, "d"))
    assert handler.thread and handler.thread != threading.main_thread().name


async def test_per_handler_action_filter(settings, sample_payload) -> None:
    handler = SynchronizeOnly(settings)
    results = await Dispatcher([handler], 1).dispatch(
        PullRequestEvent.from_payload(sample_payload, "d")
    )
    assert results == [] and handler.events == []
