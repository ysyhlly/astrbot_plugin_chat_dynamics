"""Regression coverage for lost explicit turns and duplicate response delays."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityLevel, AddressivityScore
from astrbot_plugin_chat_dynamics.core.debounce import DebounceResult
from astrbot_plugin_chat_dynamics.core.session_runtime import PendingTurn
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import At, MockEvent, _plugin, _session_key


@pytest.mark.asyncio
async def test_weaker_approved_turn_does_not_invalidate_inflight_at(monkeypatch):
    plugin = _plugin({"pipeline_mode": "exclusive", "vibe_llm_enabled": False})
    event = MockEvent("小助手帮我解释这个问题", message_id="strong", components=[At("bot_42")])
    key = _session_key(event.group_id)
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        started.set()
        await release.wait()
        return "这是回复"

    monkeypatch.setattr(plugin, "_run_native_reply", generate)
    monkeypatch.setattr(plugin.pacer, "calculate_typing_delay", lambda *a, **k: 0)
    try:
        await plugin.on_turn_flushed(DebounceResult(key, event.sender_id, event.message_str, [], [event]))
        await asyncio.wait_for(started.wait(), 1)
        runtime = plugin._sessions[key]
        revision = runtime.revision
        task = runtime.generation_task
        monkeypatch.setattr(plugin.addressivity_router, "compute_addressivity", lambda **k:
                            AddressivityScore(.6, AddressivityLevel.SAFE_HOVER, False))
        original = plugin._resolve_gate_result
        monkeypatch.setattr(plugin, "_resolve_gate_result", lambda *a:
                            replace(original(*a), should_speak=True))
        other = MockEvent("顺便讨论另一个问题", sender_id="other", message_id="weak")
        await plugin.on_turn_flushed(DebounceResult(key, other.sender_id, other.message_str, [], [other]))
        assert runtime.revision == revision
        assert runtime.latest_pending is None
        release.set()
        await asyncio.wait_for(task, 1)
        assert event.replies_sent == ["这是回复"]
    finally:
        release.set()
        await plugin.terminate()


@pytest.mark.asyncio
async def test_replacement_arriving_before_worker_starts_is_delivered(monkeypatch):
    plugin = _plugin({"vibe_llm_enabled": False})
    key = _session_key("queued")
    runtime = plugin._get_or_create_runtime(key, group_id="queued", umo=key, bot_id="bot")
    first = runtime.dag.add_message("first", "u", "first")
    second = runtime.dag.add_message("second", "u", "second")
    result = DebounceResult(key, "u", "first", [], [])
    pending = PendingTurn(result=result, revision=1, node=first, vibe_mode=GroupChatMode.SERIOUS_INQUIRY,
                          raw_event=None, addressivity_level=AddressivityLevel.WEAK, epoch=runtime.epoch)
    runtime.revision = 2
    runtime.latest_pending = replace(pending, revision=2, node=second, addressivity_level=AddressivityLevel.STRONG)
    delivered = []

    async def dispatch(rt, node, mode, event, revision, **kwargs):
        if revision == rt.revision:
            delivered.append(node.msg_id)
        assert rt.generation_level == AddressivityLevel.STRONG

    monkeypatch.setattr(plugin, "_dispatch_bot_response", dispatch)
    try:
        task = asyncio.create_task(plugin._run_generation_loop(runtime, pending))
        runtime.generation_task = task
        await task
        assert delivered == ["second"]
        assert runtime.generation_task is None
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("target, expected", [("bot_42", True), ("other", False)])
async def test_bare_at_component_preserves_request(monkeypatch, target, expected):
    plugin = _plugin({"vibe_llm_enabled": False, "debounce_base_cooldown": 30})
    event = MockEvent("", components=[At(target)], is_at_or_wake_command=expected)
    key = _session_key(event.group_id)

    async def generate(*args, **kwargs):
        return "在的"

    monkeypatch.setattr(plugin, "_run_native_reply", generate)
    monkeypatch.setattr(plugin.pacer, "calculate_typing_delay", lambda *a, **k: 0)
    try:
        await plugin.on_group_message(event)
        assert bool(plugin.debounce.get_pending_count(key)) is expected
        await plugin.debounce.flush(key, event.sender_id)
        task = plugin._sessions[key].generation_task
        if task is not None:
            await asyncio.wait_for(task, 1)
        assert event.replies_sent == (["在的"] if expected else [])
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_seconds, remaining", [(5.0, 0.0), (0.5, 1.5)])
async def test_first_burst_accounts_for_model_latency(monkeypatch, generation_seconds, remaining):
    clock = VirtualClock()
    plugin = _plugin({"vibe_llm_enabled": False}, clock=clock)
    event = MockEvent("问题", message_id="latency")
    key = _session_key(event.group_id)
    runtime = plugin._get_or_create_runtime(key, group_id=event.group_id, umo=key, bot_id=event.self_id)
    node = runtime.dag.add_message(event.message_id, event.sender_id, event.message_str)
    sleeps = []

    async def generate(*args, **kwargs):
        await clock.advance(generation_seconds)
        return "第一段\n第二段"

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(plugin, "_run_native_reply", generate)
    monkeypatch.setattr(plugin.pacer, "shape_and_fragment", lambda *a, **k: ["第一段", "第二段"])
    monkeypatch.setattr(plugin.pacer, "calculate_typing_delay", lambda *a, **k: 2.0)
    monkeypatch.setattr(plugin.pacer, "calculate_inter_burst_delay", lambda *a, **k: 1.0)
    monkeypatch.setattr(clock, "sleep", sleep)
    try:
        await plugin._dispatch_bot_response(runtime, node, GroupChatMode.SERIOUS_INQUIRY, event, runtime.revision)
        assert sleeps == [remaining, 1.0, 2.0]
        assert event.replies_sent == ["第一段", "第二段"]
    finally:
        await plugin.terminate()
