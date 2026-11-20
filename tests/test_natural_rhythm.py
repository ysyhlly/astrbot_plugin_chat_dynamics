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

