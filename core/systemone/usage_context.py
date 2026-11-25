"""Per-call ContextVar isolation for decision provider usage telemetry."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_DECISION_USAGE_SINK: ContextVar[list[dict[str, Any] | None] | None] = ContextVar(
    "chat_dynamics_decision_usage_sink",
    default=None,
)


@contextmanager
def capture_decision_usage() -> Iterator[list[dict[str, Any] | None]]:
    """Capture usage reported inside the current async call without cross-task clobbering."""
    sink: list[dict[str, Any] | None] = []
    token = _DECISION_USAGE_SINK.set(sink)
    try:
        yield sink
    finally:
        _DECISION_USAGE_SINK.reset(token)


def record_decision_usage(provider: Any, usage: dict[str, Any] | None) -> None:
    """Record usage for the current call and update provider.last_decision_usage for compat."""
    if provider is not None:
        provider.last_decision_usage = usage
    sink = _DECISION_USAGE_SINK.get()
    if sink is not None:
        sink.append(usage)
