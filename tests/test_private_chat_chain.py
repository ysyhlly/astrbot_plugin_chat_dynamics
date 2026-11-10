import copy
import asyncio
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.runtime_persistence import export_runtime_state, restore_runtime_state
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRegistry
from astrbot_plugin_chat_dynamics.core.telemetrics import TelemetricsTracker
from .test_persona_model import BridgeDouble, drain, flush
from .test_plugin_lifecycle import MockEvent, _plugin


def private_event(text="你好", *, sender="user_1", message_id="dm-1", umo=None):
    event = MockEvent(text, sender_id=sender, group_id="", message_id=message_id)
    event.unified_msg_origin = umo or f"mock:PrivateMessage:{sender}"
    return event


@pytest.fixture
def private_plugin(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    plugin = _plugin({
        "decision_mode": "persona_model",
        "decision_backend": "jev",
        "private_chat_enabled": True,
        "base_thinking_delay": 0,
        "debounce_base_cooldown": 10,
        "daily_rhythm_enabled": False,
    })
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge

    async def jev_must_not_run(**_kwargs):
        raise AssertionError("private direct turns must not call Jev")

    plugin.jev.evaluate = jev_must_not_run
    plugin.persona_engine.decide = AsyncMock(side_effect=AssertionError("private turn entered decide()"))
    return plugin, bridge


@pytest.mark.asyncio
async def test_private_message_uses_shared_agent_and_blocks_native_duplicate(private_plugin):
    plugin, bridge = private_plugin
    event = private_event()
    try:
        await plugin.on_private_message(event)
        runtime = plugin._sessions[event.unified_msg_origin]
        assert runtime.scope_kind == "private"
        assert runtime.group_id == ""
        assert event._chat_dynamics_blocks_native is True
        assert event.call_llm is False

        # A later host request for the original DM is rejected. The owned
        # Agent request made by the shared bridge remains separately allowed.
        request = SimpleNamespace(prompt="host duplicate", image_urls=[], audio_urls=[])
        await plugin.on_llm_request(event, request)
        assert event.is_stopped
        assert plugin._metrics["duplicate_native_request_blocked"] == 1
        assert not plugin.context.llm_prompts
        owned = copy.copy(event)
        owned._chat_dynamics_owned_request = True
        owned.continue_event()
        owned_request = SimpleNamespace(prompt="owned reply", image_urls=[], audio_urls=[])
        await plugin.on_llm_request(owned, owned_request)
        assert not owned.is_stopped

        await flush(plugin, event)
        await drain(plugin)

        assert len(event.replies_sent) == 1
        assert len(bridge.requests) == 1
        assert plugin.persona_engine.decide.await_count == 0
        assert not plugin._registry.group_keys.get("")
        assert runtime.jev_decision == {}
        assert runtime.dag.get_node("dm-1").metadata["decision_source"] == "private_direct"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_private_duplicate_is_deduped_by_umo_and_message_id(private_plugin):
    plugin, _bridge = private_plugin
    first = private_event(message_id="same")
    duplicate = private_event(message_id="same")
    try:
        await plugin.on_private_message(first)
        await plugin.on_private_message(duplicate)
        assert plugin._metrics["duplicate_ignored"] == 1
        await flush(plugin, first)
        await drain(plugin)
        assert len(first.replies_sent) == 1
        assert not duplicate.replies_sent
    finally:
        await plugin.terminate()


def test_private_chat_toggle_is_separate_from_private_field():
    default, _ = parse_runtime_config({})
    enabled, _ = parse_runtime_config({"private_chat_enabled": True, "private_field_enabled": False})
    assert default.private_chat_enabled is False
    assert default.private_field_enabled is True
    assert enabled.private_chat_enabled is True
    assert enabled.private_field_enabled is False


def test_private_runtime_roundtrip_keeps_empty_group_scope():
    clock = SimpleNamespace(time=lambda: 10.0)
    source = SimpleNamespace(
        time_service=clock,
        _registry=SessionRegistry(clock),
        telemetrics=TelemetricsTracker(time_service=clock),
        _metrics={},
        _shadow_decisions=deque(maxlen=50),
        _umo_by_session={},
        _vibe_msg_counts={},
        _last_bot_nodes={},
    )
    source._registry.get_or_create(
        "mock:PrivateMessage:user_1", group_id="", umo="mock:PrivateMessage:user_1",
        scope_kind="private",
    )
    snapshot = export_runtime_state(source)
    target = SimpleNamespace(
        time_service=clock,
        _registry=SessionRegistry(clock),
        telemetrics=TelemetricsTracker(time_service=clock),
        _metrics={},
        _shadow_decisions=deque(maxlen=50),
        _umo_by_session={},
        _vibe_msg_counts={},
        _last_bot_nodes={},
    )
    restore_runtime_state(target, snapshot)
    restored = target._registry.get("mock:PrivateMessage:user_1")
    assert restored.scope_kind == "private"
    assert restored.group_id == ""
    assert not target._registry.group_keys.get("")


@pytest.mark.asyncio
async def test_private_stop_command_only_targets_exact_umo(private_plugin):
    plugin, _bridge = private_plugin
    first = private_event(message_id="first", umo="mock:PrivateMessage:user_1")
    second = private_event("/dynamics_stop", message_id="stop-other", umo="mock:PrivateMessage:user_1:other")
    try:
        await plugin.on_private_message(first)
        runtime = plugin._sessions[first.unified_msg_origin]
        before = runtime.user_revisions["user_1"]
        await plugin.cmd_dynamics_stop(second)
        assert runtime.user_revisions["user_1"] == before

        exact = private_event("/dynamics_stop", message_id="stop-exact", umo=first.unified_msg_origin)
        await plugin.cmd_dynamics_stop(exact)
        assert runtime.user_revisions["user_1"] == before + 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_private_bridge_degraded_leaves_host_reply_available(private_plugin):
    plugin, _bridge = private_plugin
    plugin._apply_runtime_config(replace(plugin._runtime_config, decision_mode="legacy"), validated=True)
    event = private_event(message_id="legacy-fallback")
    try:
        await plugin.on_private_message(event)
        assert not hasattr(event, "_chat_dynamics_blocks_native")
        assert event.unified_msg_origin not in plugin._sessions
        assert plugin._metrics["private_persona_unavailable"] == 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_group_fast_path_flush_runs_after_session_lock_is_released(monkeypatch):
    plugin = _plugin({
        "decision_mode": "legacy",
        "pipeline_mode": "filter",
        "base_thinking_delay": 0,
    })
    event = MockEvent("小助手帮我看看", message_id="group-fast", is_at_or_wake_command=True)
    entered = asyncio.Event()

    async def reenter_lock(parsed, raw_event, runtime, **kwargs):
        async with runtime.state_lock:
            entered.set()

    monkeypatch.setattr(plugin, "_flush_single_event", reenter_lock)
    try:
        await asyncio.wait_for(plugin.on_group_message(event), timeout=1)
        assert entered.is_set()
    finally:
        await plugin.terminate()
