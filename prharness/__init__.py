"""Generic GitHub webhook harness that dispatches PR events to pluggable handlers."""

from .events import PullRequestEvent
from .handlers.base import Handler

__all__ = ["Handler", "PullRequestEvent"]
