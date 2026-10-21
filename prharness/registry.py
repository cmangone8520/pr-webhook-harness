"""Resolves handler specs (names or ``module:attr`` paths) into handler instances."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib.metadata import entry_points

from .config import Settings
from .handlers.base import Handler
from .handlers.code_review import CodeReviewHandler
from .handlers.dependency_review import DependencyReviewHandler
from .handlers.forward import ForwardHandler
from .handlers.log import LogHandler
from .handlers.security_review import SecurityReviewHandler
from .handlers.sequence import SequentialHandler
from .handlers.test_review import TestReviewHandler

ENTRY_POINT_GROUP = "prharness.handlers"

HandlerFactory = Callable[[Settings], Handler]

_REGISTRY: dict[str, HandlerFactory] = {
    LogHandler.name: LogHandler,
    ForwardHandler.name: ForwardHandler,
    CodeReviewHandler.name: CodeReviewHandler,
    SecurityReviewHandler.name: SecurityReviewHandler,
    DependencyReviewHandler.name: DependencyReviewHandler,
    TestReviewHandler.name: TestReviewHandler,
}


def register(name: str) -> Callable[[HandlerFactory], HandlerFactory]:
    """Decorator registering a handler class or factory under ``name``."""

    def decorator(factory: HandlerFactory) -> HandlerFactory:
        _REGISTRY[name] = factory
        return factory

    return decorator


def _import_path(path: str) -> HandlerFactory:
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"invalid handler path {path!r}; expected 'module:attr'")
    obj = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def resolve_factory(spec: str) -> HandlerFactory:
    if ":" in spec:
        return _import_path(spec)
    if spec in _REGISTRY:
        return _REGISTRY[spec]
    matches = entry_points(group=ENTRY_POINT_GROUP, name=spec)
    for ep in matches:
        return ep.load()
    raise LookupError(
        f"unknown handler {spec!r}; register it, expose a {ENTRY_POINT_GROUP!r} "
        "entry point, or use a 'module:attr' path"
    )


def _build(spec: str, settings: Settings) -> Handler:
    handler = resolve_factory(spec)(settings)
    if not isinstance(handler, Handler):
        raise TypeError(f"handler {spec!r} did not produce a Handler instance")
    return handler


def build_handlers(settings: Settings) -> list[Handler]:
    """Build handlers from ``HANDLERS``; ``a>b`` entries run in sequence."""
    handlers: list[Handler] = []
    for spec in settings.handler_specs:
        steps = [s.strip() for s in spec.split(">") if s.strip()]
        if len(steps) == 1:
            handlers.append(_build(steps[0], settings))
        else:
            handlers.append(SequentialHandler(settings, [_build(s, settings) for s in steps]))
    return handlers
