"""Scenario tests covering takeover, graph context, auto-cooling, and recursion."""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityLevel, AddressivityScore
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import (
    At,
    MockEvent,
    Reply,
    _plugin,
    _session_key,
)


@pytest.mark.asyncio
async def test_whitelist_takeover_only_listed_group():
    vc = VirtualClock(initial_time=1000.0)
    plugin = _plugin(
        {"takeover_all": False, "takeover_groups": ["group_ok"], "debounce_base_cooldown": 1.0},
        clock=vc,
    )
    skipped = MockEvent("小助手在吗", group_id="group_other", message_id="s1", is_at_or_wake_command=True)
    taken = MockEvent("小助手在吗", group_id="group_ok", message_id="s2", is_at_or_wake_command=True)

    await plugin.on_group_message(skipped)
    await plugin.on_group_message(taken)

    assert skipped.is_stopped is False
    assert skipped.call_llm is False
    assert taken.is_stopped is False
    assert taken.call_llm is True


@pytest.mark.asyncio
async def test_exclude_group_overrides_takeover_all():
    plugin = _plugin({"takeover_all": True, "exclude_groups": ["group_no"]})
    ev = MockEvent("小助手", group_id="group_no", is_at_or_wake_command=True)
    await plugin.on_group_message(ev)
    assert ev.is_stopped is False


@pytest.mark.asyncio
async def test_recent_nodes_used_when_no_reply_chain():
    vc = VirtualClock(initial_time=3000.0)
    plugin = _plugin({"pipeline_mode": "exclusive", "debounce_base_cooldown": 1.0, "chars_per_second": 80.0, "base_thinking_delay": 0.1}, clock=vc)
    key = _session_key("group_test")
    dag = plugin._get_or_create_runtime(key, group_id="group_test", umo=key, bot_id="bot_42").dag
    assert dag is not None
    dag.add_message("h1", "u_a", "先聊点别的", timestamp=3000.0)
    dag.add_message("h2", "u_b", "对对对", timestamp=3001.0)

    ev = MockEvent(
        "小助手帮我总结一下刚才说的",
        sender_id="u_a",
        group_id="group_test",
        message_id="h3",
        is_at_or_wake_command=True,
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(ev)
    await vc.advance(1.5)
    await vc.advance(5.0)

    assert plugin.context.agent_prompts
    prompt = plugin.context.agent_prompts[-1]
    assert "帮我总结一下刚才说的" in prompt
    assert "先聊点别的" in prompt
    assert "对对对" not in prompt


@pytest.mark.asyncio
async def test_quote_reply_to_bot_is_strong_and_speaks():
    vc = VirtualClock(initial_time=4000.0)
    plugin = _plugin({"debounce_base_cooldown": 1.0, "chars_per_second": 80.0, "base_thinking_delay": 0.1}, clock=vc)
    key = _session_key("group_test")
    dag = plugin._get_or_create_runtime(key, group_id="group_test", umo=key, bot_id="bot_42").dag
    assert dag is not None
    bot_node = dag.add_message("bot_old", "bot_42", "这是架构说明", timestamp=4000.0)
    plugin._sessions[key].last_bot_node = bot_node
    plugin._last_bot_nodes[key] = bot_node
    plugin.addressivity_router.bot_id = "bot_42"

    ev = MockEvent(
        "第二点没看懂",
        sender_id="u_a",
        group_id="group_test",
        message_id="u_q",
        components=[Reply(id="bot_old")],
    )
    await plugin.on_group_message(ev)
    await vc.advance(1.5)
    await vc.advance(5.0)

    node = dag.get_node("u_q")
    score = plugin.addressivity_router.compute_addressivity(node, dag, last_bot_node=bot_node)
    assert score.level == AddressivityLevel.STRONG
    assert ev.is_stopped is False
    assert ev.call_llm is False


@pytest.mark.asyncio
async def test_auto_cooling_after_room_fades():
    vc = VirtualClock(initial_time=5000.0)
    plugin = _plugin({"debounce_base_cooldown": 0.5}, clock=vc)
    key = _session_key("group_test")
    plugin._get_or_create_runtime(key, group_id="group_test", umo=key, bot_id="bot_42")
    plugin.arbiter.record_bot_spoke(key, timestamp=5000.0)
    plugin.vibe_analyzer.set_mode(key, GroupChatMode.CHILL_FADE)
    for i in range(3):
        plugin.vibe_analyzer.record_message(key, f"有人吗 {i}", timestamp=5000.0 + i)

    ev = MockEvent("有人在吗", sender_id="u_z", group_id="group_test", message_id="fade1")
    await plugin.on_group_message(ev)
    await vc.advance(1.0)
    await vc.advance(2.0)

    assert plugin.arbiter.is_in_deep_cooling(key, current_time=vc.time()) is True
    assert ev.replies_sent == []


@pytest.mark.asyncio
async def test_reset_clears_dag_and_cooling():
    plugin = _plugin()
    key = _session_key("group_adm")
    dag = plugin._get_or_create_runtime(key, group_id="group_adm", umo=key, bot_id="bot_42").dag
    assert dag is not None
    dag.add_message("n1", "u1", "hi", timestamp=1.0)
    plugin.arbiter.trigger_cooling(key, duration_seconds=600, current_time=10.0)
    ev = MockEvent("", sender_id="admin", group_id="group_adm", message_id="r1")
    await plugin.cmd_dynamics(ev, action="reset")
    assert len(plugin.dags[key].nodes) == 0
    assert plugin.arbiter.is_in_deep_cooling(key, current_time=11.0) is False


@pytest.mark.asyncio
async def test_llm_failure_does_not_send_canned_reply():
    vc = VirtualClock(initial_time=9000.0)
    plugin = _plugin({"debounce_base_cooldown": 0.5, "base_thinking_delay": 0.05, "chars_per_second": 80.0}, clock=vc)

    async def _boom(**kwargs):
        raise RuntimeError("provider down")

    plugin.context.tool_loop_agent = _boom
    plugin.context.llm_generate = _boom
    plugin.context.get_using_provider = lambda umo=None: None

    ev = MockEvent("小助手你好", group_id="group_test", message_id="fail1", is_at_or_wake_command=True)
    await plugin.on_group_message(ev)
    await vc.advance(1.0)
    await vc.advance(3.0)
    assert ev.replies_sent == []


def _force_weak_banter(plugin, session_key: str, score: float = 0.35, level=AddressivityLevel.WEAK):
    plugin.vibe_analyzer.get_mode = lambda *_args, **_kwargs: GroupChatMode.FAST_BANTER
    plugin.vibe_analyzer.peek_mode = lambda *_args, **_kwargs: GroupChatMode.FAST_BANTER
    plugin.addressivity_router.compute_addressivity = lambda *_args, **_kwargs: AddressivityScore(
        score=score,
        level=level,
        is_bot_targeted=False,
    )
    return session_key


@pytest.mark.asyncio
async def test_filter_default_does_not_ambient_speak():
    vc = VirtualClock(initial_time=11000.0)
    plugin = _plugin(
        {"debounce_base_cooldown": 0.2, "base_thinking_delay": 0.05, "chars_per_second": 80.0},
        clock=vc,
    )
    plugin.pacer.min_typing_delay = 0.0
    key = _session_key("group_test")
    _force_weak_banter(plugin, key)
    ev = MockEvent(
        "这个 Python 接口为什么失败了，部署日志在哪看",
        group_id="group_test",
        message_id="amb0",
    )
    await plugin.on_group_message(ev)
    await vc.advance(1.0)
    await vc.advance(2.0)
    assert plugin.context.agent_prompts == []
    assert ev.replies_sent == []


@pytest.mark.asyncio
async def test_ambient_intervention_speaks_on_weak_high_value_turn():
    vc = VirtualClock(initial_time=12000.0)
    plugin = _plugin(
        {
            "ambient_intervention": True,
            "debounce_base_cooldown": 0.2,
            "base_thinking_delay": 0.05,
            "chars_per_second": 80.0,
        },
        clock=vc,
    )
    plugin.pacer.min_typing_delay = 0.0
    _force_weak_banter(plugin, _session_key("group_test"), score=0.39)
    ev = MockEvent(
        "这个 Python 接口为什么失败了，部署日志在哪看",
        group_id="group_test",
        message_id="amb1",
    )
    await plugin.on_group_message(ev)
    await vc.advance(1.0)
    await vc.advance(3.0)
    assert plugin.context.agent_prompts
    assert ev.replies_sent


@pytest.mark.asyncio
async def test_ambient_intervention_still_silences_hover():
    vc = VirtualClock(initial_time=13000.0)
    plugin = _plugin(
        {"ambient_intervention": True, "debounce_base_cooldown": 0.2, "base_thinking_delay": 0.05},
        clock=vc,
    )
    _force_weak_banter(plugin, _session_key("group_test"), score=0.55, level=AddressivityLevel.SAFE_HOVER)
    ev = MockEvent("这个方案然后呢", group_id="group_test", message_id="amb2")
    await plugin.on_group_message(ev)
    await vc.advance(1.0)
    await vc.advance(2.0)
    assert plugin.context.agent_prompts == []


@pytest.mark.asyncio
async def test_cooling_is_restored_on_initialize():
    plugin = _plugin()
    key = _session_key("persist_cool")
    now = plugin.time_service.time()
    plugin.arbiter.trigger_cooling(key, duration_seconds=600.0, current_time=now)
    stored = {}
    for _ in range(20):
        await asyncio.sleep(0)
        stored = await plugin.get_kv_data("cooling_until", {}) or {}
        if key in stored:
            break
    assert key in stored

    restored = _plugin()
    if hasattr(plugin, "_kv") and hasattr(restored, "_kv"):
        restored._kv = plugin._kv
    else:
        await restored.put_kv_data("cooling_until", stored)
    await restored.initialize()
    assert restored.arbiter.is_in_deep_cooling(key, current_time=now + 1.0) is True
    await restored.delete_kv_data("cooling_until")


class _SplitClock:
    def __init__(self, mono: float, wall: float):
        self._mono = float(mono)
        self._wall = float(wall)

    def time(self) -> float:
        return self._mono

    def wall_time(self) -> float:
        return self._wall

    async def sleep(self, seconds: float) -> None:
        return None


@pytest.mark.asyncio
async def test_cooling_persist_uses_wall_epoch_across_monotonic_reset():
    old = _plugin(clock=_SplitClock(200_000.0, 1_750_000_000.0))
    key = _session_key("persist_wall")
    old.arbiter.trigger_cooling(key, duration_seconds=900.0, current_time=old.time_service.time())
    stored = {}
    for _ in range(20):
        await asyncio.sleep(0)
        stored = await old.get_kv_data("cooling_until", {}) or {}
        if key in stored:
            break
    assert key in stored
    assert abs(float(stored[key]) - (1_750_000_000.0 + 900.0)) < 1.0

    restored = _plugin(clock=_SplitClock(5.0, 1_750_000_030.0))
    restored._kv = old._kv
    await restored.initialize()
    remaining = restored.arbiter.cooling_remaining(key, current_time=5.0)
    assert abs(remaining - 870.0) < 1.0
    await restored.delete_kv_data("cooling_until")
