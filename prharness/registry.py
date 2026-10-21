"""Resolves handler specs (names or ``module:attr`` paths) into handler instances."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib.metadata import entry_points

from .config import Settings
from .handlers.base import Handler
from .handlers.code_review import CodeReviewHandler
from .handlers.compat_review import CompatReviewHandler
from .handlers.forward import ForwardHandler
from .handlers.hygiene_review import HygieneReviewHandler
from .handlers.lead_review import LeadReviewHandler
from .handlers.log import LogHandler
from .handlers.parallel import ParallelHandler
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
    TestReviewHandler.name: TestReviewHandler,
    CompatReviewHandler.name: CompatReviewHandler,
    HygieneReviewHandler.name: HygieneReviewHandler,
    LeadReviewHandler.name: LeadReviewHandler,
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


def split_top_level(text: str, sep: str) -> list[str]:
    """Split on ``sep`` outside parentheses; raises on unbalanced parentheses."""
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced ')' in handler spec {text!r}")
        if char == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if depth:
        raise ValueError(f"unbalanced '(' in handler spec {text!r}")
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def _build_spec(spec: str, settings: Settings) -> Handler:
    """``a>b`` runs in sequence; ``(a,b)`` runs in parallel; groups can nest."""
    steps = split_top_level(spec, ">")
    if len(steps) > 1:
        return SequentialHandler(settings, [_build_spec(s, settings) for s in steps])
    step = steps[0] if steps else ""
    if step.startswith("(") and step.endswith(")"):
        members = split_top_level(step[1:-1], ",")
        if not members:
            raise ValueError(f"empty handler group in {spec!r}")
        children = [_build_spec(m, settings) for m in members]
        return children[0] if len(children) == 1 else ParallelHandler(settings, children)
    if not step or "(" in step or ")" in step:
        raise ValueError(f"invalid handler spec {spec!r}")
    return _build(step, settings)


def build_handlers(settings: Settings) -> list[Handler]:
    """Build handlers from ``HANDLERS``.

    Top-level entries run concurrently, ``a>b`` runs in sequence and ``(a,b)`` is a parallel
    group, e.g. ``log,(code_review,security_review)>lead_review``.
    """
    return [_build_spec(spec, settings) for spec in split_top_level(settings.handlers, ",")]
