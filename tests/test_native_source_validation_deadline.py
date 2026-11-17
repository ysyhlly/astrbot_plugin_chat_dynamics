from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core import native_memory_bridge as bridge
from astrbot_plugin_chat_dynamics.core.member_boundary import BoundaryCancelled


def _install_guard(prepare_summary, *, persona=None, deferred=True):
    event = SimpleNamespace(
        _chat_dynamics_owned_request=True,
        _cd_owned_original_input_v1=True,
        send=AsyncMock(),
    )
    request = SimpleNamespace(_lm_private_summary_sources_v1={"source": "legacy-summary"})

    async def persona_is_current():
        return await persona() if persona is not None else True

    persona_is_current._cd_private_persona_owner_v1 = (request, event)
    request._cd_private_persona_current_v1 = persona_is_current
    if deferred:
        prepare_summary._lm_summary_deferred_final_owner_v1 = (request, event)
    request._lm_private_summary_validate_v1 = prepare_summary
    provider = SimpleNamespace(text_chat=AsyncMock())
    runner = SimpleNamespace(req=request, provider=provider)
    send = event.send
    assert bridge._install_native_memory_guard(runner, request, event, None)
    return event, runner, send, provider


def _effect(kind, event, runner, send, provider):
    if kind == "provider":
        return runner.provider.text_chat, provider.text_chat
    return event.send, send


def _set_test_tool_sources(runner, *validators):
    registry = runner._cd_tool_memory_sources_registry_v1
    registry._validators = validators
    registry._validator_ids = tuple(id(validator) for validator in validators)


@pytest.mark.asyncio
async def test_slow_valid_summary_is_fresh_on_provider_and_send():
    prepare_calls = []
    final_calls = []

    async def prepare_summary(*, defer_final):
        assert defer_final
        await asyncio.sleep(1.05)
        attempt = object()
        prepare_calls.append(attempt)

        async def finalize():
            final_calls.append(attempt)
            return True

        return finalize

    event, runner, send, provider = _install_guard(prepare_summary)
    await runner.provider.text_chat()
    await event.send()
    assert len(prepare_calls) == 2
    assert final_calls == prepare_calls
    provider.text_chat.assert_awaited_once()
    send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "send"])
@pytest.mark.parametrize("phase", ["prepare", "finalize"])
async def test_hung_summary_cancels_and_blocks_effect(monkeypatch, kind, phase):
    monkeypatch.setattr(bridge, "_SOURCE_VALIDATION_STAGE_SECONDS", 0.05)
    finished = asyncio.Event()

    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def prepare_summary(*, defer_final):
        if phase == "prepare":
            await hang()
        return hang

    event, runner, send, provider = _install_guard(prepare_summary)
    invoke, delegate = _effect(kind, event, runner, send, provider)
    with pytest.raises(BoundaryCancelled):
        await asyncio.wait_for(invoke(), timeout=1)
    assert finished.is_set()
    assert event._cd_native_memory_failure_reason_v1 == "validation_timeout"
    delegate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "send"])
async def test_validation_rejects_revocation_after_summary_preparation(kind):
    active = True
    prepared = False

    async def persona():
        nonlocal active
        if prepared:
            await asyncio.sleep(0)
            active = False
        return True

    async def prepare_summary(*, defer_final):
        nonlocal prepared
        prepared = True

        async def finalize():
            return active

        return finalize

    event, runner, send, provider = _install_guard(prepare_summary, persona=persona)
    invoke, delegate = _effect(kind, event, runner, send, provider)
    with pytest.raises(BoundaryCancelled):
        await invoke()
    assert event._cd_native_memory_failure_reason_v1 == "summary_sources_not_current"
    delegate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "send"])
async def test_late_true_after_swallowing_timeout_never_reaches_effect(monkeypatch, kind):
    monkeypatch.setattr(bridge, "_SOURCE_VALIDATION_STAGE_SECONDS", 0.05)

    async def prepare_summary(*, defer_final):
        async def finalize():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return True

        return finalize

    event, runner, send, provider = _install_guard(prepare_summary)
    invoke, delegate = _effect(kind, event, runner, send, provider)
    with pytest.raises(BoundaryCancelled):
        await asyncio.wait_for(invoke(), timeout=1)
    assert event._cd_native_memory_failure_reason_v1 == "validation_timeout"
    delegate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "send"])
async def test_external_cancellation_during_validation_is_propagated(kind):
    entered = asyncio.Event()

    async def prepare_summary(*, defer_final):
        async def finalize():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return True

        return finalize

    event, runner, send, provider = _install_guard(prepare_summary)
    invoke, delegate = _effect(kind, event, runner, send, provider)
    task = asyncio.create_task(invoke())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not hasattr(event, "_cd_native_memory_failure_reason_v1")
    delegate.assert_not_awaited()


@pytest.mark.asyncio
async def test_effect_deadline_bounds_all_stages_together(monkeypatch):
    monkeypatch.setattr(bridge, "_SOURCE_VALIDATION_EFFECT_SECONDS", 0.075)
    monkeypatch.setattr(bridge, "_SOURCE_VALIDATION_STAGE_SECONDS", 0.10)

    async def persona():
        await asyncio.sleep(0.035)
        return True

    async def prepare_summary(*, defer_final):
        await asyncio.sleep(0.035)

        async def finalize():
            return True

        return finalize

    event, runner, _, provider = _install_guard(prepare_summary, persona=persona)
    with pytest.raises(BoundaryCancelled):
        await asyncio.wait_for(runner.provider.text_chat(), timeout=1)
    assert event._cd_native_memory_failure_reason_v1 == "validation_timeout"
    provider.text_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_stages_share_one_stage_allowance(monkeypatch):
    monkeypatch.setattr(bridge, "_SOURCE_VALIDATION_STAGE_SECONDS", 0.06)

    async def prepare_summary(*, defer_final):
        await asyncio.sleep(0.04)

        async def finalize():
            await asyncio.sleep(0.04)
            return True

        return finalize

    event, runner, send, _ = _install_guard(prepare_summary)
    with pytest.raises(BoundaryCancelled):
        await asyncio.wait_for(event.send(), timeout=1)
    assert event._cd_native_memory_failure_reason_v1 == "validation_timeout"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_bool_summary_callback_keeps_its_deadline(monkeypatch):
    monkeypatch.setattr(bridge, "_LEGACY_SOURCE_VALIDATION_SECONDS", 0.05)
    delay = 0

    async def validate_summary():
        await asyncio.sleep(delay)
        return True

    event, runner, send, provider = _install_guard(validate_summary, deferred=False)
    await runner.provider.text_chat()
    provider.text_chat.assert_awaited_once()
    delay = 0.10
    with pytest.raises(BoundaryCancelled):
        await event.send()
    assert event._cd_native_memory_failure_reason_v1 == "validation_timeout"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_summary_final_read_error_blocks_delivery_and_logs_only_timing(monkeypatch):
    warnings = []
    monkeypatch.setattr(bridge, "logger", SimpleNamespace(
        warning=lambda message, *args: warnings.append(message % args),
    ))

    async def prepare_summary(*, defer_final):
        async def finalize():
            raise RuntimeError("sensitive-source-text")

        return finalize

    event, _, send, _ = _install_guard(prepare_summary)
    with pytest.raises(BoundaryCancelled):
        await event.send()
    assert event._cd_native_memory_failure_reason_v1 == "validation_exception"
    assert len(warnings) == 1
    assert all(fragment in warnings[0] for fragment in (
        "stage=summary_final", "effect=send", "elapsed_ms=", "stage_times_ms=",
    ))
    assert "sensitive-source-text" not in warnings[0]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_tool_sources_and_summary_finalizer_share_terminal_deadline(monkeypatch):
    monkeypatch.setattr(bridge, "_LEGACY_SOURCE_VALIDATION_SECONDS", 0.10)
    completed = []

    async def prepare_summary(*, defer_final):
        await asyncio.sleep(0.12)
        completed.append("summary_prepared")

        async def finalize():
            await asyncio.sleep(0.06)
            completed.append("summary_finalized")
            return True

        return finalize

    async def check_tool():
        await asyncio.sleep(0.06)
        completed.append("tool_checked")
        return True

    event, runner, send, _ = _install_guard(prepare_summary)
    _set_test_tool_sources(runner, check_tool)
    with pytest.raises(BoundaryCancelled):
        await asyncio.wait_for(event.send(), timeout=1)
    assert completed == ["summary_prepared", "tool_checked"]
    assert event._cd_native_memory_failure_reason_v1 == "validation_timeout"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_validator_registered_while_final_summary_reads_blocks_effect():
    async def check_new_tool_source():
        return True

    async def prepare_summary(*, defer_final):
        async def finalize():
            await asyncio.sleep(0)
            _set_test_tool_sources(runner, check_new_tool_source)
            return True

        return finalize

    event, runner, send, _ = _install_guard(prepare_summary)
    with pytest.raises(BoundaryCancelled):
        await event.send()
    assert event._cd_native_memory_failure_reason_v1 == "tool_binding_invalid"
    send.assert_not_awaited()
