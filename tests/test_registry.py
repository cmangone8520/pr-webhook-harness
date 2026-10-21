import pytest

from prharness.config import Settings
from prharness.handlers.forward import ForwardHandler
from prharness.handlers.log import LogHandler
from prharness.registry import build_handlers, register, resolve_factory
from tests.conftest import RecordingHandler


def test_builtin_and_dotted_path(settings: Settings) -> None:
    settings.handlers = "log, tests.conftest:RecordingHandler"
    handlers = build_handlers(settings)
    assert [type(h) for h in handlers] == [LogHandler, RecordingHandler]


def test_register_decorator(settings: Settings) -> None:
    register("custom")(RecordingHandler)
    assert resolve_factory("custom") is RecordingHandler


def test_unknown_handler_raises() -> None:
    with pytest.raises(LookupError):
        resolve_factory("does-not-exist")


def test_non_handler_factory_rejected(settings: Settings) -> None:
    settings.handlers = "builtins:dict"
    with pytest.raises(TypeError):
        build_handlers(settings)


def test_forward_requires_url(settings: Settings) -> None:
    with pytest.raises(ValueError):
        ForwardHandler(settings)


def test_parallel_groups_and_sequences(settings: Settings) -> None:
    from prharness.handlers.parallel import ParallelHandler
    from prharness.handlers.sequence import SequentialHandler

    settings.handlers = "log, (tests.conftest:RecordingHandler, log) > (log) > log"
    log_handler, seq = build_handlers(settings)
    assert isinstance(log_handler, LogHandler) and isinstance(seq, SequentialHandler)
    group, single, last = seq.handlers
    assert isinstance(group, ParallelHandler)
    assert [type(h) for h in group.handlers] == [RecordingHandler, LogHandler]
    assert isinstance(single, LogHandler) and isinstance(last, LogHandler)
    assert seq.name == "(recording,log)>log>log"


@pytest.mark.parametrize("spec", ["(log", "log)", "()>log", "lo(g)"])
def test_invalid_group_specs(settings: Settings, spec: str) -> None:
    settings.handlers = spec
    with pytest.raises(ValueError):
        build_handlers(settings)
