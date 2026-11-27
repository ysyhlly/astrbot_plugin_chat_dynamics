"""Virtual-clock replay and contract tests for Natural Reply Rhythm."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.dashboard import scene_replay_snapshot
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.natural_rhythm import NaturalRhythmController
from astrbot_plugin_chat_dynamics.core.pacer import PacingShaper
from astrbot_plugin_chat_dynamics.core.runtime_persistence import (
    export_runtime_state,
    restore_runtime_state,
)
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode


class _FakeRegistry:
    def __init__(self) -> None:
        self.runtimes: Dict[str, SessionRuntime] = {}
        self.max_sessions = 100

    def capacity_reached(self) -> bool:
        return len(self.runtimes) >= self.max_sessions

    def get_or_create(
        self,
        session_key: str,
        *,
        group_id: str = "",
        umo: str = "",
        bot_id: str = "bot_anan",
        scope_kind: str = "group",
    ) -> SessionRuntime:
        rt = self.runtimes.get(session_key)
        if rt is None:
            rt = SessionRuntime(
                session_key=session_key,
                group_id=group_id,
                umo=umo or session_key,
                bot_id=bot_id,
                scope_kind=scope_kind,
                dag=ConversationDAG(),
            )
            self.runtimes[session_key] = rt
        return rt


class _FakePlugin:
    def __init__(self, clock: VirtualClock, config_overrides: Dict[str, Any] | None = None) -> None:
        raw = {
            "natural_reply_rhythm_enabled": True,
            "rhythm_catchup_enabled": True,
            "rhythm_afterthought_enabled": True,
            "rhythm_micro_pacing_enabled": True,
            "rhythm_lookback_minutes": 30,
            "rhythm_direct_lookback_minutes": 120,
            "rhythm_distract_min_seconds": 60.0,
            "rhythm_distract_max_seconds": 300.0,
            "rhythm_distract_probability": 1.0,
            "rhythm_catchup_max_items": 2,
            "rhythm_afterthought_min_seconds": 120.0,
            "rhythm_afterthought_max_seconds": 120.0,
            "rhythm_afterthought_cooldown_seconds": 1500.0,
            "rhythm_afterthought_probability": 1.0,
            **(config_overrides or {}),
        }
        self._runtime_config, _ = parse_runtime_config(raw)
        self.time_service = clock
        self._registry = _FakeRegistry()
        self._sessions = self._registry.runtimes
        self.dags: Dict[str, ConversationDAG] = {}
        self._umo_by_session: Dict[str, str] = {}
        self._vibe_msg_counts: Dict[str, int] = {}
        self._last_bot_nodes: Dict[str, Any] = {}
        self._shadow_decisions: List[Any] = []
        self._shutting_down = False
        self.shadow_mode = False
        self.presence_knob = "lively"
        self.console_show_message_content = True
        self.replay_show_topic_titles = True
        self._metrics: Dict[str, int] = {
            "rhythm_deferred": 0,
            "rhythm_catchup_sent": 0,
            "rhythm_catchup_skipped": 0,
            "rhythm_afterthought_sent": 0,
            "rhythm_afterthought_skipped": 0,
            "rhythm_recall_cancelled": 0,
            "rhythm_reload_dedup_skipped": 0,
        }
        self.telemetrics = SimpleNamespace(_records={}, max_history=200)
        self.vibe_analyzer = None
        self.arbiter = None
        self.decision_gate = None
        self.narrative = None
        self.natural_rhythm: NaturalRhythmController | None = None

    def _metric(self, name: str) -> None:
        self._metrics[name] = self._metrics.get(name, 0) + 1

    def _mark_panel_runtime_dirty(self) -> None:
        pass

    def make_session(
        self,
        session_key: str,
        *,
        scope_kind: str = "group",
        group_id: str = "g100",
        bot_id: str = "bot_anan",
    ) -> SessionRuntime:
        rt = self._registry.get_or_create(
            session_key,
            group_id="" if scope_kind == "private" else group_id,
            umo=session_key,
            bot_id=bot_id,
            scope_kind=scope_kind,
        )
        assert rt.dag is not None
        self.dags[session_key] = rt.dag
        return rt


def _add_msg(
    runtime: SessionRuntime,
    clock: VirtualClock,
    msg_id: str,
    user_id: str,
    text: str,
    *,
    topic_id: str = "",
    reply_to_id: str | None = None,
    mentions: List[str] | None = None,
    is_wake: bool = False,
) -> Any:
    assert runtime.dag is not None
    return runtime.dag.add_message(
        msg_id=msg_id,
        user_id=user_id,
        text=text,
        timestamp=clock.time(),
        reply_to_id=reply_to_id,
        mentioned_users=mentions or [],
        metadata={
            "platform_message_id": True,
            "is_wake": is_wake,
            "wall_timestamp": clock.wall_time(),
            "routing": {"topic_id": topic_id} if topic_id else {},
        },
    )


@pytest.mark.asyncio
async def test_scenario1_away_10m_resolved_question_and_window_seat_afterthought():
    """Leaving 10m: settled time question is skipped; window seat is quoted & replied;
    2m quiet triggers one natural afterthought, never a second."""
    clock = VirtualClock(initial_time=10_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:GroupMessage:g100")

    sent_catchups: List[Dict[str, Any]] = []
    sent_afterthoughts: List[Dict[str, Any]] = []

    async def fake_catchup(runtime, item):
        sent_catchups.append(
            {
                "primary": item.primary_msg_id,
                "message_ids": item.message_ids,
                "should_quote": item.should_quote,
            }
        )
        bot_node = _add_msg(
            runtime,
            clock,
            "bot_m1",
            runtime.bot_id,
            "靠窗那个给我留一下",
            topic_id=item.topic_id,
            reply_to_id=item.primary_msg_id,
        )
        runtime.last_bot_node = bot_node
        ctrl.on_turn_delivered(
            runtime,
            anchor_bot_msg_id="bot_m1",
            trigger_msg_id=item.primary_msg_id,
            target_user_id=item.author_id,
            anchor_topic_id=item.topic_id,
            delivered_text="靠窗那个给我留一下",
            persona_fingerprint="p_anan",
            private_direct=False,
            all_fragments_delivered=True,
        )
        return {"status": "sent", "message_id": "bot_m1"}

    async def fake_afterthought(runtime, slot, *, quote_self: bool):
        sent_afterthoughts.append({"anchor": slot.anchor_bot_msg_id, "quote_self": quote_self})
        _add_msg(
            runtime,
            clock,
            "bot_m2",
            runtime.bot_id,
            "对了，回程换你坐窗边吧",
            topic_id=slot.anchor_topic_id,
            reply_to_id=slot.anchor_bot_msg_id if quote_self else None,
        )
        # Re-invoking on_turn_delivered with is_afterthought=True must NEVER arm another slot.
        armed_again = ctrl.on_turn_delivered(
            runtime,
            anchor_bot_msg_id="bot_m2",
            trigger_msg_id=slot.trigger_msg_id,
            target_user_id=slot.target_user_id,
            anchor_topic_id=slot.anchor_topic_id,
            delivered_text="对了，回程换你坐窗边吧",
            persona_fingerprint="p_anan",
            private_direct=False,
            all_fragments_delivered=True,
            is_afterthought=True,
        )
        assert armed_again is False
        return {"status": "sent", "message_id": "bot_m2"}

    ctrl = NaturalRhythmController(
        plugin,
        rng=random.Random(42),
        catchup_executor=fake_catchup,
        afterthought_executor=fake_afterthought,
    )
    plugin.natural_rhythm = ctrl

    # She is busy in dynamics_world.
    n1 = _add_msg(rt, clock, "m1", "u1", "周六几点走？", topic_id="trip")
    dec1 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="m1",
        user_id="u1",
        text=n1.text,
        topic_id="trip",
        availability="busy",
        persona_fingerprint="p_anan",
        node=n1,
    )
    assert dec1.defer is True

    await clock.advance(180.0)
    n2 = _add_msg(rt, clock, "m2", "u2", "大家已经定好了八点出发", topic_id="trip")
    ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="m2",
        user_id="u2",
        text=n2.text,
        topic_id="trip",
        availability="busy",
        persona_fingerprint="p_anan",
        node=n2,
    )

    await clock.advance(120.0)
    n3 = _add_msg(
        rt,
        clock,
        "m3",
        "u3",
        "@安安 给你留靠窗的位置吗？",
        topic_id="trip",
        mentions=["bot_anan"],
        is_wake=True,
    )
    dec3 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="m3",
        user_id="u3",
        text=n3.text,
        topic_id="trip",
        wake_kind="at",
        directed=True,
        availability="busy",
        persona_fingerprint="p_anan",
        node=n3,
    )
    assert dec3.defer is True

    # 10 minutes total away -> world returns to available -> catch-up runs.
    await clock.advance(300.0)
    ctrl.on_availability_changed(rt, "available")
    await clock.advance(10.0)
    await asyncio.sleep(0)

    assert len(sent_catchups) == 1
    assert sent_catchups[0]["primary"] == "m3"
    # m1 was skipped as resolved, recorded in replay trace.
    assert n1.metadata["rhythm_trace"]["reason_code"] == "rhythm_catchup_skipped_resolved"

    # Wait 2 minutes of silence -> single afterthought fires.
    assert len(sent_afterthoughts) == 0
    await clock.advance(125.0)
    await asyncio.sleep(0)
    assert len(sent_afterthoughts) == 1
    assert sent_afterthoughts[0]["anchor"] == "bot_m1"
    assert sent_afterthoughts[0]["quote_self"] is False

    # Advance another 10 minutes -> strictly no second afterthought!
    await clock.advance(600.0)
    await asyncio.sleep(0)
    assert len(sent_afterthoughts) == 1

    # Replay snapshot includes natural rhythm events without errors.
    replay = scene_replay_snapshot(plugin, session_key=rt.session_key)
    codes = {ev["reason_code"] for ev in replay["events"]}
    assert "rhythm_catchup_sent" in codes
    assert "rhythm_afterthought_sent" in codes
    await ctrl.stop()


@pytest.mark.asyncio
async def test_scenario2_normal_private_then_urgent_breakthrough_and_merge():
    """Ordinary private messages defer and merge; urgent ping breaks through immediately."""
    clock = VirtualClock(initial_time=20_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:FriendMessage:u9", scope_kind="private", group_id="")

    delivered_items: List[Any] = []

    async def fake_catchup(runtime, item):
        delivered_items.append(item)
        return {"status": "sent", "message_id": f"bot_{item.primary_msg_id}"}

    ctrl = NaturalRhythmController(plugin, rng=random.Random(1), catchup_executor=fake_catchup)
    plugin.natural_rhythm = ctrl

    # Two normal private messages arrive while distracted -> deferred & later merged.
    p1 = _add_msg(rt, clock, "pm1", "u9", "今天天气不错呀")
    d1 = ctrl.evaluate_ingress_pacing(
        rt, msg_id="pm1", user_id="u9", text=p1.text, private_direct=True, directed=True,
        availability="available", persona_fingerprint="p_anan", node=p1,
    )
    assert d1.defer is True
    assert d1.away_kind == "micro_distract"

    await clock.advance(10.0)
    p2 = _add_msg(rt, clock, "pm2", "u9", "晚上想吃拉面")
    d2 = ctrl.evaluate_ingress_pacing(
        rt, msg_id="pm2", user_id="u9", text=p2.text, private_direct=True, directed=True,
        availability="available", persona_fingerprint="p_anan", node=p2,
    )
    assert d2.defer is True

    # Urgent message arrives -> immediate breakthrough (not deferred!).
    await clock.advance(5.0)
    p3 = _add_msg(rt, clock, "pm3", "u9", "急事！文件报错了等你决定怎么处理")
    d3 = ctrl.evaluate_ingress_pacing(
        rt, msg_id="pm3", user_id="u9", text=p3.text, private_direct=True, directed=True,
        availability="available", persona_fingerprint="p_anan", node=p3,
    )
    assert d3.defer is False
    assert d3.urgent_breakthrough is True
    assert p3.metadata["rhythm_trace"]["reason_code"] == "rhythm_urgent_breakthrough"

    # Let the catch-up timer fire for the two deferred private messages -> merged into one catch-up item.
    await clock.advance(400.0)
    await asyncio.sleep(0)
    assert len(delivered_items) == 1
    assert delivered_items[0].message_ids == ("pm1", "pm2")
    await ctrl.stop()


@pytest.mark.asyncio
async def test_scenario3_group_unrelated_chatter_allows_quoted_afterthought_unless_conflict():
    """In group chat, unrelated topic still allows quoting own message; conflict skips it."""
    clock = VirtualClock(initial_time=30_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:GroupMessage:g200")

    fired: List[bool] = []

    async def fake_afterthought(runtime, slot, *, quote_self: bool):
        fired.append(quote_self)
        return {"status": "sent", "message_id": "bot_sup_1"}

    ctrl = NaturalRhythmController(plugin, rng=random.Random(2), afterthought_executor=fake_afterthought)
    plugin.natural_rhythm = ctrl

    _add_msg(rt, clock, "bot_a1", rt.bot_id, "这个解谜第三关可以从右侧绕过去", topic_id="puzzle")
    armed = ctrl.on_turn_delivered(
        rt,
        anchor_bot_msg_id="bot_a1",
        trigger_msg_id="u_q1",
        target_user_id="u1",
        anchor_topic_id="puzzle",
        delivered_text="这个解谜第三关可以从右侧绕过去",
        persona_fingerprint="p_anan",
        private_direct=False,
        all_fragments_delivered=True,
    )
    assert armed is True

    # Others start talking about an unrelated topic (lunch).
    await clock.advance(30.0)
    _add_msg(rt, clock, "other_1", "u5", "中午吃什么呀大家", topic_id="lunch")
    ctrl.on_user_message_observed(
        rt, msg_id="other_1", user_id="u5", text="中午吃什么呀大家", topic_id="lunch", directed=False
    )

    # Afterthought fires and sets quote_self=True!
    await clock.advance(100.0)
    await asyncio.sleep(0)
    assert fired == [True]

    # Now test conflict guard: arm another afterthought (bypass cooldown with advance),
    # set last_occasion to conflict, add unrelated chatter -> must skip!
    await clock.advance(2000.0)
    _add_msg(rt, clock, "bot_a2", rt.bot_id, "其实第二章也有彩蛋", topic_id="puzzle2")
    assert ctrl.on_turn_delivered(
        rt,
        anchor_bot_msg_id="bot_a2",
        trigger_msg_id="u_q2",
        target_user_id="u1",
        anchor_topic_id="puzzle2",
        delivered_text="其实第二章也有彩蛋",
        persona_fingerprint="p_anan",
        private_direct=False,
        all_fragments_delivered=True,
    )
    await clock.advance(20.0)
    rt.last_occasion = {"kind": "conflict"}
    _add_msg(rt, clock, "other_2", "u6", "你们别吵了", topic_id="drama")
    await clock.advance(120.0)
    await asyncio.sleep(0)
    assert len(fired) == 1  # skipped!
    assert any(ev["reason_code"] == "rhythm_afterthought_skipped_interrupt" for ev in ctrl.replay_rows(rt.session_key))
    await ctrl.stop()


@pytest.mark.asyncio
async def test_scenario4_user_replies_before_afterthought_cancels_cleanly():
    """If the user replies right before afterthought fires, cancel immediately."""
    clock = VirtualClock(initial_time=40_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:FriendMessage:u7", scope_kind="private", group_id="")

    fired = 0

    async def fake_afterthought(runtime, slot, *, quote_self: bool):
        nonlocal fired
        fired += 1
        return {"status": "sent", "message_id": "bot_sup_x"}

    ctrl = NaturalRhythmController(plugin, rng=random.Random(3), afterthought_executor=fake_afterthought)
    plugin.natural_rhythm = ctrl

    _add_msg(rt, clock, "bot_p1", rt.bot_id, "我刚把草稿发你了")
    assert ctrl.on_turn_delivered(
        rt,
        anchor_bot_msg_id="bot_p1",
        trigger_msg_id="u_p1",
        target_user_id="u7",
        anchor_topic_id="",
        delivered_text="我刚把草稿发你了",
        persona_fingerprint="p_anan",
        private_direct=True,
        all_fragments_delivered=True,
    )

    # 10s before fire time (120s), user replies!
    await clock.advance(110.0)
    _add_msg(rt, clock, "u_p2", "u7", "收到啦我马上看")
    ctrl.on_user_message_observed(
        rt, msg_id="u_p2", user_id="u7", text="收到啦我马上看", private_direct=True, directed=True
    )

    await clock.advance(60.0)
    await asyncio.sleep(0)
    assert fired == 0
    assert any(ev["reason_code"] == "rhythm_afterthought_cancelled_reply" for ev in ctrl.replay_rows(rt.session_key))
    await ctrl.stop()


@pytest.mark.asyncio
async def test_boundaries_lookback_30m_vs_120m_failed_fragments_recall_and_reload_dedupe():
    """Verify 30m public / 120m direct boundaries, failed fragment guard, recall cancel, and reload dedupe."""
    clock = VirtualClock(initial_time=50_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:GroupMessage:g300")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(7))
    plugin.natural_rhythm = ctrl

    # 1) Partial / failed fragment delivery must NEVER arm afterthought.
    assert (
        ctrl.on_turn_delivered(
            rt,
            anchor_bot_msg_id="bot_fail",
            trigger_msg_id="t0",
            target_user_id="u1",
            anchor_topic_id="t",
            delivered_text="前半句",
            persona_fingerprint="p_anan",
            private_direct=False,
            all_fragments_delivered=False,
        )
        is False
    )
    assert (
        ctrl.on_turn_delivered(
            rt,
            anchor_bot_msg_id="bot_unc",
            trigger_msg_id="t0",
            target_user_id="u1",
            anchor_topic_id="t",
            delivered_text="不确定送达",
            persona_fingerprint="p_anan",
            private_direct=False,
            all_fragments_delivered=True,
            delivery_uncertain=True,
        )
        is False
    )

    # 2) Lookback boundaries:
    # t=0: direct @ message (will be 45m old -> inside 120m direct window!)
    m_direct = _add_msg(rt, clock, "m_direct", "u1", "@安安 这本书你看过吗？", mentions=["bot_anan"], is_wake=True)
    ctrl.evaluate_ingress_pacing(
        rt, msg_id="m_direct", user_id="u1", text=m_direct.text, directed=True,
        availability="busy", persona_fingerprint="p_anan", node=m_direct,
    )
    # t=0: public message (will be 45m old -> EXCEEDS 30m public window!)
    m_old_pub = _add_msg(rt, clock, "m_old_pub", "u2", "听说新版本要更新地图了", topic_id="game")
    ctrl.evaluate_ingress_pacing(
        rt, msg_id="m_old_pub", user_id="u2", text=m_old_pub.text, topic_id="game", directed=False,
        availability="busy", persona_fingerprint="p_anan", node=m_old_pub,
    )
    # t=0: message that will be recalled before catch-up
    m_recalled = _add_msg(rt, clock, "m_recalled", "u3", "@安安 帮我查一下天气", mentions=["bot_anan"], is_wake=True)
    ctrl.evaluate_ingress_pacing(
        rt, msg_id="m_recalled", user_id="u3", text=m_recalled.text, directed=True,
        availability="busy", persona_fingerprint="p_anan", node=m_recalled,
    )
    # t=0: message whose delivery became in_flight right before reload -> must become uncertain & skip!
    m_inflight = _add_msg(rt, clock, "m_inflight", "u4", "@安安 帮我看下这个谱子", mentions=["bot_anan"], is_wake=True)
    ctrl.evaluate_ingress_pacing(
        rt, msg_id="m_inflight", user_id="u4", text=m_inflight.text, directed=True,
        availability="busy", persona_fingerprint="p_anan", node=m_inflight,
    )
    book = ctrl._book(rt.session_key)
    for c in book.pending_items:
        if c.msg_id == "m_inflight":
            c.status = "in_flight"
            ctrl._record_ledger(book, "m_inflight", "in_flight")

    # Advance 45 minutes -> add a fresh public message (within 30m).
    await clock.advance(45 * 60.0)
    m_fresh_pub = _add_msg(rt, clock, "m_fresh_pub", "u5", "有没有好玩的推理游戏推荐呀？", topic_id="mystery")
    ctrl.evaluate_ingress_pacing(
        rt, msg_id="m_fresh_pub", user_id="u5", text=m_fresh_pub.text, topic_id="mystery", directed=False,
        availability="busy", persona_fingerprint="p_anan", node=m_fresh_pub,
    )

    # Recall m_recalled.
    ctrl.on_message_recalled(rt.session_key, "m_recalled", runtime=rt)

    # Export snapshot and restore into a brand-new plugin instance (simulating reload!).
    snapshot = export_runtime_state(plugin)
    plugin2 = _FakePlugin(clock)
    ctrl2 = NaturalRhythmController(plugin2, rng=random.Random(7))
    plugin2.natural_rhythm = ctrl2
    restore_runtime_state(plugin2, snapshot)

    rt2 = plugin2._sessions[rt.session_key]
    plan = await ctrl2.select_catchup_items(rt2, now_wall=clock.wall_time(), persona_fingerprint="p_anan")
    picked = [item.primary_msg_id for item in plan]

    # m_direct (45m, direct <= 120m) and m_fresh_pub (0m <= 30m) MUST be picked in chronological order!
    assert picked == ["m_direct", "m_fresh_pub"]
    # m_old_pub (>30m), m_recalled (recalled), and m_inflight (uncertain after reload) MUST be excluded!
    assert "m_old_pub" not in picked
    assert "m_recalled" not in picked
    assert "m_inflight" not in picked
    assert plugin2._metrics["rhythm_reload_dedup_skipped"] >= 1
    await ctrl.stop()
    await ctrl2.stop()


def test_online_micro_pacing_typing_and_target_delay():
    """Verify length-aware jittered inter-burst delay and online target delay."""
    pacer = PacingShaper(chars_per_second=20.0, base_thinking_delay=0.3, inter_burst_interval=1.2)
    short_delay = pacer.calculate_inter_burst_delay(
        GroupChatMode.CHILL_FADE, fragment_text="好呀", mpm=4.0, jitter=True, jitter_seed="s1"
    )
    long_delay = pacer.calculate_inter_burst_delay(
        GroupChatMode.CHILL_FADE,
        fragment_text="这段话稍微长一点，需要打字多花一点时间才能发出去，不能跟两个字一样快。",
        mpm=4.0,
        jitter=True,
        jitter_seed="s1",
    )
    assert long_delay > short_delay

    active_target = pacer.calculate_online_target_delay(
        first_fragment="嗯嗯", input_text="在吗", mode=GroupChatMode.FAST_BANTER, idle_seconds=5.0, mpm=15.0, jitter_seed="k"
    )
    cold_long_target = pacer.calculate_online_target_delay(
        first_fragment="这个事情我仔细想了一下其实可以分两步来做",
        input_text="安安帮我看下这个比较长的问题，我刚写完一段剧情感觉前后节奏有点怪不知道怎么改比较好",
        mode=GroupChatMode.CHILL_FADE,
        idle_seconds=300.0,
        mpm=1.5,
        jitter_seed="k",
    )
    assert cold_long_target > active_target


@pytest.mark.asyncio
async def test_deferred_private_and_at_do_not_trigger_mandatory_fallback():
    """Intentional rhythm deferral must NOT trigger the '收到你的消息了，但这次暂时没能生成完整回复。' fallback."""
    from astrbot_plugin_chat_dynamics.core import outcome_recorder as outcomes
    from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn, PersonaEngine
    from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext

    clock = VirtualClock(initial_time=60_000.0)
    plugin = _FakePlugin(clock)
    plugin.context = SimpleNamespace()
    plugin.is_runtime_scope_enabled = lambda _rt: True
    plugin._persona_mode = lambda: True

    sent_payloads: List[str] = []

    async def fake_send_owned(_rt, _ev, fragment, **_kw):
        sent_payloads.append(str(fragment))
        return SimpleNamespace(success=True, message_id="fb_1", error="", delivery_uncertain=False)

    plugin._send_owned = fake_send_owned
    engine = PersonaEngine(plugin)

    # 1) Deferred private chat turn
    rt_priv = plugin.make_session("aiocqhttp:FriendMessage:u42", scope_kind="private", group_id="")
    node_priv = _add_msg(rt_priv, clock, "dm_def_1", "u42", "今天好累呀")
    outcomes.mark_suppressed(node_priv, "rhythm_deferred_distract", stage="gate")
    turn_priv = TurnContext(
        rt_priv.session_key,
        "u42",
        "今天好累呀",
        (MessageSnapshot("dm_def_1", "u42", "今天好累呀", timestamp=clock.time()),),
        (),
        rt_priv.epoch,
        0,
        clock.time(),
        True,
        wake_kind="private",
        bot_id=rt_priv.bot_id,
    )
    ev_priv = SimpleNamespace(_extras={})
    item_priv = ModelTurn(
        turn_priv, (ev_priv,), {}, shadow=False, outcome_nodes=(node_priv,), private_direct=True
    )
    await engine._ensure_mandatory_reply(rt_priv, item_priv)

    # 2) Deferred group @ turn
    rt_grp = plugin.make_session("aiocqhttp:GroupMessage:g42", scope_kind="group", group_id="g42")
    node_grp = _add_msg(rt_grp, clock, "grp_def_1", "u42", "@安安 等会聊", mentions=["bot_anan"], is_wake=True)
    outcomes.mark_suppressed(node_grp, "rhythm_deferred_busy", stage="gate")
    turn_grp = TurnContext(
        rt_grp.session_key,
        "u42",
        "@安安 等会聊",
        (MessageSnapshot("grp_def_1", "u42", "@安安 等会聊", timestamp=clock.time()),),
        (),
        rt_grp.epoch,
        0,
        clock.time(),
        True,
        wake_kind="at",
        bot_id=rt_grp.bot_id,
    )
    ev_grp = SimpleNamespace(_extras={})
    item_grp = ModelTurn(
        turn_grp, (ev_grp,), {}, shadow=False, outcome_nodes=(node_grp,), private_direct=False
    )
    await engine._ensure_mandatory_reply(rt_grp, item_grp)

    assert sent_payloads == []


def _write_bytes(path: str, data: bytes) -> None:
    from pathlib import Path

    Path(path).write_bytes(data)


def _read_bytes(path: str) -> bytes:
    from pathlib import Path

    return Path(path).read_bytes()


def _path_exists(path: str) -> bool:
    from pathlib import Path

    return Path(path).exists()


def _path_is_dir(path: str) -> bool:
    from pathlib import Path

    return Path(path).is_dir()


def _path_is_file(path: str) -> bool:
    from pathlib import Path

    return Path(path).is_file()


def _unlink_file(path: str) -> None:
    from pathlib import Path

    Path(path).unlink()


@pytest.mark.asyncio
async def test_catchup_preserves_independent_media_copy_across_original_release(tmp_path):
    """Deferred catch-up clones and preserves local media so original turn cleanup cannot break catch-up."""
    from dataclasses import dataclass
    from astrbot_plugin_chat_dynamics.core.deferred_media import preserve_event_media, release_event_media
    from astrbot_plugin_chat_dynamics.core.persona_engine import PersonaEngine

    @dataclass
    class Image:
        file: str

    @dataclass
    class FakeMsgObj:
        message: list

    async def _noop_send(_chain):
        return None

    class _FakeMediaEvent:
        def __init__(self, umo: str, components: list, *, extras: dict | None = None, temp_files: list | None = None):
            self.unified_msg_origin = umo
            self.message_obj = FakeMsgObj(message=components)
            self._extras = dict(extras or {})
            if temp_files is not None:
                self._temporary_local_files = list(temp_files)
            self.send = _noop_send

    # Create a temporary platform image file and simulate original turn preservation
    src_img = str(tmp_path / "temp_photo.png")
    _write_bytes(src_img, b"\x89PNG\r\n\x1a\nfake-image-bytes")

    raw_event = _FakeMediaEvent(
        "aiocqhttp:FriendMessage:u99",
        [Image(file=src_img)],
        extras={"origin": "live"},
        temp_files=[src_img],
    )
    turn_preserved = preserve_event_media(raw_event)
    turn_media_dir = str(turn_preserved._chat_dynamics_media_directory)
    assert _path_is_dir(turn_media_dir)

    clock = VirtualClock(initial_time=70_000.0)
    plugin = _FakePlugin(clock, {"rhythm_distract_min_seconds": 60.0, "rhythm_distract_max_seconds": 60.0})
    rt = plugin.make_session("aiocqhttp:FriendMessage:u99", scope_kind="private", group_id="")
    node = _add_msg(rt, clock, "m_img_1", "u99", "看看这张图", is_wake=True)

    captured_turns: List[Any] = []

    class _CapturingEngine(PersonaEngine):
        async def process(self, runtime, item):
            captured_turns.append(item)
            runtime.last_bot_node = runtime.dag.add_message("bot_reply_img", runtime.bot_id, "看到了！", clock.time())

    plugin.context = SimpleNamespace()
    plugin.decision_gate = SimpleNamespace(
        media=SimpleNamespace(
            evaluate=lambda **_kw: SimpleNamespace(
                request_understand=True,
                privacy_hit=False,
                as_dict=lambda: {"request_understand": True, "privacy_hit": False},
            )
        )
    )
    plugin.persona_engine = _CapturingEngine(plugin)

    ctrl = NaturalRhythmController(plugin, rng=random.Random(7))
    plugin.natural_rhythm = ctrl

    dec = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="m_img_1",
        user_id="u99",
        text="看看这张图",
        wake_kind="private",
        directed=True,
        private_direct=True,
        availability="busy",
        persona_fingerprint="p_anan",
        node=node,
        live_event=turn_preserved,
        live_events=(turn_preserved,),
    )
    assert dec.defer is True

    book = ctrl._book(rt.session_key)
    retained = book.live_events["m_img_1"]
    assert isinstance(retained, tuple) and len(retained) == 1
    catchup_dir = str(retained[0]._chat_dynamics_media_directory)
    assert _path_is_dir(catchup_dir)
    assert catchup_dir != turn_media_dir

    # Now both the platform temp file AND the original turn's preserved copy are deleted/released.
    _unlink_file(src_img)
    release_event_media(turn_preserved)
    assert not _path_exists(src_img)
    assert not _path_exists(turn_media_dir)
    # The catch-up clone's independent copy must STILL exist and be readable!
    assert _path_is_dir(catchup_dir)
    retained_path = str(retained[0].message_obj.message[0].file)
    assert _path_is_file(retained_path)
    assert _read_bytes(retained_path) == b"\x89PNG\r\n\x1a\nfake-image-bytes"

    # When catch-up fires later, PersonaEngine receives the preserved media and then releases it.
    await clock.advance(185.0)
    ctrl.on_availability_changed(rt, "available")
    sent = await ctrl.run_catchup(rt)
    assert sent == 1
    assert len(captured_turns) == 1
    model_turn = captured_turns[0]
    assert model_turn.context.messages[0].attachments == ("image",)
    assert "m_img_1" in model_turn.context.visible_node_ids
    assert str(model_turn.events[0].message_obj.message[0].file) == retained_path
    assert rt.request_media_understand is True
    # After catch-up finishes, the independent media directory is released and removed from disk.
    assert "m_img_1" not in book.live_events
    assert not _path_exists(catchup_dir)


@pytest.mark.asyncio
async def test_catchup_media_released_on_recall_stop_reset_persona_switch_eviction_and_shutdown(tmp_path):
    """Preserved catch-up media copies must be cleaned up on every cancellation, eviction, and stop path."""
    from dataclasses import dataclass

    @dataclass
    class Image:
        file: str

    @dataclass
    class MsgObj:
        message: list

    async def _noop_send(_chain):
        return None

    class _FakeMediaEvent:
        def __init__(self, umo: str, components: list):
            self.unified_msg_origin = umo
            self.message_obj = MsgObj(message=components)
            self._extras = {}
            self.send = _noop_send

    def _make_media_event(name: str):
        p = str(tmp_path / f"{name}.png")
        _write_bytes(p, b"img:" + name.encode("utf-8"))
        return _FakeMediaEvent("aiocqhttp:GroupMessage:g_rel", [Image(file=p)])

    clock = VirtualClock(initial_time=80_000.0)
    plugin = _FakePlugin(clock, {"rhythm_distract_min_seconds": 60.0, "rhythm_distract_max_seconds": 60.0})
    ctrl = NaturalRhythmController(plugin, rng=random.Random(11))
    ctrl.MAX_PENDING_ITEMS = 2
    rt = plugin.make_session("aiocqhttp:GroupMessage:g_rel", group_id="g_rel")

    def _defer(mid: str, uid: str = "u1", fp: str = "p1") -> str:
        n = _add_msg(rt, clock, mid, uid, f"msg {mid}", is_wake=True)
        ev = _make_media_event(mid)
        dec = ctrl.evaluate_ingress_pacing(
            rt,
            msg_id=mid,
            user_id=uid,
            text=f"msg {mid}",
            wake_kind="at",
            directed=True,
            availability="busy",
            persona_fingerprint=fp,
            node=n,
            live_event=ev,
        )
        assert dec.defer is True
        book = ctrl._book(rt.session_key)
        return str(book.live_events[mid][0]._chat_dynamics_media_directory)

    # 1) Recall cancels and releases media
    dir_recall = _defer("m_recall", uid="u1")
    assert _path_is_dir(dir_recall)
    assert ctrl.on_message_recalled(rt.session_key, "m_recall", runtime=rt) is True
    assert not _path_exists(dir_recall)

    # 2) Member stop cancels and releases media
    dir_stop = _defer("m_stop", uid="u2")
    assert _path_is_dir(dir_stop)
    ctrl.on_member_stopped(rt.session_key, "u2")
    assert not _path_exists(dir_stop)

    # 3) Capacity eviction (MAX_PENDING_ITEMS = 2) releases evicted oldest item's media
    dir_evict_1 = _defer("m_ev1", uid="u3")
    dir_evict_2 = _defer("m_ev2", uid="u3")
    assert _path_is_dir(dir_evict_1) and _path_is_dir(dir_evict_2)
    dir_evict_3 = _defer("m_ev3", uid="u3")
    assert not _path_exists(dir_evict_1)
    assert _path_is_dir(dir_evict_2) and _path_is_dir(dir_evict_3)

    # 4) Persona switch clears pending debt and releases all retained media
    dir_switch = _defer("m_p2", uid="u3", fp="p2")
    assert not _path_exists(dir_evict_2)
    assert not _path_exists(dir_evict_3)
    assert _path_is_dir(dir_switch)

    # 5) Session reset releases retained media
    ctrl.reset_session(rt.session_key)
    assert not _path_exists(dir_switch)

    # 6) Controller stop() releases all retained media across remaining books
    dir_shutdown = _defer("m_shut", uid="u4", fp="p2")
    assert _path_is_dir(dir_shutdown)
    await ctrl.stop()
    assert not _path_exists(dir_shutdown)


class _StubVibeAnalyzer:
    def __init__(self, mode: GroupChatMode | None, mpm: float, chill_fade_enter_mpm: float = 3.0) -> None:
        self._mode = mode
        self._mpm = mpm
        self.chill_fade_enter_mpm = chill_fade_enter_mpm

    def peek_mode(self, _session_id: str, current_time: float | None = None) -> GroupChatMode | None:
        return self._mode

    def get_telemetrics(self, _session_id: str, current_time: float | None = None) -> Any:
        return SimpleNamespace(mpm=self._mpm)


@pytest.mark.asyncio
async def test_lively_group_never_enters_micro_distract():
    """Lively group (FAST_BANTER or mpm > 3.0) must never enter micro_distract even with probability=1.0."""
    clock = VirtualClock(initial_time=90_000.0)
    plugin = _FakePlugin(clock, {"rhythm_distract_probability": 1.0})
    rt = plugin.make_session("aiocqhttp:GroupMessage:g_lively", group_id="g_lively")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(1))
    plugin.natural_rhythm = ctrl

    # Case 1: FAST_BANTER mode (even if mpm happens to be low due to hysteresis)
    plugin.vibe_analyzer = _StubVibeAnalyzer(mode=GroupChatMode.FAST_BANTER, mpm=2.5)
    n1 = _add_msg(rt, clock, "ml_1", "u1", "@安安 快来看看这个", mentions=["bot_anan"], is_wake=True)
    d1 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="ml_1",
        user_id="u1",
        text=n1.text,
        wake_kind="at",
        directed=True,
        availability="available",
        persona_fingerprint="p_anan",
        node=n1,
    )
    assert d1.defer is False
    assert d1.away_kind == "none"

    # Case 2: CHILL_FADE mode but mpm > 3.0
    plugin.vibe_analyzer = _StubVibeAnalyzer(mode=GroupChatMode.CHILL_FADE, mpm=4.2)
    n2 = _add_msg(rt, clock, "ml_2", "u2", "@安安 晚上打游戏吗", mentions=["bot_anan"], is_wake=True)
    d2 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="ml_2",
        user_id="u2",
        text=n2.text,
        wake_kind="at",
        directed=True,
        availability="available",
        persona_fingerprint="p_anan",
        node=n2,
    )
    assert d2.defer is False
    assert d2.away_kind == "none"

    # Case 3: SERIOUS_INQUIRY mode with low mpm
    plugin.vibe_analyzer = _StubVibeAnalyzer(mode=GroupChatMode.SERIOUS_INQUIRY, mpm=2.0)
    n3 = _add_msg(rt, clock, "ml_3", "u3", "@安安 帮我分析一下这个问题", mentions=["bot_anan"], is_wake=True)
    d3 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="ml_3",
        user_id="u3",
        text=n3.text,
        wake_kind="at",
        directed=True,
        availability="available",
        persona_fingerprint="p_anan",
        node=n3,
    )
    assert d3.defer is False
    assert d3.away_kind == "none"
    await ctrl.stop()


@pytest.mark.asyncio
async def test_group_recent_bot_speech_blocks_micro_distract():
    """Group where bot spoke < 90s ago must never enter micro_distract, even for a different user."""
    clock = VirtualClock(initial_time=95_000.0)
    plugin = _FakePlugin(clock, {"rhythm_distract_probability": 1.0})
    plugin.vibe_analyzer = _StubVibeAnalyzer(mode=GroupChatMode.CHILL_FADE, mpm=1.0)
    rt = plugin.make_session("aiocqhttp:GroupMessage:g_recent", group_id="g_recent")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(2))
    plugin.natural_rhythm = ctrl

    # Bot just spoke to u1.
    rt.last_interlocutor = "u1"
    ctrl.on_turn_delivered(
        rt,
        anchor_bot_msg_id="bot_r1",
        trigger_msg_id="u_r1",
        target_user_id="u1",
        anchor_topic_id="t1",
        delivered_text="刚说完一句",
        persona_fingerprint="p_anan",
        private_direct=False,
        all_fragments_delivered=True,
    )

    # 30s later (inside 45s window, different user u2 -> no 0.15 mid-chat fallback)
    await clock.advance(30.0)
    n1 = _add_msg(rt, clock, "mr_1", "u2", "@安安 你也在啊", mentions=["bot_anan"], is_wake=True)
    d1 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="mr_1",
        user_id="u2",
        text=n1.text,
        wake_kind="at",
        directed=True,
        availability="available",
        persona_fingerprint="p_anan",
        node=n1,
    )
    assert d1.defer is False
    assert d1.away_kind == "none"

    # 89s total since bot spoke (< 90s threshold) -> still never distracts.
    await clock.advance(59.0)
    n2 = _add_msg(rt, clock, "mr_2", "u3", "@安安 顺便问一下", mentions=["bot_anan"], is_wake=True)
    d2 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="mr_2",
        user_id="u3",
        text=n2.text,
        wake_kind="at",
        directed=True,
        availability="available",
        persona_fingerprint="p_anan",
        node=n2,
    )
    assert d2.defer is False
    assert d2.away_kind == "none"
    await ctrl.stop()


@pytest.mark.asyncio
async def test_cold_group_allows_micro_distract_and_70s_rapid_ping_breaks_through():
    """Cold group (CHILL_FADE, mpm <= 3.0, bot idle >= 90s) can micro_distract,
    and a follow-up @/quote 70s later breaks through within the 90s RAPID_PING_WINDOW."""
    clock = VirtualClock(initial_time=100_000.0)
    plugin = _FakePlugin(
        clock,
        {
            "rhythm_distract_probability": 1.0,
            "rhythm_distract_min_seconds": 180.0,
            "rhythm_distract_max_seconds": 180.0,
        },
    )
    plugin.vibe_analyzer = _StubVibeAnalyzer(mode=GroupChatMode.CHILL_FADE, mpm=3.0)
    rt = plugin.make_session("aiocqhttp:GroupMessage:g_cold", group_id="g_cold")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(3))
    plugin.natural_rhythm = ctrl

    # Bot spoke 90s ago -> meets COLD_GROUP_MIN_BOT_IDLE_SECONDS boundary.
    ctrl.on_turn_delivered(
        rt,
        anchor_bot_msg_id="bot_c0",
        trigger_msg_id="u_c0",
        target_user_id="u1",
        anchor_topic_id="t0",
        delivered_text="嗯好",
        persona_fingerprint="p_anan",
        private_direct=False,
        all_fragments_delivered=True,
    )
    await clock.advance(90.0)

    # First @ ping in cold group -> enters micro_distract.
    n1 = _add_msg(rt, clock, "mc_1", "u1", "@安安 在不在呀", mentions=["bot_anan"], is_wake=True)
    ctrl.on_user_message_observed(rt, msg_id="mc_1", user_id="u1", text=n1.text, directed=True)
    d1 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="mc_1",
        user_id="u1",
        text=n1.text,
        wake_kind="at",
        directed=True,
        availability="available",
        persona_fingerprint="p_anan",
        node=n1,
    )
    assert d1.defer is True
    assert d1.away_kind == "micro_distract"

    # Follow-up @ ping 70s later (inside widened 90s RAPID_PING_WINDOW, still within 180s distract) -> breaks through!
    await clock.advance(70.0)
    n2 = _add_msg(rt, clock, "mc_2", "u1", "@安安 怎么不理我呀", mentions=["bot_anan"], is_wake=True)
    ctrl.on_user_message_observed(rt, msg_id="mc_2", user_id="u1", text=n2.text, directed=True)
    d2 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="mc_2",
        user_id="u1",
        text=n2.text,
        wake_kind="at",
        directed=True,
        availability="available",
        persona_fingerprint="p_anan",
        node=n2,
    )
    assert d2.defer is False
    assert d2.urgent_breakthrough is True
    assert d2.reason_code == "rhythm_urgent_breakthrough"
    await ctrl.stop()


@pytest.mark.asyncio
async def test_probe_world_availability_resolves_concrete_scope_without_bundle():
    """Cold/private sessions with bundle=None must resolve concrete 5-field scope and never trigger NarrativeValidationError."""
    clock = VirtualClock(initial_time=110_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:FriendMessage:u_cold", scope_kind="private", group_id="")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(9))
    plugin.natural_rhythm = ctrl

    captured_scopes: List[Dict[str, Any]] = []

    class _StrictNarrative:
        def __init__(self, *, worldbook_result: Any = None, bundles: Dict[str, Any] | None = None) -> None:
            self._bundles = bundles or {}
            self._worldbook_result = worldbook_result

        def enabled(self, _umo: str) -> bool:
            return True

        async def call(self, provider: str, method: str, *args: Any, timeout: float = 3.0) -> Any:
            if provider == "worldbook" and method == "get_character_context":
                return self._worldbook_result
            if provider == "dynamic_life_state" and method == "get_snapshot":
                scope = args[0]
                if not isinstance(scope, dict) or scope.get("schema_version") != 1:
                    raise ValueError("unsupported_scope_schema")
                if not str(scope.get("persona_id") or "").strip() or not str(scope.get("instance_id") or "").strip():
                    raise ValueError("a_concrete_persona_is_required")
                captured_scopes.append(dict(scope))
                return {"schema_version": 1, "managed": True, "stale": False, "availability": "asleep"}
            return None

    # 1) Cold session (bundle=None) resolves concrete scope via worldbook.get_character_context.
    plugin.narrative = _StrictNarrative(
        worldbook_result={
            "schema_version": 1,
            "available": True,
            "scope": {
                "platform": "aiocqhttp",
                "umo": rt.umo,
                "persona_id": "anan_v2",
                "instance_id": "inst_anan_1",
            },
        }
    )
    avail = await ctrl._probe_world_availability(rt)
    assert avail == "asleep"
    assert len(captured_scopes) == 1
    assert captured_scopes[0] == {
        "schema_version": 1,
        "platform": "aiocqhttp",
        "umo": rt.umo,
        "persona_id": "anan_v2",
        "instance_id": "inst_anan_1",
    }

    # 2) Worldbook unavailable, fallback to host persona_manager resolves concrete scope.
    captured_scopes.clear()
    plugin.narrative = _StrictNarrative(worldbook_result=None)

    async def _get_cid(_umo: str) -> str:
        return "cid_1"

    async def _get_conv(_umo: str, _cid: str) -> Any:
        return SimpleNamespace(persona_id="anan_host")

    async def _resolve_persona(**_kw: Any) -> tuple:
        return ("anan_host", {"name": "anan_host"}, None, None)

    plugin.context = SimpleNamespace(
        conversation_manager=SimpleNamespace(get_curr_conversation_id=_get_cid, get_conversation=_get_conv),
        persona_manager=SimpleNamespace(resolve_selected_persona=_resolve_persona),
        get_config=lambda **_kw: {"provider_settings": {}},
    )
    avail_host = await ctrl._probe_world_availability(rt)
    assert avail_host == "asleep"
    assert captured_scopes == [
        {
            "schema_version": 1,
            "platform": "aiocqhttp",
            "umo": rt.umo,
            "persona_id": "anan_host",
            "instance_id": "anan_host",
        }
    ]

    # 3) No concrete persona resolvable -> returns "unmanaged" cleanly without calling get_snapshot.
    captured_scopes.clear()
    plugin.context = None
    plugin.narrative = _StrictNarrative(worldbook_result=None)
    avail_none = await ctrl._probe_world_availability(rt)
    assert avail_none == "unmanaged"
    assert captured_scopes == []

    # 4) Bundle present with 4-field scope -> normalized to include schema_version=1.
    captured_scopes.clear()
    bundle = SimpleNamespace(
        scope={
            "platform": "aiocqhttp",
            "umo": rt.umo,
            "persona_id": "anan_bundle",
            "instance_id": "anan_bundle",
        },
        snapshot={"availability": "available", "stale": False},
    )
    plugin.narrative = _StrictNarrative(worldbook_result=None, bundles={rt.umo: bundle})
    avail_bundle = await ctrl._probe_world_availability(rt)
    assert avail_bundle == "asleep"
    assert captured_scopes[0]["schema_version"] == 1
    assert captured_scopes[0]["persona_id"] == "anan_bundle"
    await ctrl.stop()


@pytest.mark.asyncio
async def test_sleep_private_repeated_messages_wake_up_merge_and_no_duplicate_catchup():
    """Private chat during world_asleep: <3 msgs defer; 3rd msg within 90s wakes, merges prior pending msgs, and prevents duplicate catch-up."""
    clock = VirtualClock(initial_time=120_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:FriendMessage:u_sleep1", scope_kind="private", group_id="")

    catchup_sent: List[Any] = []

    async def fake_catchup(_rt, item):
        catchup_sent.append(item)
        return {"status": "sent", "message_id": f"cu_{item.primary_msg_id}"}

    ctrl = NaturalRhythmController(plugin, rng=random.Random(12), catchup_executor=fake_catchup)
    plugin.natural_rhythm = ctrl

    ev1 = SimpleNamespace(tag="ev1")
    ev2 = SimpleNamespace(tag="ev2")

    n1 = _add_msg(rt, clock, "sm1", "u_sleep1", "安安?")
    d1 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="sm1",
        user_id="u_sleep1",
        text=n1.text,
        private_direct=True,
        directed=True,
        availability="asleep",
        persona_fingerprint="p_anan",
        node=n1,
        live_event=ev1,
    )
    assert d1.defer is True
    assert d1.reason_code == "rhythm_deferred_asleep"
    assert d1.woken_from_sleep is False
    assert d1.sleep_wake_note == ""

    await clock.advance(20.0)
    n2 = _add_msg(rt, clock, "sm2", "u_sleep1", "还醒着么")
    d2 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="sm2",
        user_id="u_sleep1",
        text=n2.text,
        private_direct=True,
        directed=True,
        availability="asleep",
        persona_fingerprint="p_anan",
        node=n2,
        live_event=ev2,
    )
    assert d2.defer is True
    assert d2.reason_code == "rhythm_deferred_asleep"

    # 3rd message within 90s -> breaks through & merges sm1, sm2 chronologically!
    await clock.advance(20.0)
    n3 = _add_msg(rt, clock, "sm3", "u_sleep1", "喵喵喵")
    d3 = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="sm3",
        user_id="u_sleep1",
        text=n3.text,
        private_direct=True,
        directed=True,
        availability="asleep",
        persona_fingerprint="p_anan",
        node=n3,
    )
    assert d3.defer is False
    assert d3.away_kind == "world_asleep"
    assert d3.reason_code == "rhythm_sleep_wake_breakthrough"
    assert d3.woken_from_sleep is True
    assert d3.temp_awake_active is True
    assert "睡觉" in d3.sleep_wake_note and "吵醒" in d3.sleep_wake_note
    assert d3.merged_pending_ids == ("sm1", "sm2")
    assert d3.merged_pending_texts == ("安安?", "还醒着么")
    assert len(d3.merged_pending_events) == 2
    assert [getattr(e, "tag", "") for e in d3.merged_pending_events] == ["ev1", "ev2"]

    # Later when morning comes and availability turns available, catch-up MUST NOT re-send sm1/sm2/sm3.
    await clock.advance(1800.0)
    ctrl.on_availability_changed(rt, "available")
    sent = await ctrl.run_catchup(rt)
    assert sent == 0
    assert catchup_sent == []
    await ctrl.stop()


@pytest.mark.asyncio
async def test_sleep_private_90s_window_expiry_and_poke_wakeup():
    """Single poke defers; 2 pokes within 90s wake & merge prior messages; pings outside 90s do not accumulate."""
    clock = VirtualClock(initial_time=130_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:FriendMessage:u_poke", scope_kind="private", group_id="")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(13))
    plugin.natural_rhythm = ctrl

    # 1) Messages spread > 90s apart do not reach threshold of 3 within 90s.
    m1 = _add_msg(rt, clock, "sp1", "u_poke", "安安在吗")
    assert ctrl.evaluate_ingress_pacing(
        rt, msg_id="sp1", user_id="u_poke", text=m1.text, private_direct=True, directed=True, availability="asleep", node=m1
    ).defer is True

    await clock.advance(60.0)
    m2 = _add_msg(rt, clock, "sp2", "u_poke", "睡了吗")
    assert ctrl.evaluate_ingress_pacing(
        rt, msg_id="sp2", user_id="u_poke", text=m2.text, private_direct=True, directed=True, availability="asleep", node=m2
    ).defer is True

    await clock.advance(40.0)  # sp1 is now 100s old (>90s) -> only 2 msgs in window -> still defers!
    m3 = _add_msg(rt, clock, "sp3", "u_poke", "好吧晚安")
    d3 = ctrl.evaluate_ingress_pacing(
        rt, msg_id="sp3", user_id="u_poke", text=m3.text, private_direct=True, directed=True, availability="asleep", node=m3
    )
    assert d3.defer is True

    # 2) Separate private session: single poke defers, 2nd poke within 90s wakes & merges prior text + first poke.
    rt2 = plugin.make_session("aiocqhttp:FriendMessage:u_poke2", scope_kind="private", group_id="")
    t1 = _add_msg(rt2, clock, "pk_t1", "u_poke2", "安安?")
    assert ctrl.evaluate_ingress_pacing(
        rt2, msg_id="pk_t1", user_id="u_poke2", text=t1.text, private_direct=True, directed=True, availability="asleep", node=t1
    ).defer is True

    await clock.advance(10.0)
    p1 = _add_msg(rt2, clock, "pk_1", "u_poke2", "[戳了你一下]")
    p1.metadata["has_poke"] = True
    dp1 = ctrl.evaluate_ingress_pacing(
        rt2, msg_id="pk_1", user_id="u_poke2", text=p1.text, private_direct=True, directed=True, availability="asleep", node=p1
    )
    assert dp1.defer is True
    assert dp1.reason_code == "rhythm_deferred_asleep"

    await clock.advance(15.0)
    p2 = _add_msg(rt2, clock, "pk_2", "u_poke2", "[戳了你一下]")
    dp2 = ctrl.evaluate_ingress_pacing(
        rt2,
        msg_id="pk_2",
        user_id="u_poke2",
        text=p2.text,
        private_direct=True,
        directed=True,
        availability="asleep",
        node=p2,
        is_poke=True,
    )
    assert dp2.defer is False
    assert dp2.woken_from_sleep is True
    assert dp2.temp_awake_active is True
    assert dp2.merged_pending_ids == ("pk_t1", "pk_1")
    assert dp2.merged_pending_texts == ("安安?", "[戳了你一下]")
    await ctrl.stop()


@pytest.mark.asyncio
async def test_sleep_group_requires_directed_pings_or_pokes_at_bot_and_isolates_users():
    """Group sleep wake-up ignores undirected chatter and peer pokes, tracks per-user directed @/pokes-at-bot, and merges only that user's pending items."""
    clock = VirtualClock(initial_time=140_000.0)
    plugin = _FakePlugin(clock)
    rt = plugin.make_session("aiocqhttp:GroupMessage:g_sleep", group_id="g_sleep")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(14))
    plugin.natural_rhythm = ctrl

    # 1) Undirected group messages (even 4 from same user) NEVER wake bot during asleep.
    for idx in range(4):
        n = _add_msg(rt, clock, f"ug_{idx}", "u_chat", f"夜聊消息 {idx}")
        dec = ctrl.evaluate_ingress_pacing(
            rt, msg_id=n.msg_id, user_id="u_chat", text=n.text, directed=False, private_direct=False, availability="asleep", node=n
        )
        assert dec.defer is True
        await clock.advance(5.0)

    # 2) Peer pokes (A pokes B, wake_kind="poke_peer") NEVER wake bot.
    for idx in range(3):
        n = _add_msg(rt, clock, f"pp_{idx}", "u_peer", "戳了别人一下")
        n.metadata["has_poke"] = True
        n.metadata["poke_at_bot"] = False
        dec = ctrl.evaluate_ingress_pacing(
            rt,
            msg_id=n.msg_id,
            user_id="u_peer",
            text=n.text,
            wake_kind="poke_peer",
            directed=False,
            private_direct=False,
            availability="asleep",
            node=n,
        )
        assert dec.defer is True
        await clock.advance(5.0)

    # 3) Per-user isolation: u_a sends 2 @bot, u_b sends 2 @bot -> neither reaches 3 yet.
    for uid in ("u_a", "u_b"):
        for idx in (1, 2):
            n = _add_msg(rt, clock, f"{uid}_{idx}", uid, f"@安安 叫你第{idx}次", mentions=["bot_anan"], is_wake=True)
            dec = ctrl.evaluate_ingress_pacing(
                rt, msg_id=n.msg_id, user_id=uid, text=n.text, wake_kind="at", directed=True, private_direct=False, availability="asleep", node=n
            )
            assert dec.defer is True
            await clock.advance(5.0)

    # u_a sends 3rd @bot within 90s -> WAKES UP and merges ONLY u_a's pending items (u_b remains pending!).
    na3 = _add_msg(rt, clock, "u_a_3", "u_a", "@安安 快醒醒呀", mentions=["bot_anan"], is_wake=True)
    da3 = ctrl.evaluate_ingress_pacing(
        rt, msg_id="u_a_3", user_id="u_a", text=na3.text, wake_kind="at", directed=True, private_direct=False, availability="asleep", node=na3
    )
    assert da3.defer is False
    assert da3.woken_from_sleep is True
    assert da3.merged_pending_ids == ("u_a_1", "u_a_2")
    assert da3.merged_pending_texts == ("@安安 叫你第1次", "@安安 叫你第2次")
    remaining_ids = {c.msg_id for c in ctrl._book(rt.session_key).pending_items if c.status == "pending"}
    assert "u_b_1" in remaining_ids and "u_b_2" in remaining_ids
    assert "u_a_1" not in remaining_ids and "u_a_2" not in remaining_ids

    # 4) Group poke-at-bot: 1st defers, 2nd within 90s wakes.
    rt2 = plugin.make_session("aiocqhttp:GroupMessage:g_sleep_poke", group_id="g_sleep_poke")
    gp1 = _add_msg(rt2, clock, "gp1", "u_gp", "[戳了你一下]", is_wake=True)
    gp1.metadata["has_poke"] = True
    gp1.metadata["poke_at_bot"] = True
    assert ctrl.evaluate_ingress_pacing(
        rt2, msg_id="gp1", user_id="u_gp", text=gp1.text, wake_kind="poke", directed=True, private_direct=False, availability="asleep", node=gp1
    ).defer is True

    await clock.advance(15.0)
    gp2 = _add_msg(rt2, clock, "gp2", "u_gp", "[戳了你一下]", is_wake=True)
    gp2.metadata["has_poke"] = True
    gp2.metadata["poke_at_bot"] = True
    dgp2 = ctrl.evaluate_ingress_pacing(
        rt2, msg_id="gp2", user_id="u_gp", text=gp2.text, wake_kind="poke", directed=True, private_direct=False, availability="asleep", node=gp2
    )
    assert dgp2.defer is False
    assert dgp2.woken_from_sleep is True
    assert dgp2.merged_pending_ids == ("gp1",)
    await ctrl.stop()


@pytest.mark.asyncio
async def test_sleep_temp_awake_window_refresh_expiry_isolation_and_note():
    """Temporary awake window is per-session (12 min), refreshes on messages/replies, emits sleep_wake_note, and falls back to asleep after 12m idle."""
    clock = VirtualClock(initial_time=150_000.0)
    plugin = _FakePlugin(clock)
    rt1 = plugin.make_session("aiocqhttp:FriendMessage:u_ta1", scope_kind="private", group_id="")
    rt2 = plugin.make_session("aiocqhttp:FriendMessage:u_ta2", scope_kind="private", group_id="")
    ctrl = NaturalRhythmController(plugin, rng=random.Random(15))
    plugin.natural_rhythm = ctrl

    # Wake rt1 via urgent text during asleep.
    u1 = _add_msg(rt1, clock, "ta_m1", "u_ta1", "急事！快看一下这个")
    d1 = ctrl.evaluate_ingress_pacing(
        rt1, msg_id="ta_m1", user_id="u_ta1", text=u1.text, private_direct=True, directed=True, availability="asleep", node=u1
    )
    assert d1.defer is False
    assert d1.urgent_breakthrough is True
    assert d1.woken_from_sleep is True
    assert d1.temp_awake_active is True
    assert d1.sleep_wake_note != ""
    assert ctrl.is_temp_awake(rt1) is True
    assert ctrl.get_sleep_wake_note(rt1) != ""

    # Per-session isolation: rt2 MUST stay asleep (no global dynamics_world mutation).
    assert ctrl.is_temp_awake(rt2) is False
    assert ctrl.get_sleep_wake_note(rt2) == ""
    other_n = _add_msg(rt2, clock, "ta_other", "u_ta2", "睡了吗")
    d_other = ctrl.evaluate_ingress_pacing(
        rt2, msg_id="ta_other", user_id="u_ta2", text=other_n.text, private_direct=True, directed=True, availability="asleep", node=other_n
    )
    assert d_other.defer is True
    assert d_other.reason_code == "rhythm_deferred_asleep"

    # 6 minutes later -> normal single message in rt1 passes through & refreshes 12m window.
    await clock.advance(360.0)
    u2 = _add_msg(rt1, clock, "ta_m2", "u_ta1", "那明天早上再说吧")
    d2 = ctrl.evaluate_ingress_pacing(
        rt1, msg_id="ta_m2", user_id="u_ta1", text=u2.text, private_direct=True, directed=True, availability="asleep", node=u2
    )
    assert d2.defer is False
    assert d2.woken_from_sleep is False
    assert d2.temp_awake_active is True
    assert d2.reason_code == "rhythm_sleep_temp_awake"
    assert "睡觉" in d2.sleep_wake_note

    # Bot reply 5s later refreshes window again via on_turn_delivered.
    await clock.advance(5.0)
    ctrl.on_turn_delivered(
        rt1,
        anchor_bot_msg_id="bot_ta1",
        trigger_msg_id="ta_m2",
        target_user_id="u_ta1",
        anchor_topic_id="",
        delivered_text="唔...好，明天再看",
        persona_fingerprint="p_anan",
        private_direct=True,
        all_fragments_delivered=True,
    )

    # 10 minutes after reply (< 12 min) -> still temp-awake!
    await clock.advance(600.0)
    assert ctrl.is_temp_awake(rt1) is True
    assert ctrl.get_sleep_wake_note(rt1) != ""
    assert ctrl.get_sleep_wake_note(rt1, availability="available") == ""

    # 2 minutes + 5 seconds later (> 12 min idle since last message/reply) -> naturally falls back to asleep!
    await clock.advance(125.0)
    assert ctrl.is_temp_awake(rt1) is False
    assert ctrl.get_sleep_wake_note(rt1) == ""
    u3 = _add_msg(rt1, clock, "ta_m3", "u_ta1", "顺便一提")
    d3 = ctrl.evaluate_ingress_pacing(
        rt1, msg_id="ta_m3", user_id="u_ta1", text=u3.text, private_direct=True, directed=True, availability="asleep", node=u3
    )
    assert d3.defer is True
    assert d3.reason_code == "rhythm_deferred_asleep"
    assert d3.temp_awake_active is False
    assert d3.sleep_wake_note == ""

    # Group chat: undirected ambient chatter during temp-awake passes through without resetting the 12-min timer.
    rt_grp = plugin.make_session("aiocqhttp:GroupMessage:g_ta_guard", group_id="g_ta_guard")
    gw = _add_msg(rt_grp, clock, "gta_wake", "u_g1", "@安安 急事！快看一下", mentions=["bot_anan"], is_wake=True)
    dgw = ctrl.evaluate_ingress_pacing(
        rt_grp,
        msg_id="gta_wake",
        user_id="u_g1",
        text=gw.text,
        wake_kind="at",
        directed=True,
        private_direct=False,
        availability="asleep",
        node=gw,
    )
    assert dgw.defer is False and dgw.woken_from_sleep is True
    initial_deadline = ctrl._book(rt_grp.session_key).temp_awake_until_mono

    await clock.advance(360.0)
    g_amb = _add_msg(rt_grp, clock, "gta_amb", "u_other", "大家还在熬夜聊天啊")
    d_amb = ctrl.evaluate_ingress_pacing(
        rt_grp,
        msg_id="gta_amb",
        user_id="u_other",
        text=g_amb.text,
        directed=False,
        private_direct=False,
        availability="asleep",
        node=g_amb,
    )
    assert d_amb.defer is False and d_amb.temp_awake_active is True
    # Timer MUST NOT have been extended by undirected ambient chatter!
    assert ctrl._book(rt_grp.session_key).temp_awake_until_mono == initial_deadline

    # Advance remaining 6 min + 1s (total 12m01s since wake) -> expired and falls back to asleep!
    await clock.advance(361.0)
    assert ctrl.is_temp_awake(rt_grp) is False
    g_after = _add_msg(rt_grp, clock, "gta_after", "u_g1", "@安安 还在吗", mentions=["bot_anan"], is_wake=True)
    d_after = ctrl.evaluate_ingress_pacing(
        rt_grp,
        msg_id="gta_after",
        user_id="u_g1",
        text=g_after.text,
        wake_kind="at",
        directed=True,
        private_direct=False,
        availability="asleep",
        node=g_after,
    )
    assert d_after.defer is True
    assert d_after.reason_code == "rhythm_deferred_asleep"
    await ctrl.stop()




