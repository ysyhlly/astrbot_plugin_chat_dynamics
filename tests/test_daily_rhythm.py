"""v1.3.3 daily rhythm: PRD acceptance scripts 1–6 + config/status."""

from __future__ import annotations

import time
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.daily_rhythm import (
    STATE_ASLEEP_AFTER_WIND,
    STATE_ASLEEP_SELF,
    STATE_AWAKE,
    STATE_BRIEF_WAKE,
    STATE_WINDING_DOWN,
    DailyRhythmGate,
)
from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode


def _cfg(**overrides):
    cfg, _ = parse_runtime_config({})
    if not overrides:
        return cfg
    data = cfg.__dict__.copy()
    data.update(overrides)
    return SimpleNamespace(**data)


def _node(text, user_id="u1", ts=None, reply_to="", mentions=None, msg_id="m"):
    return SimpleNamespace(
        msg_id=msg_id,
        user_id=user_id,
        reply_to_id=reply_to,
        mentioned_users=list(mentions or []),
        text=text,
        timestamp=ts if ts is not None else time.time(),
    )


def _tele(mpm=1.0):
    return SimpleNamespace(mpm=mpm, scene_tags=(), emotion_tags=())


def _stamp_at(hour: int, *, minute: int = 0) -> float:
    """Local-time stamp on a fixed date so hour-of-day tests stay stable."""
    return time.mktime((2026, 3, 15, hour, minute, 0, 0, 0, -1))


def test_config_v133_defaults():
    cfg, warnings = parse_runtime_config({})
    assert cfg.daily_rhythm_enabled is True
    assert cfg.rhythm_morning_hi_enabled is True
    assert cfg.rhythm_day_share_slots == 1
    assert cfg.rhythm_goodnight_text_quota == 1
    assert cfg.rhythm_sleep_after_winddown is True
    assert cfg.rhythm_allow_self_sleep is True
    assert cfg.rhythm_allow_wake is True
    assert cfg.rhythm_insomnia_enabled is False
    assert cfg.rhythm_force_sleep is False
    assert cfg.rhythm_skip_morning_hi_tonight is False
    assert not any("rhythm" in w for w in warnings)


def test_script1_morning_hi_ok_but_not_while_deciding():
    """早到可早安；决策中不早安."""
    gate = DynamicsDecisionGate()
    cfg = _cfg(presence_knob="lively")
    # Morning band: use a stamp whose local hour is 8 if possible; else force via text.
    # Build a local-morning timestamp for today.
    now_struct = list(time.localtime(time.time()))
    now_struct[3] = 8  # hour
    now_struct[4] = 0
    morning = time.mktime(tuple(now_struct))

    hi = gate.evaluate(
        session_id="g1",
        user_id="u1",
        text="早啊各位",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        now=morning,
        recent_nodes=[_node("早啊", user_id="u2", ts=morning - 5)],
        telemetrics=_tele(2.0),
    )
    assert hi.rhythm is not None
    # May speak as morning_hi or pass ok; must not be denied for deciding.
    assert hi.reason_code != "morning_hi_skip"
    if hi.rhythm.action == "morning_hi":
        assert hi.should_speak is True

    # Deciding blocks morning proactive (and ambient banter).
    deciding = gate.evaluate(
        session_id="g2",
        user_id="u1",
        text="我们约几点见面？选个时间",
        vibe_mode=GroupChatMode.FAST_BANTER,
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        now=morning + 10,
        recent_nodes=[],
        telemetrics=_tele(3.0),
    )
    assert deciding.should_speak is False
    assert deciding.reason_code in {"deciding_no_banter", "occasion_silence"}
    assert deciding.rhythm is None or deciding.rhythm.action != "morning_hi"


def test_script2_ten_goodnights_quota():
    """十人连续晚安最多 1～2 次文字."""
    gate = DynamicsDecisionGate()
    cfg = _cfg(presence_knob="sensible", rhythm_goodnight_text_quota=1)
    now = 5_000_000.0
    spoke = 0
    for i in range(10):
        res = gate.evaluate(
            session_id="gn",
            user_id=f"u{i}",
            text="晚安～",
            explicit=False,
            willingness=0.9,
            cfg=cfg,
            now=now + i,
            recent_nodes=[_node("晚安", user_id=f"u{i}", ts=now + i)],
            telemetrics=_tele(4.0),
            bot_id="bot",
        )
        if res.should_speak:
            spoke += 1
            gate.note_spoke("gn", skin=res.skin, now=now + i, proactive=res.proactive, rhythm=res.rhythm)
        else:
            assert res.reason_code in {
                "wind_later_silence",
                "wind_goodnight_done",
                "asleep_plain_gn",
                "asleep_ambient",
            }
    assert 1 <= spoke <= 2


def test_allow_wake_false_blocks_explicit_mention():
    gate = DynamicsDecisionGate()
    cfg = _cfg(presence_knob="sensible", rhythm_allow_wake=False)
    now = _stamp_at(2)
    sess = gate.rhythm._ensure("nw", now)
    sess.state = STATE_ASLEEP_AFTER_WIND
    sess.asleep_since = now - 30 * 60
    res = gate.evaluate(
        session_id="nw",
        user_id="d",
        text="@bot 帮我看下报错",
        explicit=True,
        willingness=1.0,
        cfg=cfg,
        now=now,
        recent_nodes=[_node("@bot 帮我看下报错", user_id="d", ts=now, mentions=["bot"])],
        telemetrics=_tele(0.5),
        bot_id="bot",
    )
    assert res.should_speak is False
    assert res.rhythm is not None
    assert res.rhythm.allow is False
    assert res.reason_code != "asleep_wake_ok"


def test_overnight_sleep_wakes_in_morning_same_calendar_day():
    rhythm = DailyRhythmGate()
    cfg = _cfg()
    gn = _stamp_at(1)
    first = rhythm.evaluate(
        session_id="ov",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=gn,
        telemetrics=_tele(0.5),
        recent_nodes=[_node("晚安", ts=gn)],
    )
    assert first.state == STATE_WINDING_DOWN
    rhythm.note_spoke("ov", verdict=first, now=gn)
    asleep_at = gn + 50 * 60
    asleep = rhythm.evaluate(
        session_id="ov",
        user_id="b",
        text=".",
        cfg=cfg,
        now=asleep_at,
        telemetrics=_tele(0.2),
        recent_nodes=[_node(".", user_id="b", ts=asleep_at - 100)],
    )
    assert asleep.state == STATE_ASLEEP_AFTER_WIND
    morning = _stamp_at(8)
    hi = rhythm.evaluate(
        session_id="ov",
        user_id="c",
        text="早安",
        cfg=cfg,
        now=morning,
        telemetrics=_tele(1.5),
        recent_nodes=[_node("早安", user_id="c", ts=morning)],
    )
    assert hi.state == STATE_AWAKE
    assert hi.reason_code != "asleep_ambient"
    assert hi.allow is True


def test_reset_clears_rhythm_and_proactive_quota():
    gate = DynamicsDecisionGate()
    cfg = _cfg()
    now = _stamp_at(1)
    v = gate.rhythm.evaluate(
        session_id="rst",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=now,
        telemetrics=_tele(0.5),
        recent_nodes=[_node("晚安", ts=now)],
    )
    gate.rhythm.note_spoke("rst", verdict=v, now=now)
    gate.useful.note_proactive("rst", gap_fingerprint="gap-1", now=now)
    assert gate.rhythm.status("rst", now=now)["state"] == STATE_WINDING_DOWN
    assert gate.useful.quota_status("rst", now=now)["proactive_used"] >= 1
    gate.reset_session("rst")
    st = gate.rhythm.status("rst", now=now)
    assert st["state"] == STATE_AWAKE
    assert st["goodnight_text_used"] == 0
    assert gate.useful.quota_status("rst", now=now)["proactive_used"] == 0


def test_goodnight_quota_waits_for_note_spoke():
    """evaluate() must not burn the goodnight quota before a real send."""
    rhythm = DailyRhythmGate()
    cfg = _cfg(rhythm_goodnight_text_quota=1)
    now = 9_000_000.0
    first = rhythm.evaluate(
        session_id="q1",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=now,
        telemetrics=_tele(2.0),
        recent_nodes=[_node("晚安", ts=now)],
    )
    assert first.allow is True
    assert first.consume_goodnight_quota is True
    assert rhythm.status("q1", now=now)["goodnight_text_used"] == 0
    second = rhythm.evaluate(
        session_id="q1",
        user_id="b",
        text="晚安",
        cfg=cfg,
        now=now + 1,
        telemetrics=_tele(2.0),
        recent_nodes=[_node("晚安", user_id="b", ts=now + 1)],
    )
    assert second.allow is True
    rhythm.note_spoke("q1", verdict=first, now=now)
    assert rhythm.status("q1", now=now + 1)["goodnight_text_used"] == 1
    third = rhythm.evaluate(
        session_id="q1",
        user_id="c",
        text="晚安",
        cfg=cfg,
        now=now + 2,
        telemetrics=_tele(2.0),
        recent_nodes=[_node("晚安", user_id="c", ts=now + 2)],
    )
    assert third.allow is False


def test_script3_first_wave_stays_winding_not_asleep():
    """首波回完仍收束中，不立刻已睡."""
    gate = DynamicsDecisionGate()
    cfg = _cfg(presence_knob="sensible")
    now = 6_000_000.0
    res = gate.evaluate(
        session_id="w1",
        user_id="a",
        text="晚安大家",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        now=now,
        recent_nodes=[_node("晚安大家", user_id="a", ts=now)],
        telemetrics=_tele(3.0),
        bot_id="bot",
    )
    assert res.should_speak is True
    assert res.rhythm is not None
    assert res.rhythm.state == STATE_WINDING_DOWN
    assert res.rhythm.action == "goodnight_reply"
    gate.note_spoke("w1", skin=res.skin, now=now, rhythm=res.rhythm)
    st = gate.rhythm.status("w1", now=now + 1)
    assert st["state"] == STATE_WINDING_DOWN
    assert st["state"] != STATE_ASLEEP_AFTER_WIND


def test_script4_cold_then_asleep_hot_delays():
    """群冷/多数歇后再已睡；热聊推迟."""
    rhythm = DailyRhythmGate()
    cfg = _cfg()
    now = 7_000_000.0
    # Enter winding
    v1 = rhythm.evaluate(
        session_id="s4",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=now,
        telemetrics=_tele(2.0),
        recent_nodes=[_node("晚安", ts=now)],
    )
    assert v1.state == STATE_WINDING_DOWN
    rhythm.note_spoke("s4", verdict=v1, now=now)

    # Hot chat shortly after — still winding, delay reason
    hot = rhythm.evaluate(
        session_id="s4",
        user_id="b",
        text="再聊一会儿哈哈",
        cfg=cfg,
        now=now + 5 * 60,
        telemetrics=_tele(12.0),
        recent_nodes=[_node("哈哈哈", user_id="c", ts=now + 5 * 60 - 1) for _ in range(5)],
    )
    assert hot.state == STATE_WINDING_DOWN
    assert rhythm.status("s4", now=now + 5 * 60)["state"] == STATE_WINDING_DOWN

    # Advance past 40min with cold field → asleep
    cold_now = now + 45 * 60
    cold = rhythm.evaluate(
        session_id="s4",
        user_id="d",
        text="…",
        cfg=cfg,
        now=cold_now,
        telemetrics=_tele(0.5),
        recent_nodes=[_node("嗯", user_id="d", ts=cold_now - 600)],
    )
    assert cold.state == STATE_ASLEEP_AFTER_WIND
    assert cold.allow is False
    assert cold.proactive_blocked is True


def test_script5_asleep_plain_gn_no_wake_at_can_brief_wake():
    """已睡串晚安不吵醒；@可短醒一次再睡."""
    gate = DynamicsDecisionGate()
    cfg = _cfg(presence_knob="sensible", rhythm_allow_wake=True)
    now = 8_000_000.0
    # Force into asleep via rhythm internals
    r = gate.rhythm
    v = r.evaluate(
        session_id="s5", user_id="a", text="晚安", cfg=cfg, now=now, telemetrics=_tele(1.0),
        recent_nodes=[_node("晚安", ts=now)],
    )
    r.note_spoke("s5", verdict=v, now=now)
    # Jump time + force sleep tick
    asleep_at = now + 50 * 60
    r.evaluate(
        session_id="s5",
        user_id="b",
        text=".",
        cfg=cfg,
        now=asleep_at,
        telemetrics=_tele(0.2),
        recent_nodes=[_node(".", user_id="b", ts=asleep_at - 100)],
    )
    assert r.status("s5", now=asleep_at)["state"] == STATE_ASLEEP_AFTER_WIND

    plain = gate.evaluate(
        session_id="s5",
        user_id="c",
        text="晚安哦",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        now=asleep_at + 10,
        recent_nodes=[_node("晚安哦", user_id="c", ts=asleep_at + 10)],
        telemetrics=_tele(0.5),
        bot_id="bot",
    )
    assert plain.should_speak is False
    assert plain.reason_code in {"asleep_plain_gn", "asleep_ambient"}

    wake = gate.evaluate(
        session_id="s5",
        user_id="d",
        text="@bot 帮我看下报错",
        explicit=True,
        willingness=0.9,
        cfg=cfg,
        now=asleep_at + 20,
        recent_nodes=[_node("@bot 帮我看下报错", user_id="d", ts=asleep_at + 20, mentions=["bot"])],
        telemetrics=_tele(0.5),
        bot_id="bot",
    )
    assert wake.should_speak is True
    assert wake.rhythm is not None
    assert wake.rhythm.state == STATE_BRIEF_WAKE
    assert wake.rhythm.action == "wake_reply"
    gate.note_spoke("s5", skin=wake.skin, now=asleep_at + 20, rhythm=wake.rhythm)

    # After cooldown, back to sleep
    later = asleep_at + 20 + 11 * 60
    again = gate.evaluate(
        session_id="s5",
        user_id="e",
        text="随便聊聊",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        now=later,
        recent_nodes=[_node("随便聊聊", user_id="e", ts=later)],
        telemetrics=_tele(0.5),
        bot_id="bot",
    )
    assert again.should_speak is False
    assert r.status("s5", now=later)["state"] in {STATE_ASLEEP_AFTER_WIND, "asleep_self"}


def test_script6_insomnia_default_off_and_cap_when_on():
    """睡不着默认不出现；若开最多一句."""
    rhythm = DailyRhythmGate()
    cfg_off = _cfg(rhythm_insomnia_enabled=False)
    now = 9_000_000.0
    # Force late-night hour
    st = list(time.localtime(now))
    st[3] = 2
    late = time.mktime(tuple(st))
    off = rhythm.evaluate(
        session_id="ins1",
        user_id="x",
        text="……",
        cfg=cfg_off,
        now=late,
        presence_knob="lively",
        telemetrics=_tele(0.5),
        recent_nodes=[],
    )
    assert off.action != "insomnia_line"
    assert off.state != "insomniac"

    # When enabled: at most one line even if we force state
    cfg_on = _cfg(rhythm_insomnia_enabled=True)
    sess = rhythm._ensure("ins2", late)
    sess.state = "insomniac"
    sess.insomnia_lines_today = 1
    capped = rhythm.evaluate(
        session_id="ins2",
        user_id="x",
        text="……",
        cfg=cfg_on,
        now=late + 10,
        presence_knob="lively",
        telemetrics=_tele(0.5),
        recent_nodes=[],
    )
    assert capped.allow is False
    assert capped.reason_code == "insomnia_cap"


def test_hard_split_goodnight_never_jumps_to_asleep():
    rhythm = DailyRhythmGate()
    cfg = _cfg()
    now = 10_000_000.0
    v = rhythm.evaluate(
        session_id="hard",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=now,
        telemetrics=_tele(1.0),
        recent_nodes=[_node("晚安", ts=now)],
    )
    assert v.state == STATE_WINDING_DOWN
    assert v.state not in {STATE_ASLEEP_AFTER_WIND, "asleep_self"}


def test_status_and_why_silent_codes_readable():
    rhythm = DailyRhythmGate()
    cfg = _cfg()
    now = 11_000_000.0
    v = rhythm.evaluate(
        session_id="ui",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=now,
        telemetrics=_tele(1.0),
        recent_nodes=[_node("晚安", ts=now)],
    )
    rhythm.note_spoke("ui", verdict=v, now=now)
    # second wave silence
    rhythm.evaluate(
        session_id="ui",
        user_id="b",
        text="晚安呀",
        cfg=cfg,
        now=now + 30,
        telemetrics=_tele(1.0),
        recent_nodes=[_node("晚安呀", user_id="b", ts=now + 30)],
    )
    st = rhythm.status("ui", now=now + 30)
    assert st["state_zh"] in {"收束中", "还醒着", "已睡·收束后", "已睡·自己睡", "短醒·被吵醒", "失眠中"}
    rows = rhythm.why_silent_rows("ui")
    assert any(r.get("reason_zh") for r in rows)


def test_gate_priority_manners_before_rhythm():
    """Conflict/ghost still win before rhythm."""
    gate = DynamicsDecisionGate()
    cfg = _cfg(presence_knob="ghost")
    res = gate.evaluate(
        session_id="prio",
        user_id="a",
        text="晚安",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        now=12_000_000.0,
        recent_nodes=[],
    )
    assert res.should_speak is False
    assert res.reason_code == "presence_ghost"


def test_calendar_midnight_keeps_winding_until_morning():
    """23:50 晚安 must still be 收束中 after midnight, then wake in the morning."""
    rhythm = DailyRhythmGate()
    cfg = _cfg()
    gn = time.mktime((2026, 3, 15, 23, 50, 0, 0, 0, -1))
    first = rhythm.evaluate(
        session_id="mid",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=gn,
        telemetrics=_tele(0.5),
        recent_nodes=[_node("晚安", ts=gn)],
    )
    assert first.state == STATE_WINDING_DOWN
    rhythm.note_spoke("mid", verdict=first, now=gn)

    after_midnight = time.mktime((2026, 3, 16, 0, 5, 0, 0, 0, -1))
    still = rhythm.evaluate(
        session_id="mid",
        user_id="b",
        text="嗯",
        cfg=cfg,
        now=after_midnight,
        telemetrics=_tele(0.4),
        recent_nodes=[_node("嗯", user_id="b", ts=after_midnight)],
    )
    assert still.state == STATE_WINDING_DOWN
    assert rhythm.status("mid", now=after_midnight)["state"] == STATE_WINDING_DOWN

    asleep_at = time.mktime((2026, 3, 16, 0, 40, 0, 0, 0, -1))
    asleep = rhythm.evaluate(
        session_id="mid",
        user_id="c",
        text=".",
        cfg=cfg,
        now=asleep_at,
        telemetrics=_tele(0.2),
        recent_nodes=[_node(".", user_id="c", ts=asleep_at - 100)],
    )
    assert asleep.state == STATE_ASLEEP_AFTER_WIND

    morning = time.mktime((2026, 3, 16, 8, 0, 0, 0, 0, -1))
    hi = rhythm.evaluate(
        session_id="mid",
        user_id="d",
        text="早安",
        cfg=cfg,
        now=morning,
        telemetrics=_tele(1.5),
        recent_nodes=[_node("早安", user_id="d", ts=morning)],
    )
    assert hi.state == STATE_AWAKE
    assert hi.allow is True
    assert hi.reason_code != "asleep_ambient"


def test_wake_reply_count_waits_for_note_spoke():
    """evaluate() must not burn the daily wake count before a real send."""
    rhythm = DailyRhythmGate()
    cfg = _cfg(rhythm_allow_wake=True)
    now = _stamp_at(2)
    first = rhythm.evaluate(
        session_id="wk",
        user_id="a",
        text="晚安",
        cfg=cfg,
        now=now,
        telemetrics=_tele(0.5),
        recent_nodes=[_node("晚安", ts=now)],
    )
    rhythm.note_spoke("wk", verdict=first, now=now)
    asleep_at = now + 50 * 60
    rhythm.evaluate(
        session_id="wk",
        user_id="b",
        text=".",
        cfg=cfg,
        now=asleep_at,
        telemetrics=_tele(0.2),
        recent_nodes=[_node(".", user_id="b", ts=asleep_at - 100)],
    )
    assert rhythm.status("wk", now=asleep_at)["state"] == STATE_ASLEEP_AFTER_WIND

    wake = rhythm.evaluate(
        session_id="wk",
        user_id="d",
        text="@bot 帮我看下报错",
        explicit=True,
        cfg=cfg,
        now=asleep_at + 20,
        bot_id="bot",
        telemetrics=_tele(0.5),
        recent_nodes=[_node("@bot 帮我看下报错", user_id="d", ts=asleep_at + 20, mentions=["bot"])],
    )
    assert wake.allow is True
    assert wake.action == "wake_reply"
    assert wake.state == STATE_BRIEF_WAKE
    assert rhythm.status("wk", now=asleep_at + 20)["state"] == STATE_ASLEEP_AFTER_WIND
    assert rhythm.status("wk", now=asleep_at + 20)["wake_replies_today"] == 0
    rhythm.note_spoke("wk", verdict=wake, now=asleep_at + 20)
    assert rhythm.status("wk", now=asleep_at + 21)["state"] == STATE_BRIEF_WAKE
    assert rhythm.status("wk", now=asleep_at + 21)["wake_replies_today"] == 1


def test_system_clock_wall_time_is_civil():
    from astrbot_plugin_chat_dynamics.core.time_service import SystemClock

    clock = SystemClock()
    wall = clock.wall_time()
    assert abs(wall - time.time()) < 1.0
    assert wall >= 1_000_000_000
    assert clock.time() < 1_000_000_000 or abs(clock.time() - wall) > 60.0


def test_allow_self_sleep_at_night_when_cold():
    rhythm = DailyRhythmGate()
    cfg = _cfg(rhythm_allow_self_sleep=True)
    night = time.mktime((2026, 3, 15, 23, 30, 0, 0, 0, -1))
    v = rhythm.evaluate(
        session_id="ss",
        user_id="a",
        text="哈喽",
        explicit=False,
        cfg=cfg,
        now=night,
        telemetrics=_tele(0.4),
        recent_nodes=[_node("哈喽", user_id="a", ts=night)],
    )
    assert v.allow is False
    assert v.state == STATE_ASLEEP_SELF
    assert v.proactive_blocked is True

    off = DailyRhythmGate()
    cfg_off = _cfg(rhythm_allow_self_sleep=False)
    kept = off.evaluate(
        session_id="ss2",
        user_id="a",
        text="哈喽",
        explicit=False,
        cfg=cfg_off,
        now=night,
        telemetrics=_tele(0.4),
        recent_nodes=[_node("哈喽", user_id="a", ts=night)],
    )
    assert kept.state == STATE_AWAKE
    assert kept.allow is True
