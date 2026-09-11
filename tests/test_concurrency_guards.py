from __future__ import annotations

import asyncio
from collections import deque

import pytest

import astrbot_plugin_chat_dynamics.main as main_module
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockContext, MockEvent, _plugin, _session_key


@pytest.mark.asyncio
async def test_owned_send_does_not_suppress_another_umo(monkeypatch):
    plugin = _plugin()
    key_a = _session_key("send-a")
    key_b = _session_key("send-b")
    runtime_a = plugin._get_or_create_runtime(key_a, group_id="send-a", umo=key_a, bot_id="bot-a")
    plugin._get_or_create_runtime(key_b, group_id="send-b", umo=key_b, bot_id="bot-b")
    event_a = MockEvent("a", group_id="send-a", message_id="a1", self_id="bot-a")
    event_b = MockEvent("b", group_id="send-b", message_id="b1", self_id="bot-b")
    event_b.set_result("native b")
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_send(event, _context, _session, text, reply_to_id=None):
        if event is event_a:
            started.set()
            await release.wait()
        return SendResult(True, f"sent-{text}")

    monkeypatch.setattr(main_module, "send_plain", fake_send)
    task = asyncio.create_task(plugin._send_owned(runtime_a, event_a, "a"))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await plugin.on_decorating_result(event_b)
    await event_b.send(event_b.get_result())
    await plugin.after_message_sent(event_b)
    runtime_b = plugin._sessions[key_b]
    assert runtime_b.last_bot_node is not None
    assert runtime_b.last_bot_node.text == "native b"

    release.set()
    await task


@pytest.mark.asyncio
async def test_owned_send_marker_skips_one_delayed_hook_only(monkeypatch):
    plugin = _plugin()
    key = _session_key("delayed-owned")
    runtime = plugin._get_or_create_runtime(key, group_id="delayed-owned", umo=key, bot_id="bot")
    event = MockEvent("trigger", group_id="delayed-owned", message_id="in", self_id="bot")

    async def successful_send(*_args, **_kwargs):
        return SendResult(True, "out-1")

    monkeypatch.setattr(main_module, "send_plain", successful_send)
    await plugin._send_owned(runtime, event, "plugin output")
    event.set_result("plugin output")
    await plugin.after_message_sent(event)
    assert runtime.dag.nodes == {}

    event.set_result("native output")
    await plugin.on_decorating_result(event)
    await event.send(event.get_result())
    await plugin.after_message_sent(event)
    assert [node.text for node in runtime.dag.get_recent_nodes()] == ["native output"]


@pytest.mark.asyncio
async def test_owned_send_marker_counts_delayed_tail_hooks(monkeypatch):
    plugin = _plugin()
    key = _session_key("delayed-owned-tails")
    runtime = plugin._get_or_create_runtime(key, group_id="delayed-owned-tails", umo=key, bot_id="bot")
    event = MockEvent("trigger", group_id="delayed-owned-tails", message_id="in", self_id="bot")

    async def successful_send(*_args, **_kwargs):
        return SendResult(True, "tail-id")

    monkeypatch.setattr(main_module, "send_plain", successful_send)
    await plugin._send_owned(runtime, event, "tail one")
    await plugin._send_owned(runtime, event, "tail two")
    event.set_result("tail one")
    await plugin.after_message_sent(event)
    event.set_result("tail two")
    await plugin.after_message_sent(event)
    assert runtime.dag.nodes == {}


@pytest.mark.asyncio
async def test_reset_blocks_new_ingest_until_old_state_is_cleared(monkeypatch):
    plugin = _plugin({"debounce_base_cooldown": 30.0})
    key = _session_key("reset-barrier")
    old = MockEvent("old", group_id="reset-barrier", message_id="old")
    await plugin.on_group_message(old)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_discard = plugin.debounce.discard

    async def blocked_discard(session_id, user_id=None):
        entered.set()
        await release.wait()
        return await original_discard(session_id, user_id=user_id)

    monkeypatch.setattr(plugin.debounce, "discard", blocked_discard)
    reset_task = asyncio.create_task(plugin._reset_session_state_async(key))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    new = MockEvent("new", group_id="reset-barrier", message_id="new")
    ingest_task = asyncio.create_task(plugin.on_group_message(new))
    await asyncio.sleep(0)
    assert not ingest_task.done()

    release.set()
    await reset_task
    await ingest_task
    assert plugin.debounce.get_pending_count(key) == 1
    assert plugin.dags[key].nodes == {}


@pytest.mark.asyncio
async def test_hard_cap_callback_is_deferred_while_state_lock_is_held():
    plugin = _plugin({"debounce_base_cooldown": 0.0, "debounce_extended_cooldown": 0.0, "debounce_max_cap": 0.0})
    event = MockEvent("hard cap", group_id="hard-cap", message_id="hard-cap-1")
    await asyncio.wait_for(plugin.on_group_message(event), timeout=1.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert plugin.dags[event.unified_msg_origin].nodes


@pytest.mark.asyncio
async def test_shadow_mode_keeps_native_pipeline_untouched():
    clock = VirtualClock(initial_time=100.0)
    plugin = _plugin({"shadow_mode": True, "debounce_base_cooldown": 1.0}, clock=clock)
    event = MockEvent("小助手请分析这个问题", group_id="shadow", message_id="shadow-1", is_at_or_wake_command=True)

    await plugin.on_group_message(event)
    assert event.call_llm is False
    assert event.is_stopped is False
    await clock.advance(2.0)
    await asyncio.sleep(0)
    assert plugin.context.llm_prompts == []
    assert event.replies_sent == []
    assert plugin._metrics.get("shadow_decision", 0) >= 1


@pytest.mark.asyncio
async def test_enabling_shadow_mode_invalidates_inflight_generation():
    plugin = _plugin()
    key = _session_key("shadow-transition")
    runtime = plugin._get_or_create_runtime(key, group_id="shadow-transition", umo=key, bot_id="bot")
    pending = asyncio.create_task(asyncio.sleep(60))
    runtime.generation_task = pending
    plugin.config["shadow_mode"] = True
    plugin.refresh_config()
    assert plugin.is_group_takeover_enabled("shadow-transition") is True
    await asyncio.gather(pending, return_exceptions=True)
    assert pending.cancelled()
    assert runtime.epoch == 1


@pytest.mark.asyncio
async def test_preset_only_changes_declared_behavior_fields():
    plugin = _plugin({"provider": "legacy", "takeover_groups": ["keep"]})
    result = await plugin.apply_preset("active")

    assert result["name"] == "active"
    assert plugin.config["provider"] == "legacy"
    assert plugin.config["takeover_groups"] == ["keep"]
    assert plugin.shadow_mode is False
    assert plugin.ambient_intervention is True
    assert plugin.vibe_llm_enabled is True


@pytest.mark.asyncio
async def test_preset_save_failure_rolls_back_and_is_not_reported_successfully():
    class Config(dict):
        def save_config(self):
            raise OSError("storage unavailable")

    config = Config({"enable": True, "takeover_all": True, "shadow_mode": False})
    plugin = main_module.ChatDynamicsPlugin(context=MockContext(), config=config)
    with pytest.raises(OSError):
        await plugin.apply_preset("observe")
    assert config["shadow_mode"] is False
    assert plugin.shadow_mode is False


@pytest.mark.asyncio
async def test_cooling_persistence_coalesces_to_latest_snapshot():
    plugin = _plugin()
    key = _session_key("cooling-writer")
    plugin._get_or_create_runtime(key, group_id="cooling-writer", umo=key, bot_id="bot")
    started = asyncio.Event()
    release = asyncio.Event()
    writes = []

    async def writer(_name, payload):
        writes.append(dict(payload))
        if len(writes) == 1:
            started.set()
            await release.wait()

    plugin.put_kv_data = writer
    plugin.arbiter.trigger_cooling(key, duration_seconds=60.0, current_time=plugin.time_service.time())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    plugin.arbiter.trigger_cooling(key, duration_seconds=120.0, current_time=plugin.time_service.time())
    release.set()
    task = plugin._cooling_persist_task
    assert task is not None
    await task
    assert len(writes) == 2
    assert writes[-1][key] > writes[0][key]


@pytest.mark.asyncio
async def test_reset_waits_for_and_cancels_native_followup_hook():
    plugin = _plugin()
    key = _session_key("hook-reset")
    runtime = plugin._get_or_create_runtime(key, group_id="hook-reset", umo=key, bot_id="bot")
    event = MockEvent("trigger", group_id="hook-reset", message_id="in", self_id="bot")
    event.set_result("native response")
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["first", "tail"]
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 0.0
    started = asyncio.Event()

    async def blocked_send(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    plugin._send_owned = blocked_send
    await plugin.on_decorating_result(event)
    await event.send(event.get_result())
    hook = asyncio.create_task(plugin.after_message_sent(event))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await plugin._reset_session_state_async(key)
    await asyncio.gather(hook, return_exceptions=True)
    assert runtime.followup_queue == deque()


@pytest.mark.asyncio
async def test_reset_rejects_late_native_hook_callback_from_old_epoch():
    plugin = _plugin()
    key = _session_key("late-hook")
    runtime = plugin._get_or_create_runtime(key, group_id="late-hook", umo=key, bot_id="bot")
    event = MockEvent("trigger", group_id="late-hook", message_id="in", self_id="bot")
    event.set_result("old native result")
    runtime.native_trigger_node = runtime.dag.add_message("trigger", "user", "trigger", timestamp=1.0)
    event._chat_dynamics_epoch = runtime.epoch
    await plugin._reset_session_state_async(key)
    await plugin.after_message_sent(event)
    assert runtime.dag.nodes == {}


@pytest.mark.asyncio
async def test_reset_rejects_native_hook_that_was_stamped_only_at_ingress():
    plugin = _plugin()
    key = _session_key("ingress-stamp")
    event = MockEvent("trigger", group_id="ingress-stamp", message_id="in", self_id="bot")
    await plugin.on_group_message(event)
    old_epoch = event._chat_dynamics_epoch
    event.set_result("old native result")

    await plugin._reset_session_state_async(key)
    assert plugin._sessions[key].epoch > old_epoch
    await plugin.after_message_sent(event)
    assert plugin._sessions[key].dag.nodes == {}


def test_session_runtime_fingerprint_dedup_is_bounded():
    runtime = SessionRuntime("s", "g", "u")
    assert runtime.remember_fingerprint("fp:one") is True
    assert runtime.remember_fingerprint("fp:one") is False
    for index in range(300):
        runtime.remember_fingerprint(f"fp:{index}")
    assert len(runtime.fallback_fingerprints) <= 256
    assert len(runtime.fallback_fingerprint_set) <= 256


@pytest.mark.asyncio
async def test_session_capacity_evicts_old_idle_and_fails_open_when_all_active():
    plugin = _plugin()
    plugin._registry.max_sessions = 2
    first = plugin._get_or_create_runtime("one", group_id="one", umo="one", bot_id="bot")
    second = plugin._get_or_create_runtime("two", group_id="two", umo="two", bot_id="bot")
    first.last_activity = 1.0
    second.last_activity = 2.0
    assert plugin._ensure_runtime_capacity("three") is True
    assert "one" not in plugin._sessions
    active = plugin._get_or_create_runtime("active", group_id="active", umo="active", bot_id="bot")
    active.last_activity = 3.0
    plugin._in_flight.add("two")
    plugin._in_flight.add("active")
    assert plugin._ensure_runtime_capacity("four") is False
    assert plugin._metrics["session_capacity_bypass"] == 1


@pytest.mark.asyncio
async def test_session_capacity_and_idle_prune_skip_auxiliary_tasks():
    plugin = _plugin()
    plugin._registry.max_sessions = 1
    key = "auxiliary"
    runtime = plugin._get_or_create_runtime(key, group_id=key, umo=key, bot_id="bot")
    runtime.last_activity = 1.0
    blocked = asyncio.create_task(asyncio.sleep(60))
    plugin._embedding_tasks_by_session[key] = {blocked}

    assert plugin._ensure_runtime_capacity("new") is False
    plugin._prune_idle_sessions(plugin.time_service.time() + 7200.0)
    assert key in plugin._sessions

    blocked.cancel()
    await asyncio.gather(blocked, return_exceptions=True)
