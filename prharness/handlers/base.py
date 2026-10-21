from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable

from ..config import Settings
from ..events import PullRequestEvent


class Handler(ABC):
    """Base class for PR event handlers.

    Subclasses set ``name`` and implement ``handle`` (sync or async). Set
    ``actions`` to restrict a handler to a subset of the globally dispatched
    actions. Handlers are constructed with the app ``Settings``; override
    ``__init__`` to read handler-specific configuration from it.
    """

    name: str = ""
    actions: frozenset[str] | None = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def accepts(self, event: PullRequestEvent) -> bool:
        return self.actions is None or event.action in self.actions

    @abstractmethod
    def handle(self, event: PullRequestEvent) -> Awaitable[None] | None: ...

    async def aclose(self) -> None:  # noqa: B027
        """Release resources on shutdown."""
