"""Behavioral unit tests for core/daily_rhythm.py CI-coverage gaps.

These tests exercise the helper predicates and the DailyRhythmGate state
machine branches that the acceptance-script tests (test_daily_rhythm.py) do
not reach: dict-node accessors, note_spoke bookkeeping side effects, the
insomnia roll band, status aggregation and several defensive guards.
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace as NS

from astrbot_plugin_chat_dynamics.core import daily_rhythm as dr


def _stamp(hour: int, minute: int = 0, day: int = 15) -> float:
    """Local wall-clock stamp on 2026-03-{day} so hour checks stay stable."""
    return time.mktime((2026, 3, day, hour, minute, 0, 0, 0, -1))


def _node(text, user_id="u1", ts=None, mentions=None, msg_id="m1", reply_to_id=""):
    return NS(
        msg_id=msg_id,
        user_id=user_id,
        reply_to_id=reply_to_id,
        mentioned_users=list(mentions or []),
        text=text,
        timestamp=ts if ts is not None else _stamp(12),
    )


def _tele(mpm=1.0):
    return NS(mpm=mpm)


def _ok_verdict(**kw):
    return dr.DailyRhythmVerdict(True, "ok", dr.REASON["ok"], **kw)


def _sleep_sess(gate, sid, stamp, kind="after_wind"):
    """Put a session into an asleep state with a realistic asleep_since."""
    sess = gate._ensure(sid, stamp)
    sess.state = dr.STATE_ASLEEP_AFTER_WIND if kind == "after_wind" else dr.STATE_ASLEEP_SELF
    sess.asleep_since = stamp - 30 * 60
    sess.asleep_kind = kind
    return sess


# --- helper predicates -----------------------------------------------------


def test_node_text_prefers_text_and_falls_back_to_content():
    assert dr._node_text(NS(text="hello")) == "hello"
    assert dr._node_text(NS(text="", content="fallback")) == "fallback"
    assert dr._node_text(NS(content=None, text=None)) == ""
    assert dr._node_text(object()) == ""


def test_node_ts_reads_dict_keys_and_tolerates_bad_values():
    assert dr._node_ts({"timestamp": "123.5"}) == 123.5
    assert dr._node_ts({"time": 7.5}) == 7.5
    assert dr._node_ts({"ts": "not-a-number"}) == 0.0
    assert dr._node_ts(NS(timestamp="nope", ts=None)) == 0.0
    assert dr._node_ts(NS()) == 0.0


def test_goodnight_and_morning_empty_text_are_false():
    assert dr.is_goodnight_text("") is False
    assert dr.is_goodnight_text("   ") is False
    assert dr.is_goodnight_text(None) is False
    assert dr.is_goodnight_text("晚安啦") is True
    assert dr.is_morning_text("") is False
    assert dr.is_morning_text(None) is False
    assert dr.is_morning_text("早上好呀") is True


def test_mentions_bot_dict_nodes_mention_match_and_noise():
    # dict nodes carry mentioned_users but no usable user_id -> no match.
    assert (
        dr._mentions_bot("", [{"mentioned_users": ["bot"], "user_id": "u1"}], bot_id="bot", user_id="u1")
        is False
    )
    # object node whose author matches the caller.
    assert dr._mentions_bot("", [_node("hi", user_id="u1", mentions=["bot"])], bot_id="bot", user_id="u1") is True
    # wrong author must not match.
    assert dr._mentions_bot("", [_node("hi", user_id="u2", mentions=["bot"])], bot_id="bot", user_id="u1") is False
    # non-iterable mentions field must not blow up the scan.
    node = NS(mentioned_users=3, user_id="u1")
    assert dr._mentions_bot("", [node], bot_id="bot", user_id="u1") is False
    # crude "@bot" inside the raw text still counts.
    assert dr._mentions_bot("@bot 在吗", [], bot_id="bot", user_id="u1") is True
    assert dr._mentions_bot("@other 在吗", [], bot_id="bot", user_id="u1") is False


def test_quoted_bot_flag_reply_resolution_and_dict_tip():
    assert dr._quoted_bot([], bot_id="bot", quoted_bot=True) is True
    # tip replies to an earlier message authored by the bot.
    nodes = [
        _node("earlier", user_id="bot", ts=1.0, msg_id="m9"),
        _node("quote", user_id="u1", ts=2.0, msg_id="m10", reply_to_id="m9"),
    ]
    assert dr._quoted_bot(nodes, bot_id="bot", quoted_bot=False) is True
    # dict tip whose reply target does not belong to the bot.
    assert dr._quoted_bot([{"reply_to_id": "zz", "msg_id": "m1", "user_id": "u1"}], bot_id="bot", quoted_bot=False) is False
    # reply target absent from history.
    assert dr._quoted_bot([_node("x", msg_id="m1", reply_to_id="missing")], bot_id="bot", quoted_bot=False) is False


def test_looks_command_empty_prefix_and_punct():
    assert dr._looks_command("") is False
    assert dr._looks_command("   ") is False
    assert dr._looks_command("/status") is True
    assert dr._looks_command("!ping") is True
    assert dr._looks_command("！ping") is True
    assert dr._looks_command("普通消息") is False
    assert dr._looks_command("/x", command_prefix="") is True


def test_chat_heat_from_dict_telemetrics_and_bad_rate():
    assert dr._chat_heat({"messages_per_minute": 9.0}, [], stamp=_stamp(12)) == "hot"
    assert dr._chat_heat({"messages_per_minute": 1.0}, [], stamp=_stamp(12)) == "cold"
    assert dr._chat_heat({"mpm": "boom"}, [], stamp=_stamp(12)) == "normal"


def test_unique_speakers_skips_old_bot_and_anonymous_nodes():
    nodes = [
        _node("old", user_id="a", ts=_stamp(12) - 3600),
        _node("bot", user_id="bot", ts=_stamp(12)),
        _node("anon", user_id="", ts=_stamp(12)),
        _node("a again", user_id="a", ts=_stamp(12)),
        _node("b", user_id="b", ts=_stamp(12)),
    ]
    assert dr._unique_speakers(nodes, since=_stamp(12) - 600, bot_id="bot") == 2


# --- evaluate() state transitions ------------------------------------------


def test_force_sleep_puts_awake_session_into_asleep_self():
    gate = dr.DailyRhythmGate()
    cfg = NS(rhythm_force_sleep=True)
    v = gate.evaluate(session_id="fs", user_id="u1", text=".", cfg=cfg, now=_stamp(2), telemetrics=_tele(0.5))
    assert v.allow is False
    assert v.state == dr.STATE_ASLEEP_SELF
    assert v.reason_code == "asleep_ambient"
    assert v.proactive_blocked is True
    assert gate.status("fs", now=_stamp(2))["state"] == dr.STATE_ASLEEP_SELF


def test_insomniac_session_resets_to_awake_when_feature_off():
    gate = dr.DailyRhythmGate()
    now = _stamp(2)
    gate.note_spoke("io", verdict=_ok_verdict(action="insomnia_line"), now=now)
    assert gate.status("io", now=now)["state"] == dr.STATE_INSOMNIAC
    v = gate.evaluate(session_id="io", user_id="u1", text="你好", now=now + 60, telemetrics=_tele(2.0))
    assert v.allow is True
    assert gate.status("io", now=now + 60)["state"] == dr.STATE_AWAKE


def test_second_goodnight_same_day_is_silenced_after_morning_wake():
    gate = dr.DailyRhythmGate()
    # 01:00 goodnight: first wave gets the quota and is noted.
    first = gate.evaluate(session_id="2gn", user_id="u1", text="晚安", now=_stamp(1), telemetrics=_tele(0.5))
    assert first.action == "goodnight_reply"
    gate.note_spoke("2gn", verdict=first, now=_stamp(1))
    # Morning wake (same calendar day: counters survive the _ensure day check).
    assert gate.status("2gn", now=_stamp(8))["state"] == dr.STATE_AWAKE
    # 23:40 second goodnight with quota already consumed -> later-wave silence.
    v = gate.evaluate(session_id="2gn", user_id="u2", text="晚安", now=_stamp(23, 40), telemetrics=_tele(0.5))
    assert v.allow is False
    assert v.reason_code == "wind_later_silence"
    assert v.state == dr.STATE_WINDING_DOWN
    assert v.proactive_blocked is True
    assert gate.status("2gn", now=_stamp(23, 40))["last_reason_code"] == "wind_later_silence"
    assert any(r["reason_code"] == "wind_later_silence" for r in gate.why_silent_rows("2gn"))


def _roll_true_sid(stamp: float) -> str:
    day = time.strftime("%Y%m%d", time.localtime(stamp))
    for i in range(500):
        sid = f"ci-roll-{i}"
        digest = hashlib.sha1(f"insomnia:{sid}:{day}".encode()).hexdigest()
        if int(digest[:4], 16) % 100 < 2:
            return sid
    raise AssertionError("no roll-winning sid found")


def test_insomnia_roll_gate_bounds_and_determinism():
    late = _stamp(2)
    # Quota already spent today -> no roll at all.
    assert dr.DailyRhythmGate._insomnia_roll("any", late, NS(insomnia_lines_today=1)) is False
    # Outside the 01:00-04:59 band -> no roll.
    assert dr.DailyRhythmGate._insomnia_roll("any", _stamp(9), NS(insomnia_lines_today=0, timezone="")) is False
    # In-band roll is deterministic per sid.
    sid = _roll_true_sid(late)
    assert dr.DailyRhythmGate._insomnia_roll(sid, late, NS(insomnia_lines_today=0, timezone="")) is True
    assert dr.DailyRhythmGate._insomnia_roll(sid, late, NS(insomnia_lines_today=0, timezone="")) is True
    # A non-winning sid stays False in-band.
    loser = "ci-roll-zz"
    while int(hashlib.sha1(f"insomnia:{loser}:{time.strftime('%Y%m%d', time.localtime(late))}".encode()).hexdigest()[:4], 16) % 100 < 2:
        loser += "x"
    assert dr.DailyRhythmGate._insomnia_roll(loser, late, NS(insomnia_lines_today=0, timezone="")) is False


def test_insomnia_enabled_ambient_line_only_when_roll_hits():
    late = _stamp(2)
    sid = _roll_true_sid(late)
    gate = dr.DailyRhythmGate()
    cfg = NS(rhythm_insomnia_enabled=True)
    hit = gate.evaluate(
        session_id=sid, user_id="u1", text="……", explicit=False, cfg=cfg, now=late, telemetrics=_tele(1.0)
    )
    assert hit.allow is True
    assert hit.reason_code == "insomnia_ok"
    assert hit.action == "insomnia_line"
    assert hit.state == dr.STATE_INSOMNIAC
    assert hit.length_hint == "brief"

    loser = "ci-roll-zz"
    while int(hashlib.sha1(f"insomnia:{loser}:{time.strftime('%Y%m%d', time.localtime(late))}".encode()).hexdigest()[:4], 16) % 100 < 2:
        loser += "x"
    miss = gate.evaluate(
        session_id=loser, user_id="u1", text="……", explicit=False, cfg=cfg, now=late, telemetrics=_tele(1.0)
    )
    assert miss.allow is True
    assert miss.action == ""
    assert gate.status(loser, now=late)["state"] == dr.STATE_AWAKE


def test_insomniac_state_expires_after_thirty_minutes_of_quiet():
    gate = dr.DailyRhythmGate()
    now = _stamp(2)
    gate.note_spoke("ix", verdict=_ok_verdict(action="insomnia_line"), now=now)
    v = gate.evaluate(session_id="ix", user_id="u1", text="你好", now=now + 31 * 60, telemetrics=_tele(1.0))
    assert v.allow is True
    assert v.state == dr.STATE_AWAKE
    assert gate.status("ix", now=now + 31 * 60)["state"] == dr.STATE_AWAKE


# --- note_spoke() bookkeeping ----------------------------------------------


def test_note_spoke_without_verdict_is_noop():
    gate = dr.DailyRhythmGate()
    assert gate.note_spoke("nv", now=_stamp(12)) is None
    assert gate.status("nv", now=_stamp(12))["state"] == dr.STATE_AWAKE


def test_note_spoke_goodnight_while_awake_lands_in_winding():
    gate = dr.DailyRhythmGate()
    now = _stamp(23)
    v = _ok_verdict(action="goodnight_reply", consume_goodnight_quota=True)
    gate.note_spoke("aw", verdict=v, now=now)
    sess = gate._ensure("aw", now)
    assert sess.state == dr.STATE_WINDING_DOWN
    assert sess.goodnight_replied is True
    assert sess.goodnight_text_used == 1
    assert 20 * 60 <= sess.wind_sleep_deadline - now <= 40 * 60


def test_note_spoke_goodnight_from_brief_wake_moves_to_winding():
    gate = dr.DailyRhythmGate()
    now = _stamp(2)
    gate.note_spoke("bw", verdict=_ok_verdict(action="wake_reply"), now=now)
    assert gate.status("bw", now=now)["state"] == dr.STATE_BRIEF_WAKE
    gate.note_spoke("bw", verdict=_ok_verdict(action="goodnight_reply"), now=now + 60)
    assert gate.status("bw", now=now + 60)["state"] == dr.STATE_WINDING_DOWN
    assert gate._ensure("bw", now + 60).goodnight_replied is True


def test_note_spoke_goodnight_while_asleep_keeps_session_asleep():
    gate = dr.DailyRhythmGate()
    now = _stamp(2)
    _sleep_sess(gate, "as", now, kind="after_wind")
    gate.note_spoke("as", verdict=_ok_verdict(action="goodnight_reply", consume_goodnight_quota=True), now=now)
    sess = gate._ensure("as", now)
    assert sess.state == dr.STATE_ASLEEP_AFTER_WIND
    assert sess.wind_started_at == 0.0  # never re-entered winding
    assert sess.goodnight_replied is True


def test_note_spoke_day_share_and_morning_hi_quota_once():
    gate = dr.DailyRhythmGate()
    noon = _stamp(14)
    share = gate.evaluate(
        session_id="ds", user_id="u1", text="今天人好少", explicit=False, now=noon,
        telemetrics=_tele(3.0), gap_fill_candidate=True,
    )
    assert share.reason_code == "day_share_ok"
    assert share.day_share is True
    gate.note_spoke("ds", verdict=share, now=noon)
    assert gate.status("ds", now=noon)["day_share_used"] == 1
    again = gate.evaluate(
        session_id="ds", user_id="u2", text="又没人了", explicit=False, now=noon + 60,
        telemetrics=_tele(3.0), gap_fill_candidate=True,
    )
    assert again.day_share is False
    assert again.allow is True

    morning = _stamp(8)
    hi = gate.evaluate(session_id="ds", user_id="u3", text="早安", explicit=False, now=morning, telemetrics=_tele(2.0))
    assert hi.action == "morning_hi"
    gate.note_spoke("ds", verdict=hi, now=morning)
    assert gate.status("ds", now=morning)["morning_hi_done"] is True
    later = gate.evaluate(session_id="ds", user_id="u4", text="早安呀", explicit=False, now=morning + 30 * 60, telemetrics=_tele(2.0))
    assert later.action == ""


def test_note_spoke_insomnia_line_and_goodnight_hint_recording():
    gate = dr.DailyRhythmGate()
    now = _stamp(2)
    gate.note_spoke("ir", verdict=_ok_verdict(action="insomnia_line"), now=now)
    st = gate.status("ir", now=now)
    assert st["state"] == dr.STATE_INSOMNIAC
    assert st["insomnia_lines_today"] == 1

    gate.note_spoke("ir", verdict=_ok_verdict(), now=now + 60, text="晚安，明天见")
    sess = gate._ensure("ir", now + 60)
    assert sess.pre_sleep_bot_msg_hint == "晚安，明天见"
    gate.note_spoke("ir", verdict=_ok_verdict(), now=now + 120, text="随便聊聊")
    assert sess.pre_sleep_bot_msg_hint == "晚安，明天见"  # non-goodnight leaves hint


# --- status / why aggregation ----------------------------------------------


def test_status_aggregate_picks_most_interesting_session():
    gate = dr.DailyRhythmGate()
    now = _stamp(22)
    gate.evaluate(session_id="a", user_id="u1", text="晚安", now=now, telemetrics=_tele(0.5))
    gate._ensure("c", now)
    agg = gate.status()
    assert agg["state"] == dr.STATE_WINDING_DOWN
    assert agg["sessions"] == 2
    assert agg["last_reason_code"] == "wind_goodnight_ok"

    gate.note_spoke("b", verdict=_ok_verdict(action="wake_reply"), now=_stamp(2))
    pick = gate.status()
    assert pick["state"] == dr.STATE_BRIEF_WAKE
    assert pick["sessions"] == 3

    gate.note_spoke("d", verdict=_ok_verdict(action="insomnia_line"), now=_stamp(2, 1))
    best = gate.status()
    assert best["state"] == dr.STATE_INSOMNIAC
    assert best["sessions"] == 4


def test_why_rows_aggregate_sorted_and_limited():
    gate = dr.DailyRhythmGate()
    gate._note_why("a", 5.0, "c5", "z5")
    gate._note_why("b", 9.0, "c9", "z9")
    gate._note_why("a", 1.0, "c1", "z1")
    gate._note_why("b", 3.0, "c3", "z3")
    assert [r["reason_code"] for r in gate.why_silent_rows()] == ["c9", "c5", "c3", "c1"]
    assert [r["reason_code"] for r in gate.why_silent_rows(limit=2)] == ["c9", "c5"]


def test_why_bucket_is_trimmed_to_last_thirty_rows():
    gate = dr.DailyRhythmGate()
    for i in range(45):
        gate._note_why("t", float(i), f"c{i}", f"z{i}")
    rows = gate.why_silent_rows("t", limit=100)
    assert len(rows) == 34
    assert rows[0]["ts"] == 11.0
    assert rows[-1]["ts"] == 44.0


# --- winding_down internals ------------------------------------------------


def test_hot_goodnight_enters_winding_with_extended_deadline():
    gate = dr.DailyRhythmGate()
    now = _stamp(22)
    v = gate.evaluate(session_id="hw", user_id="u1", text="晚安", now=now, telemetrics=_tele(12.0))
    assert v.action == "goodnight_reply"
    sess = gate._ensure("hw", now)
    assert sess.state == dr.STATE_WINDING_DOWN
    assert sess.wind_sleep_deadline - now >= 30 * 60  # hot band: base * 1.5
    assert sess.hot_delay_until == now + 15 * 60


def test_enter_winding_again_with_hot_heat_pushes_deadline_out():
    gate = dr.DailyRhythmGate()
    now = _stamp(22)
    gate.evaluate(session_id="hw2", user_id="u1", text="晚安", now=now, telemetrics=_tele(2.0))
    sess = gate._ensure("hw2", now)
    prev_deadline = sess.wind_sleep_deadline
    assert sess.last_reason_code != "wind_hot_delay"
    later = now + 60
    gate._enter_winding(sess, later, heat="hot")
    assert sess.wind_sleep_deadline == max(prev_deadline, later + 15 * 60)
    assert sess.hot_delay_until == later + 15 * 60
    assert sess.last_reason_code == "wind_hot_delay"
    assert sess.last_reason_zh == dr.REASON["wind_hot_delay"]


def test_winding_with_sleep_after_wind_disabled_never_sleeps():
    gate = dr.DailyRhythmGate()
    cfg = NS(rhythm_sleep_after_winddown=False)
    now = _stamp(23)
    v = gate.evaluate(session_id="nsw", user_id="u1", text="晚安", now=now, telemetrics=_tele(0.5))
    gate.note_spoke("nsw", verdict=v, now=now)
    later = gate.evaluate(session_id="nsw", user_id="u2", text="……", cfg=cfg, now=now + 50 * 60, telemetrics=_tele(0.2))
    assert later.state == dr.STATE_WINDING_DOWN
    assert later.allow is False
    assert gate.status("nsw", now=now + 50 * 60)["state"] == dr.STATE_WINDING_DOWN


def test_tick_keeps_winding_when_deadline_not_reached_on_normal_heat():
    gate = dr.DailyRhythmGate()
    now = _stamp(1)
    v = gate.evaluate(session_id="wd", user_id="u1", text="晚安", now=now, telemetrics=_tele(0.5))
    gate.note_spoke("wd", verdict=v, now=now)
    sess = gate._ensure("wd", now)
    probe = now + 25 * 60
    sess.wind_started_at = probe - 25 * 60
    sess.wind_sleep_deadline = probe + 10 * 60  # deadline still in the future
    later = gate.evaluate(session_id="wd", user_id="u2", text="嗯", now=probe, telemetrics=_tele(4.0))
    assert later.state == dr.STATE_WINDING_DOWN
    assert later.allow is False
    assert gate.status("wd", now=probe)["state"] == dr.STATE_WINDING_DOWN


def test_winding_explicit_goodnight_and_plain_message_are_allowed_short():
    gate = dr.DailyRhythmGate()
    now = _stamp(1)
    v = gate.evaluate(session_id="we", user_id="u1", text="晚安", now=now, telemetrics=_tele(0.5))
    gate.note_spoke("we", verdict=v, now=now)
    gn = gate.evaluate(session_id="we", user_id="u2", text="晚安", explicit=True, now=now + 60, telemetrics=_tele(2.0))
    assert gn.allow is True
    assert gn.reason_code == "ok"
    assert gn.state == dr.STATE_WINDING_DOWN
    assert gn.force_scale == 0.6
    assert gn.proactive_blocked is True
    plain = gate.evaluate(session_id="we", user_id="u3", text="有正事说", explicit=True, now=now + 120, telemetrics=_tele(2.0))
    assert plain.allow is True
    assert plain.state == dr.STATE_WINDING_DOWN
    assert plain.force_scale == 0.7


# --- asleep wake handling ---------------------------------------------------


def test_brief_wake_cooldown_hushes_ambient_but_allows_explicit():
    gate = dr.DailyRhythmGate()
    now = _stamp(2)
    _sleep_sess(gate, "bw", now, kind="after_wind")
    wake = gate.evaluate(
        session_id="bw", user_id="u1", text="@bot 帮我看下报错", explicit=True, now=now,
        telemetrics=_tele(0.5), bot_id="bot",
        recent_nodes=[_node("@bot 帮我看下报错", user_id="u1", ts=now, mentions=["bot"])],
    )
    assert wake.allow is True
    assert wake.action == "wake_reply"
    gate.note_spoke("bw", verdict=wake, now=now)
    st = gate.status("bw", now=now)
    assert st["state"] == dr.STATE_BRIEF_WAKE
    assert st["wake_replies_today"] == 1

    ambient = gate.evaluate(session_id="bw", user_id="u2", text="随便聊聊", now=now + 60, telemetrics=_tele(2.0))
    assert ambient.allow is False
    assert ambient.reason_code == "brief_wake_cooldown"
    assert ambient.proactive_blocked is True

    explicit = gate.evaluate(
        session_id="bw", user_id="u3", text="那问你个问题", explicit=True, now=now + 90, telemetrics=_tele(2.0)
    )
    assert explicit.allow is True
    assert explicit.reason_code == "ok"
    assert explicit.state == dr.STATE_BRIEF_WAKE
    assert explicit.force_scale == 0.5


def test_mention_goodnight_does_not_wake_twice_per_day():
    gate = dr.DailyRhythmGate()
    now = _stamp(2)
    _sleep_sess(gate, "c2", now, kind="after_wind")
    wake = gate.evaluate(
        session_id="c2", user_id="u1", text="@bot 在吗", explicit=True, now=now,
        telemetrics=_tele(0.5), bot_id="bot",
        recent_nodes=[_node("@bot 在吗", user_id="u1", ts=now, mentions=["bot"])],
    )
    gate.note_spoke("c2", verdict=wake, now=now)
    assert gate.status("c2", now=now)["wake_replies_today"] == 1
    # Cooldown expires: back to asleep_after_wind.
    redo = gate.evaluate(session_id="c2", user_id="u2", text=".", now=now + 11 * 60, telemetrics=_tele(0.2))
    assert gate.status("c2", now=now + 11 * 60)["state"] == dr.STATE_ASLEEP_AFTER_WIND
    assert redo.allow is False
    # Second @goodnight with the daily wake already spent -> still hush.
    capped = gate.evaluate(
        session_id="c2", user_id="u3", text="@bot 晚安", now=now + 12 * 60,
        telemetrics=_tele(0.5), bot_id="bot",
        recent_nodes=[_node("@bot 晚安", user_id="u3", ts=now + 12 * 60, mentions=["bot"])],
    )
    assert capped.allow is False
    assert capped.reason_code == "asleep_plain_gn"
    assert gate.status("c2", now=now + 12 * 60)["wake_replies_today"] == 1


# --- _eval_insomnia explicit / ambient -------------------------------------


def test_eval_insomnia_explicit_allowed_and_ambient_capped():
    gate = dr.DailyRhythmGate()
    cfg = NS(rhythm_insomnia_enabled=True)
    now = _stamp(2)
    gate.note_spoke("ei", verdict=_ok_verdict(action="insomnia_line"), now=now)
    explicit = gate.evaluate(
        session_id="ei", user_id="u1", text="帮我看下报错", explicit=True, cfg=cfg, now=now + 60,
        telemetrics=_tele(1.0),
    )
    assert explicit.allow is True
    assert explicit.reason_code == "ok"
    assert explicit.state == dr.STATE_INSOMNIAC

    sess = gate._ensure("ea", now)
    sess.state = dr.STATE_INSOMNIAC
    sess.insomnia_last_at = now - 60  # recent enough that _tick_sleep keeps state
    ambient = gate.evaluate(session_id="ea", user_id="u1", text="……", cfg=cfg, now=now + 90, telemetrics=_tele(1.0))
    assert ambient.allow is False
    assert ambient.reason_code == "insomnia_cap"
    assert ambient.proactive_blocked is True
    assert gate.status("ea", now=now + 90)["state"] == dr.STATE_INSOMNIAC


# --- overnight sleep guards ------------------------------------------------


def test_maybe_end_sleep_without_timestamp_keeps_state():
    gate = dr.DailyRhythmGate()
    now = _stamp(8)
    sess = gate._ensure("nt", now)
    sess.state = dr.STATE_WINDING_DOWN  # no wind_started_at recorded
    st = gate.status("nt", now=now)
    assert st["state"] == dr.STATE_WINDING_DOWN
    assert st["wind_started_at"] == 0.0


def test_morning_window_wake_grace_applies_outside_clock_band():
    gate = dr.DailyRhythmGate()
    noon = _stamp(12)
    sess = gate._ensure("wg", noon)
    sess.state = dr.STATE_AWAKE
    sess.last_wake_at = noon - 2 * 3600  # woke recently, still in grace
    v = gate.evaluate(session_id="wg", user_id="u1", text="早安", now=noon, telemetrics=_tele(1.0))
    assert v.action == "morning_hi"
    assert v.morning_hi is True

    control = dr.DailyRhythmGate()
    v2 = control.evaluate(session_id="wg2", user_id="u1", text="早安", now=noon, telemetrics=_tele(1.0))
    assert v2.action == ""
    assert v2.allow is True
