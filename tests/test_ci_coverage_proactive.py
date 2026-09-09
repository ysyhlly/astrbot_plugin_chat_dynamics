"""Behavioral coverage for core/useful_proactive.py.

Exercises cfg coercion, node timestamp parsing, filler/question detection,
topic segmentation + per-topic quota reset, pace hints, newcomer caution,
help-followup gaps, cold memory nudge, and graceful degradation when the
group memory backend raises or returns unexpected shapes.
"""

import time
from types import SimpleNamespace as NS

from astrbot_plugin_chat_dynamics.core import useful_proactive as up
from astrbot_plugin_chat_dynamics.core.useful_proactive import UsefulProactiveGate

# Fixed noon stamp so every quota/help window in this file shares one hour bucket.
BASE = time.mktime((2026, 9, 9, 12, 0, 0, 0, 0, -1))


def _node(text, user_id="u", ts=None):
    return NS(text=text, user_id=user_id, timestamp=ts if ts is not None else BASE)


def _seed_speaker(gate, sid, uid, *, presence="sensible", now=BASE, count=4):
    for i in range(count):
        gate.evaluate(
            session_id=sid,
            user_id=uid,
            text=f"seed{i}",
            explicit=True,
            presence_knob=presence,
            now=now + i,
        )


def _ambient(gate, sid, uid, *, now, nodes=(), bot_id="bot", presence="sensible",
             group_memory=None, text="（旁听）", telemetrics=None, cfg=None):
    return gate.evaluate(
        session_id=sid,
        user_id=uid,
        text=text,
        explicit=False,
        presence_knob=presence,
        now=now,
        recent_nodes=nodes,
        bot_id=bot_id,
        group_memory=group_memory,
        public_memory_snippet="",
        telemetrics=telemetrics,
        cfg=cfg,
    )


def test_cfg_int_falls_back_for_none_missing_and_unparseable():
    assert up._cfg_int(None, "any", 7) == 7
    assert up._cfg_int(NS(), "missing_key", 7) == 7
    assert up._cfg_int(NS(quota="abc"), "quota", 7) == 7  # ValueError
    assert up._cfg_int(NS(quota=["x"]), "quota", 7) == 7  # TypeError
    assert up._cfg_int(NS(quota=None), "quota", 7) == 7
    assert up._cfg_int(NS(quota="3"), "quota", 7) == 3


def test_node_ts_reads_dict_nodes_and_tolerates_garbage_values():
    assert up._node_ts({"timestamp": 42.5}) == 42.5
    assert up._node_ts({"ts": 7}) == 7.0
    assert up._node_ts(NS(timestamp="boom", ts=object(), created_at="x", time="y")) == 0.0
    assert up._node_ts(NS()) == 0.0
    assert up._node_ts({"timestamp": "not-a-time"}) == 0.0


def test_question_and_pass_through_filler_edges():
    assert up._is_question("") is False
    assert up._is_question("   ") is False
    assert up._is_question("还在吗？") is True
    assert up._is_pass_through_tip("") is True
    assert up._is_pass_through_tip("。。。！？~") is True
    assert up._is_pass_through_tip("哈哈") is True
    assert up._is_pass_through_tip("6") is True
    assert up._is_pass_through_tip("好") is True
    assert up._is_pass_through_tip("你好吗？") is False


def test_touch_topic_switches_segment_and_resets_topic_quota():
    gate = UsefulProactiveGate()
    _seed_speaker(gate, "quota", "q1")

    gate.touch_topic("quota", "t1")
    assert gate._topic_seg == {"quota": "t1"}
    assert gate._topic_counts["quota"]["t1"] == 0

    # First gap-fill of the hour is allowed under topic t1…
    v1 = _ambient(gate, "quota", "q1", now=BASE, nodes=[_node("还有人在吗？", ts=BASE - 100)])
    assert v1.allow is True
    assert v1.reason_code == "gap_fill_ok"
    assert v1.proactive is True
    gate.note_proactive("quota", gap_fingerprint=v1.gap_fingerprint, now=BASE + 2)
    assert gate._topic_counts["quota"]["t1"] == 1

    # …hour quota remains (1/2) but topic quota t1 is spent.
    v2 = _ambient(gate, "quota", "q1", now=BASE + 5, nodes=[_node("有谁懂这个报错？", ts=BASE - 100)])
    assert v2.allow is False
    assert v2.reason_code == "proactive_quota"
    assert v2.quota_used == 1
    assert v2.quota_cap == 2

    # Same topic id again: segment and counts untouched.
    gate.touch_topic("quota", "t1")
    assert gate._topic_seg["quota"] == "t1"
    assert gate._topic_counts["quota"]["t1"] == 1

    # Moving to a fresh topic re-opens the topic quota while hour is still free.
    gate.touch_topic("quota", "t2")
    assert gate._topic_seg["quota"] == "t2"
    assert gate._topic_counts["quota"] == {"t1": 1, "t2": 0}
    v3 = _ambient(gate, "quota", "q1", now=BASE + 10, nodes=[_node("今晚有空吗？", ts=BASE - 100)])
    assert v3.allow is True
    assert v3.reason_code == "gap_fill_ok"
    assert v3.proactive is True

    # Default topic id falls back to the current hour bucket.
    gate.touch_topic("quota-other")
    assert gate._topic_seg["quota-other"] == time.strftime("%Y%m%d%H")
    assert gate._topic_counts["quota-other"][time.strftime("%Y%m%d%H")] == 0


def test_pace_hints_off_returns_neutral_and_fast_room_shortens():
    gate = UsefulProactiveGate()

    def verdict(cfg, mpm):
        return gate.evaluate(
            session_id="pace", user_id="p1", text="嗯", explicit=True,
            telemetrics=NS(mpm=mpm), cfg=cfg, now=BASE,
        )

    slow = verdict(NS(pace_align_enabled=False), mpm=99.0)
    assert slow.delay_scale == 1.0
    assert slow.length_hint == "normal"

    fast = verdict(NS(pace_align_enabled=True), mpm=12.0)
    assert fast.delay_scale == 0.85
    assert fast.length_hint == "brief"

    chilled = verdict(NS(pace_align_enabled=True), mpm=1.5)
    assert chilled.delay_scale == 1.35
    assert chilled.length_hint == "brief"


def test_help_followup_gap_after_silence_and_bot_spoke_since_branches():
    gate = UsefulProactiveGate()
    _seed_speaker(gate, "hf", "h1")

    def set_help(stamp):
        _ambient(gate, "hf", "h1", now=stamp, text="这个报错求助一下", nodes=())

    # Bot spoke after the help → no help-followup gap (also: bot node stops hang scan).
    set_help(BASE + 100)
    v_bot_replied = _ambient(
        gate, "hf", "h1", now=BASE + 220,
        nodes=[_node("这个问题我来解决吧", user_id="bot", ts=BASE + 150)],
    )
    assert v_bot_replied.allow is False
    assert v_bot_replied.reason_code == "no_gap"

    # No traffic at all since help → help-followup gap (empty recent nodes).
    set_help(BASE + 300)
    v_empty = _ambient(gate, "hf", "h1", now=BASE + 420, nodes=())
    assert v_empty.allow is True
    assert v_empty.gap_kind == "help_followup"
    assert v_empty.proactive is True

    # Unknown bot id → _bot_spoke_since bails out early, gap still fires.
    set_help(BASE + 500)
    v_no_bot = _ambient(gate, "hf", "h1", now=BASE + 620, nodes=(), bot_id="")
    assert v_no_bot.allow is True
    assert v_no_bot.gap_kind == "help_followup"

    # Bot node older than the help → does not count as a reply, gap fires.
    set_help(BASE + 700)
    v_old_bot = _ambient(
        gate, "hf", "h1", now=BASE + 820,
        nodes=[_node("已回复", user_id="bot", ts=BASE + 50)],
    )
    assert v_old_bot.allow is True
    assert v_old_bot.gap_kind == "help_followup"

    # Human chatter after help → walk continues, bot never seen, gap fires.
    set_help(BASE + 900)
    v_human = _ambient(
        gate, "hf", "h1", now=BASE + 1020,
        nodes=[_node("随便聊聊", user_id="z", ts=BASE + 50)],
    )
    assert v_human.allow is True
    assert v_human.gap_kind == "help_followup"


def test_cold_nudge_memory_line_and_its_degradations():
    gate = UsefulProactiveGate()
    _seed_speaker(gate, "cm", "c1", presence="lively")

    def raiser(_sid):
        raise RuntimeError("memory down")

    def chilly(memory):
        return _ambient(
            gate, "cm", "c1", now=BASE + 10, presence="lively",
            group_memory=memory, telemetrics=NS(mpm=1.0),
        )

    # list_all raising inside the cold-nudge probe: degrades to no gap, lively allows.
    boom = chilly(NS(list_all=raiser))
    assert boom.allow is True
    assert boom.reason_code == "ok"
    assert boom.proactive is False

    # Non-dict payload from memory: both probes degrade.
    listed = chilly(NS(list_all=lambda _sid: []))
    assert listed.allow is True
    assert listed.proactive is False

    # Muted notebook suppresses both probes.
    muted = chilly(NS(list_all=lambda _sid: {"mute_until": 1e18, "reminders": [], "anniversaries": []}))
    assert muted.allow is True
    assert muted.proactive is False

    # Anniversary without title falls through to the first live reminder line.
    rows = {
        "mute_until": 0,
        "anniversaries": [{"title": "  "}],
        "reminders": [
            {"id": "e1", "text": "旧提醒", "expired": True, "nudged": False},
            {"id": "e2", "text": "周末聚餐", "expired": False, "nudged": False},
        ],
    }
    nudge = chilly(NS(list_all=lambda _sid: rows))
    assert nudge.allow is True
    assert nudge.gap_kind == "cold_memory_nudge"
    assert nudge.reason_code == "cold_nudge_ok"
    assert nudge.proactive is True


def test_appointment_gap_memory_row_skips():
    gate = UsefulProactiveGate()
    _seed_speaker(gate, "apt", "a1")

    # Rows that are expired or have empty text never produce an appointment gap.
    skip_rows = _ambient(
        gate, "apt", "a1", now=BASE + 10,
        group_memory=NS(list_all=lambda _sid: {
            "mute_until": 0,
            "reminders": [
                {"id": "e1", "text": "约一下", "expired": True, "nudged": False},
                {"id": "e2", "text": "", "expired": False, "nudged": False},
            ],
        }),
    )
    assert skip_rows.allow is False
    assert skip_rows.reason_code == "no_gap"

    # Reminder without any appointment hint is not a gap either.
    no_hint = _ambient(
        gate, "apt", "a1", now=BASE + 20,
        group_memory=NS(list_all=lambda _sid: {
            "mute_until": 0,
            "reminders": [{"id": "x", "text": "今天天气真不错", "expired": False, "nudged": False}],
        }),
    )
    assert no_hint.allow is False
    assert no_hint.reason_code == "no_gap"

    # Muted notebook: appointment probe is suppressed entirely.
    muted = _ambient(
        gate, "apt", "a1", now=BASE + 30,
        group_memory=NS(list_all=lambda _sid: {"mute_until": 1e18, "reminders": []}),
    )
    assert muted.allow is False
    assert muted.reason_code == "no_gap"

    # Non-dict notebook payload degrades instead of raising.
    weird = _ambient(
        gate, "apt", "a1", now=BASE + 40,
        group_memory=NS(list_all=lambda _sid: ["not", "a", "dict"]),
    )
    assert weird.allow is False
    assert weird.reason_code == "no_gap"


def test_newcomer_denied_even_with_appointment_gap():
    gate = UsefulProactiveGate()
    # First-ever ambient turn for this user (speak count 1) + an appointment gap:
    # gap kind is not naming-safe, so newcomer caution must deny.
    v = gate.evaluate(
        session_id="nc1",
        user_id="newbie",
        text="（旁听）",
        explicit=False,
        presence_knob="sensible",
        now=BASE,
        recent_nodes=(),
        bot_id="bot",
        group_memory=NS(list_all=lambda _sid: {
            "mute_until": 0,
            "reminders": [{"id": "r1", "text": "约见面吃饭", "expired": False, "nudged": False}],
        }),
    )
    assert v.allow is False
    assert v.reason_code == "newcomer_caution"
    assert "新人" in v.reason_zh
    assert v.force_scale == 0.2
    assert v.delay_scale == 1.25
