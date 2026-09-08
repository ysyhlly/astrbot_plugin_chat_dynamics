from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from astrbot_plugin_chat_dynamics.core.native_delivery import NativeDeliveryGuard
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult


@dataclass
class Result:
    chain: list[Any]


@dataclass
class Chain:
    chain: list[Any]


class Event:
    def __init__(self, result: Any, send):
        self.result = result
        self.send = send

    def get_result(self) -> Any:
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned", "message_id"),
    [(None, None), ("platform-id", "platform-id"), ({"message_id": "dict-id"}, "dict-id")],
)
async def test_native_send_success_none_or_id_is_observed(returned: Any, message_id: str | None):
    result = Result([object()])
    calls = []

    async def send(message: Any, **kwargs: Any) -> Any:
        calls.append((message, kwargs))
        return returned

    event = Event(result, send)
    guard = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard.install() is True
    assert await event.send(result, marker="native") is returned
    assert calls == [(result, {"marker": "native"})]
    assert guard.outcome == SendResult(True, message_id)


@pytest.mark.asyncio
async def test_throwing_send_is_reraised_and_never_success():
    result = Result([object()])
    error = RuntimeError("transport failed")

    async def send(_message: Any) -> None:
        raise error

    event = Event(result, send)
    guard = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard.install() is True
    with pytest.raises(RuntimeError) as raised:
        await event.send(result)
    assert raised.value is error
    assert guard.outcome is not None and guard.outcome.success is False


@pytest.mark.asyncio
async def test_cancel_before_send_skips_original_network_call():
    result = Result([object()])
    calls = []

    async def send(_message: Any) -> None:
        calls.append(True)

    event = Event(result, send)
    guard = NativeDeliveryGuard(event, result, lambda: False, lambda: False)
    assert guard.install() is True
    assert await event.send(result) is None
    assert calls == []
    assert guard.started is False
    assert guard.cancelled is True
    assert guard.outcome is not None and guard.outcome.success is False


@pytest.mark.asyncio
async def test_cancel_during_started_send_keeps_success():
    result = Result([object()])
    started = asyncio.Event()
    release = asyncio.Event()
    state = {"current": True}

    async def send(_message: Any) -> None:
        started.set()
        await release.wait()

    event = Event(result, send)
    guard = NativeDeliveryGuard(event, result, lambda: state["current"], lambda: False)
    assert guard.install() is True
    task = asyncio.create_task(event.send(result))
    await started.wait()
    state["current"] = False
    release.set()
    assert await task is None
    assert guard.started is True
    assert guard.cancelled is True
    assert guard.outcome == SendResult(True)


@pytest.mark.asyncio
async def test_owned_and_unrelated_sends_bypass_without_touching_outcome():
    target = Result([object()])
    unrelated = Result([object()])
    calls = []

    async def send(message: Any) -> str:
        calls.append(message)
        return "passed-through"

    event = Event(target, send)
    owned = NativeDeliveryGuard(event, target, lambda: False, lambda: True)
    assert owned.install() is True
    assert await event.send(target) == "passed-through"
    assert owned.outcome is None

    # Keep event's result bound to target; the distinct result object must
    # still pass through even though this guard is installed on the event.
    event.send = send
    unrelated_guard = NativeDeliveryGuard(event, target, lambda: False, lambda: False)
    assert unrelated_guard.install() is True
    assert await event.send(unrelated) == "passed-through"
    assert unrelated_guard.outcome is None
    assert calls == [target, unrelated]


@pytest.mark.asyncio
async def test_sdk_derived_chain_is_bound_by_identity_without_text_matching():
    component = object()
    target = Result([component])
    derived = Chain([component])
    unrelated = Chain([object()])
    calls = []

    async def send(message: Any) -> None:
        calls.append(message)

    event = Event(target, send)
    guard = NativeDeliveryGuard(event, target, lambda: True, lambda: False)
    assert guard.install() is True
    await event.send(derived)
    assert guard.outcome == SendResult(True)
    # A second guard demonstrates that equal-looking text/payloads are not a
    # binding mechanism; only object identity can establish the relation.
    event.send = send
    other_guard = NativeDeliveryGuard(event, target, lambda: False, lambda: False)
    assert other_guard.install() is True
    await event.send(unrelated)
    assert other_guard.outcome is None
    assert calls == [derived, unrelated]


@pytest.mark.asyncio
async def test_kwargs_are_preserved_and_restore_is_idempotent_and_non_clobbering():
    result = Result([object()])
    calls = []

    async def send(message: Any, *, flag: str, **kwargs: Any) -> str:
        calls.append((message, flag, kwargs))
        return "ok"

    event = Event(result, send)
    original = event.send
    guard = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard.install() is True
    wrapped = event.send
    assert guard.install() is True
    assert event.send is wrapped
    assert await event.send(message=result, flag="x", extra=3) == "ok"
    assert calls == [(result, "x", {"extra": 3})]
    guard.restore()
    assert event.send is original
    guard.restore()

    # An external wrapper installed after this guard remains authoritative.
    guard2 = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard2.install() is True
    external = event.send
    event.send = lambda *args, **kwargs: "external"
    guard2.restore()
    assert event.send() == "external"
    assert event.send is not external


def test_install_returns_false_when_instance_cannot_be_wrapped():
    class SlottedEvent:
        __slots__ = ("result",)

        def __init__(self, result: Any) -> None:
            self.result = result

        def get_result(self) -> Any:
            return self.result

        async def send(self, _message: Any) -> None:
            return None

    result = Result([object()])
    event = SlottedEvent(result)
    guard = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard.install() is False
