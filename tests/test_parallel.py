import asyncio

from prharness import PullRequestEvent
from prharness.config import Settings
from prharness.handlers.base import Handler
from prharness.handlers.parallel import ParallelHandler


class Slow(Handler):
    def __init__(self, settings: Settings, name: str, started: list, fail: bool = False) -> None:
        super().__init__(settings)
        self.name, self.started, self.fail = name, started, fail

    async def handle(self, event: PullRequestEvent) -> None:
        self.started.append(self.name)
        await asyncio.sleep(0.05)
        if self.fail:
            raise RuntimeError("boom")


async def test_runs_children_concurrently_and_reports_failures(settings, sample_payload) -> None:
    event = PullRequestEvent.from_payload(sample_payload, "d")
    started: list[str] = []
    group = ParallelHandler(
        settings, [Slow(settings, "a", started), Slow(settings, "b", started, fail=True)]
    )
    loop = asyncio.get_running_loop()
    begin = loop.time()
    try:
        await group.handle(event)
    except RuntimeError as exc:
        assert "b" in str(exc) and "a" not in str(exc).replace("failed", "")
    else:
        raise AssertionError("expected failure")
    assert sorted(started) == ["a", "b"]
    assert loop.time() - begin < 0.09
    assert group.name == "(a,b)"
