"""Wave 3 regression & contract tests: Dynamics Event Bus, slow-turn ack & merged follow-up trace."""

from __future__ import annotations

import asyncio
from dataclasses import replace as dc_replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks, snapshot_overview
from astrbot_plugin_chat_dynamics.core.series_hub import DynamicsEventBus, build_series_health
from astrbot_plugin_chat_dynamics.core.session_runtime import TopicState
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, _plugin

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Public Dynamics Event Bus: pub/sub, isolation, recursion guard, diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamics_event_bus_pub_sub_isolation_timeout_and_recursion_guard():
    bus = DynamicsEventBus(callback_timeout=0.05)
    received: list[tuple[str, str]] = []

    def sync_ok(payload):
        received.append(("sync", str(payload.get("title") or "")))

    async def async_ok(payload):
        received.append(("async", str(payload.get("title") or "")))

    async def crashing(_payload):
        raise RuntimeError("boom")

    async def hanging(_payload):
        await asyncio.sleep(1.0)

    async def recursive(payload):
        # Attempting to re-emit the same event_type must be blocked cleanly
        await bus.emit("milestone_achieved", payload)

    t1 = bus.subscribe("milestone_achieved", sync_ok, owner="world")
    t2 = bus.subscribe("milestone_achieved", async_ok, owner="memory")
    t3 = bus.subscribe("milestone_achieved", crashing, owner="bad_plugin")
    t4 = bus.subscribe("milestone_achieved", hanging, owner="slow_plugin")
    t5 = bus.subscribe("milestone_achieved", recursive, owner="loop_plugin")

    delivered = await bus.emit(
        "milestone_achieved",
        {"umo": "qq:GroupMessage:5001", "source_plugin": "dynamics_games", "title": "周末共读圆满结束"},
    )
    # sync_ok, async_ok, recursive all succeed (recursive inner emit is blocked, outer callback returns)
    assert delivered == 3
    assert ("sync", "周末共读圆满结束") in received
    assert ("async", "周末共读圆满结束") in received

    diag = bus.diagnostics()
    assert diag["subscription_count"] == 5
    assert diag["counts_24h"]["milestone_achieved"] == 1
    assert diag["error_count"] == 2
    assert diag["timeout_count"] == 1
    assert diag["recursion_blocked_count"] == 1
    assert diag["recent_events"][0]["event_type"] == "milestone_achieved"

    assert bus.unsubscribe(t1) is True
    assert bus.unsubscribe(t1) is False
    for tok in (t2, t3, t4, t5):
        assert bus.unsubscribe(tok) is True
    assert bus.diagnostics()["subscription_count"] == 0


@pytest.mark.asyncio
async def test_core_lifecycle_emits_topic_archived_vibe_transition_and_quiet_window():
    p = _plugin({"takeover_all": True})
    seen: list[tuple[str, dict]] = []

    async def on_any(payload):
        seen.append((str(payload.get("event_type") or ""), dict(payload)))

    token = p.subscribe_dynamics_event("*", on_any, owner="test_suite")
    try:
        umo = "qq:GroupMessage:7001"
        rt = p._get_or_create_runtime(umo, group_id="7001", umo=umo, bot_id="bot_42")
        now = p.time_service.time()

        # 1) vibe_transition via set_mode
        p.vibe_analyzer.set_mode(umo, GroupChatMode.CHILL_FADE)
        p.vibe_analyzer.set_mode(umo, GroupChatMode.SERIOUS_INQUIRY)

        # 2) topic_archived + session_quiet_window via _prune_idle_sessions
        old_ts = now - 4000.0
        rt.dag.add_message("old-m1", "u1", "老话题消息", old_ts)
        rt.routing_state.topics["topic_old"] = TopicState(
            topic_id="topic_old",
            message_ids=["old-m1"],
            participants={"u1"},
            updated_at=old_ts,
            human_updated_at=old_ts,
            generated_title="周末露营计划",
        )
        rt.last_activity = now - 600.0
        p._prune_idle_sessions(now)
        # Allow scheduled background emit tasks to run
        await asyncio.sleep(0.05)

        types = [et for et, _ in seen]
        assert "vibe_transition" in types
        vibe_ev = next(pl for et, pl in seen if et == "vibe_transition")
        assert vibe_ev["old_vibe"] == "chill_fade"
        assert vibe_ev["new_vibe"] == "serious_inquiry"
        assert vibe_ev["serious_mode"] is True

        assert "topic_archived" in types
        arch_ev = next(pl for et, pl in seen if et == "topic_archived")
        assert arch_ev["topic_id"] == "topic_old"
        assert arch_ev["umo"] == umo

        assert "session_quiet_window" in types
        quiet_ev = next(pl for et, pl in seen if et == "session_quiet_window")
        assert quiet_ev["umo"] == umo
        assert quiet_ev["quiet_seconds"] >= 300.0

        # Calling _prune_idle_sessions again without new activity must NOT duplicate quiet_window
        quiet_before = sum(1 for et, _ in seen if et == "session_quiet_window")
        p._prune_idle_sessions(now + 10.0)
        await asyncio.sleep(0.05)
        assert sum(1 for et, _ in seen if et == "session_quiet_window") == quiet_before

        # Companion event broadcast + diagnostics visible in health & overview
        await p.emit_dynamics_event("game_state_changed", {"umo": umo, "active": True, "engine": "pictionary"})
        health = build_series_health(p)
        ov = snapshot_overview(p)
        assert health["event_bus"]["counts_24h"]["game_state_changed"] == 1
        assert ov["event_bus"]["subscription_count"] == 1
        assert ov["event_bus"]["total_emitted_24h"] >= 4
    finally:
        p.unsubscribe_dynamics_event(token)
        await p.terminate()


# ---------------------------------------------------------------------------
# 2. Long-turn typing indicator / micro-ack + merged follow-up replay trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_turn_typing_indicator_preferred_over_micro_ack(jev_plugin):
    p, bridge = jev_plugin
    p._runtime_config = dc_replace(p._runtime_config, slow_turn_ack_enabled=True, slow_turn_ack_seconds=0.05)

    entered = asyncio.Event()
    release = asyncio.Event()
    typing_calls: list[tuple[str, dict]] = []

    async def slow_reply():
        entered.set()
        await release.wait()

    bridge.before_reply = slow_reply

    async def fake_call_action(action: str, **kwargs):
        typing_calls.append((action, kwargs))
        return {"status": "ok", "retcode": 0}

    ev = MockEvent("帮我仔细分析下方案", message_id="slow-type-1", sender_id="u1", components=[At("bot_42")])
    ev.bot = SimpleNamespace(call_action=fake_call_action)
    try:
        await p.on_group_message(ev)
        await flush(p, ev)
        await asyncio.wait_for(entered.wait(), 2.0)
        await asyncio.sleep(0.12)
        release.set()
        await drain(p)

        assert any(act == "set_input_status" for act, _ in typing_calls)
        assert not any("稍等一下" in r for r in ev.replies_sent)
        assert len(ev.replies_sent) >= 1
        rt = p._sessions[ev.unified_msg_origin]
        node = rt.dag.get_node("slow-type-1")
        assert node.metadata["slow_turn_ack"]["mode"] == "typing"
        assert p._metrics["slow_turn_ack_sent"] >= 1
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_slow_turn_micro_ack_fallback_when_typing_unavailable(jev_plugin):
    p, bridge = jev_plugin
    p._runtime_config = dc_replace(p._runtime_config, slow_turn_ack_enabled=True, slow_turn_ack_seconds=0.05)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_reply():
        entered.set()
        await release.wait()

    bridge.before_reply = slow_reply
    ev = MockEvent("帮我写个长总结", message_id="slow-ack-1", sender_id="u1", components=[At("bot_42")])
    try:
        await p.on_group_message(ev)
        await flush(p, ev)
        await asyncio.wait_for(entered.wait(), 2.0)
        await asyncio.sleep(0.12)
        release.set()
        await drain(p)

        ack_msgs = [r for r in ev.replies_sent if "稍等一下" in r]
        assert len(ack_msgs) == 1, f"Expected exactly 1 micro-ack, got {ev.replies_sent}"
        assert len(ev.replies_sent) >= 2
        rt = p._sessions[ev.unified_msg_origin]
        node = rt.dag.get_node("slow-ack-1")
        assert node.metadata["slow_turn_ack"]["mode"] == "micro_ack"
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_merged_followup_replay_trace_redaction_and_ui_contracts(jev_plugin):
    p, bridge = jev_plugin
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    calls = {"n": 0}
    orig_generate = bridge.generate

    async def tracked_generate(event, events, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            entered_first.set()
            await release_first.wait()
        return await orig_generate(event, events, *args, **kwargs)

    bridge.generate = tracked_generate
    ev1 = MockEvent("帮我看看部署脚本", message_id="mf-rep-1", sender_id="u1", components=[At("bot_42")])
    ev2 = MockEvent("顺便检查下端口映射", message_id="mf-rep-2", sender_id="u1")
    try:
        await p.on_group_message(ev1)
        await flush(p, ev1)
        await asyncio.wait_for(entered_first.wait(), 2.0)

        await p.on_group_message(ev2)
        release_first.set()
        await drain(p)

        # Redacted by default
        blocks_redacted = replay_topic_blocks(p, [], ev1.unified_msg_origin)
        msgs_red = [m for b in blocks_redacted for m in (b.get("messages") or [])]
        target_red = next(m for m in msgs_red if m["msg_id"] == "mf-rep-1")
        assert target_red["merged_followups"]["count"] == 1
        assert target_red["merged_followups"]["redacted"] is True
        assert target_red["merged_followups"]["snippets"] == ["追问内容已脱敏"]

        # Revealed when admin requests show_content_override=True
        blocks_open = replay_topic_blocks(p, [], ev1.unified_msg_origin, show_content_override=True)
        msgs_open = [m for b in blocks_open for m in (b.get("messages") or [])]
        target_open = next(m for m in msgs_open if m["msg_id"] == "mf-rep-1")
        assert target_open["merged_followups"]["redacted"] is False
        assert "端口映射" in target_open["merged_followups"]["snippets"][0]

        # Frontend contracts
        console_html = (ROOT / "pages" / "app" / "console" / "index.html").read_text(encoding="utf-8")
        console_js = (ROOT / "pages" / "app" / "console" / "app.js").read_text(encoding="utf-8")
        replay_js = (ROOT / "pages" / "app" / "replay" / "app.js").read_text(encoding="utf-8")
        assert 'id="eventBusPanel"' in console_html and 'id="eventBus24hCount"' in console_html
        assert "renderEventBus" in console_js
        assert "data-merged-followups" in replay_js and "data-slow-turn-ack" in replay_js
    finally:
        release_first.set()
        await p.terminate()
