"""Unit tests for Milestone M4: Intervention & Back-off Arbiter.

Validates:
1. Strong direct addressivity forces speech approval even if in cooling.
2. Energy asymmetry detection (consecutive low-effort responses triggers shutoff).
3. Deep cooling suppression for ambient / weak messages.
4. Safe hover buffering (silently records without speaking).
5. Dynamic Willingness-to-Speak (WTS) calculation & vibe mode modulation.
"""

from __future__ import annotations

import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityLevel, AddressivityScore
from astrbot_plugin_chat_dynamics.core.arbiter import InterventionArbiter
from astrbot_plugin_chat_dynamics.core.telemetrics import RoomTelemetrics, TelemetricsTracker
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode


@pytest.fixture
def arbiter():
    return InterventionArbiter(
        base_threshold=0.60,
        deep_cooling_duration=900.0,
        asymmetry_streak_limit=2,
    )


@pytest.fixture
def sample_telemetrics():
    return RoomTelemetrics(
        mpm=10.0,
        token_density=20.0,
        emoji_ratio=0.1,
        punctuation_formality=0.6,
        sample_size=10,
        window_duration=60.0,
    )


def test_arbiter_strong_address_always_speaks(arbiter, sample_telemetrics):
    """Strong addressivity (@bot / quote) overrides cooling and triggers should_speak=True."""
    # Put session in cooling
    arbiter.trigger_cooling("group_1", duration_seconds=600.0, current_time=1000.0)
    assert arbiter.is_in_deep_cooling("group_1", current_time=1050.0) is True

    strong_addr = AddressivityScore(score=1.0, level=AddressivityLevel.STRONG, is_bot_targeted=True)
    res = arbiter.evaluate(
        session_id="group_1",
        addressivity=strong_addr,
        telemetrics=sample_telemetrics,
        vibe_mode=GroupChatMode.FAST_BANTER,
        user_id="user_1",
        text="小助手在吗？",
        current_time=1050.0,
    )

    assert res.should_speak is True
    assert res.willingness_score == 1.0


def test_arbiter_energy_asymmetry_cutoff(arbiter, sample_telemetrics):
    """2 consecutive low-effort single-word responses triggers energy asymmetry cut-off."""
    addr = AddressivityScore(score=0.8, level=AddressivityLevel.STRONG, is_bot_targeted=True)

    # 1st low-effort response ("哦")
    res1 = arbiter.evaluate("group_1", addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "哦", 1000.0)
    # Streak is 1, not yet reached limit 2
    assert res1.is_energy_asymmetric is False

    # 2nd consecutive low-effort response ("666")
    res2 = arbiter.evaluate("group_1", addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "666", 1005.0)
    assert res2.is_energy_asymmetric is True
    assert res2.should_speak is False
    assert "Energy asymmetry" in res2.reason


def test_arbiter_deep_cooling_suppresses_weak_messages(arbiter, sample_telemetrics):
    """Deep cooling blocks ambient speech when not explicitly addressed."""
    arbiter.trigger_cooling("group_1", duration_seconds=900.0, current_time=1000.0)

    ambient_addr = AddressivityScore(score=0.3, level=AddressivityLevel.WEAK, is_bot_targeted=False)
    res = arbiter.evaluate(
        "group_1", ambient_addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "大家在聊啥呢", 1100.0
    )

    assert res.should_speak is False
    assert res.in_deep_cooling is True
    assert "deep cooling" in res.reason


def test_arbiter_safe_hover_silently_buffers(arbiter, sample_telemetrics):
    """Safe hover score (0.4 - 0.7) does not speak."""
    hover_addr = AddressivityScore(score=0.55, level=AddressivityLevel.SAFE_HOVER, is_bot_targeted=False)
    res = arbiter.evaluate(
        "group_1", hover_addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "然后呢", 1000.0
    )

    assert res.should_speak is False
    assert "Safe hover" in res.reason
    assert "never speaks" in res.reason


def test_arbiter_filter_gate_blocks_weak_without_ambient(arbiter, sample_telemetrics):
    weak_addr = AddressivityScore(score=0.35, level=AddressivityLevel.WEAK, is_bot_targeted=False)
    res = arbiter.evaluate(
        "group_1",
        weak_addr,
        sample_telemetrics,
        GroupChatMode.FAST_BANTER,
        "u1",
        "这个接口怎么实现比较好？",
        1000.0,
        allow_ambient=False,
    )
    assert res.should_speak is False
    assert "no ambient intervention" in res.reason


def test_cooling_export_and_restore_drops_expired(arbiter):
    arbiter.trigger_cooling("alive", duration_seconds=60.0, current_time=1000.0)
    arbiter.trigger_cooling("dead", duration_seconds=5.0, current_time=1000.0)
    exported = arbiter.cooling_export()
    assert exported["alive"] == 1060.0
    restored = InterventionArbiter()
    restored.cooling_restore(exported, current_time=1030.0)
    assert restored.is_in_deep_cooling("alive", current_time=1030.0) is True
    assert restored.is_in_deep_cooling("dead", current_time=1030.0) is False


def test_cooling_wall_persist_survives_monotonic_reset(arbiter):
    """KV must store wall expiry so a new process monotonic clock cannot inflate cooling."""
    mono = 200_000.0
    wall = 1_750_000_000.0
    arbiter.trigger_cooling("g1", duration_seconds=900.0, current_time=mono)
    payload = arbiter.cooling_export_wall(wall_now=wall, current_time=mono)
    assert abs(payload["g1"] - (wall + 900.0)) < 0.01

    restored = InterventionArbiter()
    restored.cooling_restore_wall(
        payload, wall_now=wall + 30.0, current_time=5.0
    )
    remaining = restored.cooling_remaining("g1", current_time=5.0)
    assert abs(remaining - 870.0) < 0.01


def test_cooling_wall_restore_drops_legacy_monotonic_payload():
    restored = InterventionArbiter()
    restored.cooling_restore_wall(
        {"g1": 200_900.0},
        wall_now=1_750_000_000.0,
        current_time=5.0,
    )
    assert restored.is_in_deep_cooling("g1", current_time=5.0) is False


def test_arbiter_ack_is_not_low_effort(arbiter):
    assert arbiter.is_low_effort_text("好") is False
    assert arbiter.is_low_effort_text("好的") is False
    assert arbiter.is_low_effort_text("收到") is False
    assert arbiter.is_low_effort_text("谢谢") is False
    assert arbiter.is_low_effort_text("ok") is False
    assert arbiter.is_low_effort_text("哦") is True
    assert arbiter.is_low_effort_text("666") is True


def test_arbiter_vibe_mode_threshold_modulation(arbiter, sample_telemetrics):
    """Vibe mode dynamically shifts WTS and thresholds:
    - In CHILL_FADE: threshold is elevated (0.75) and WTS penalized.
    - In FAST_BANTER: threshold is lowered (0.55).
    """
    weak_addr = AddressivityScore(score=0.35, level=AddressivityLevel.WEAK, is_bot_targeted=False)

    # In CHILL_FADE
    res_chill = arbiter.evaluate(
        "group_1", weak_addr, sample_telemetrics, GroupChatMode.CHILL_FADE, "u1", "今天群里好安静呀，大家都在忙什么呢", 1000.0
    )
    assert res_chill.threshold == 0.75
    assert res_chill.should_speak is False

    # In FAST_BANTER
    res_banter = arbiter.evaluate(
        "group_1", weak_addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "刚才那个梗图真是太有意思了", 1000.0
    )
    assert res_banter.threshold == 0.55


def test_arbiter_record_bot_spoke_resets_streak(arbiter, sample_telemetrics):
    addr = AddressivityScore(score=0.8, level=AddressivityLevel.STRONG, is_bot_targeted=True)
    arbiter.evaluate("group_1", addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "哦", 1000.0)
    arbiter.evaluate("group_1", addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "666", 1005.0)
    assert arbiter._low_effort_streaks[("group_1", "u1")] >= 2

    arbiter.record_bot_spoke("group_1", timestamp=1010.0)
    assert ("group_1", "u1") not in arbiter._low_effort_streaks


def test_arbiter_strong_substantive_overrides_previous_asymmetry(arbiter, sample_telemetrics):
    addr = AddressivityScore(score=1.0, level=AddressivityLevel.STRONG, is_bot_targeted=True)
    arbiter.evaluate("group_1", addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "哦", 1000.0)
    arbiter.evaluate("group_1", addr, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "666", 1005.0)

    res = arbiter.evaluate(
        "group_1",
        addr,
        sample_telemetrics,
        GroupChatMode.FAST_BANTER,
        "u1",
        "小助手帮我看一下这段代码为什么报错",
        1010.0,
    )
    assert res.should_speak is True
    assert res.is_energy_asymmetric is False


def test_arbiter_ambient_low_effort_does_not_count(arbiter, sample_telemetrics):
    weak = AddressivityScore(score=0.2, level=AddressivityLevel.WEAK, is_bot_targeted=False)
    arbiter.record_bot_spoke("group_1", timestamp=990.0, user_id="alice")
    arbiter.evaluate("group_1", weak, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "666", 1000.0)
    arbiter.evaluate("group_1", weak, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1", "哦", 1005.0)
    assert arbiter._low_effort_streaks.get(("group_1", "u1"), 0) == 0


def test_arbiter_hover_from_other_user_does_not_count(arbiter, sample_telemetrics):
    hover = AddressivityScore(score=0.50, level=AddressivityLevel.SAFE_HOVER, is_bot_targeted=False)
    arbiter.record_bot_spoke("group_1", timestamp=990.0, user_id="alice")
    arbiter.evaluate("group_1", hover, sample_telemetrics, GroupChatMode.FAST_BANTER, "bob", "哦", 1000.0)
    res = arbiter.evaluate("group_1", hover, sample_telemetrics, GroupChatMode.FAST_BANTER, "bob", "6", 1005.0)
    assert res.is_energy_asymmetric is False
    assert arbiter._low_effort_streaks.get(("group_1", "bob"), 0) == 0


def test_arbiter_same_user_hover_after_bot_counts(arbiter, sample_telemetrics):
    hover = AddressivityScore(score=0.50, level=AddressivityLevel.SAFE_HOVER, is_bot_targeted=False)
    arbiter.record_bot_spoke("group_1", timestamp=990.0, user_id="alice")
    first = arbiter.evaluate("group_1", hover, sample_telemetrics, GroupChatMode.FAST_BANTER, "alice", "哦", 1000.0)
    second = arbiter.evaluate("group_1", hover, sample_telemetrics, GroupChatMode.FAST_BANTER, "alice", "6", 1005.0)
    assert first.is_energy_asymmetric is False
    assert second.is_energy_asymmetric is True
    assert second.should_speak is False


def test_arbiter_auto_cool_after_bot_spoke_and_chill(arbiter, sample_telemetrics):
    arbiter.record_bot_spoke("group_1", timestamp=1000.0)
    chilled = RoomTelemetrics(
        mpm=1.0,
        token_density=8.0,
        emoji_ratio=0.0,
        punctuation_formality=0.2,
        sample_size=6,
        window_duration=60.0,
    )
    triggered = arbiter.maybe_auto_cool(
        "group_1",
        GroupChatMode.CHILL_FADE,
        chilled,
        current_time=1100.0,
    )
    assert triggered is True
    assert arbiter.is_in_deep_cooling("group_1", current_time=1100.0) is True


def test_private_topic_blocks_ambient_but_not_explicit_address(arbiter, sample_telemetrics):
    private = RoomTelemetrics(
        **{**sample_telemetrics.__dict__, "scene_tags": ("private_topic",)}
    )
    weak = AddressivityScore(0.3, AddressivityLevel.WEAK, False)
    strong = AddressivityScore(1.0, AddressivityLevel.STRONG, True)

    quiet = arbiter.evaluate("private", weak, private, GroupChatMode.FAST_BANTER, "u1", "别告诉别人", 1000.0)
    direct = arbiter.evaluate("private", strong, private, GroupChatMode.FAST_BANTER, "u1", "小助手帮我", 1001.0)

    assert quiet.should_speak is False
    assert quiet.private_topic is True
    assert direct.should_speak is True


def test_private_window_does_not_suppress_unrelated_high_value_turn(arbiter):
    tracker = TelemetricsTracker(window_seconds=60.0)
    tracker.record_message("room", "这是秘密", timestamp=1000.0, user_id="u1")
    private_window = tracker.get_telemetrics("room", current_time=1000.0)

    weak = AddressivityScore(0.39, AddressivityLevel.WEAK, False, topic_relevance=1.0)
    private_turn = arbiter.evaluate(
        "room", weak, private_window, GroupChatMode.FAST_BANTER, "u1", "这是秘密", 1001.0
    )
    technical_turn = arbiter.evaluate(
        "room", weak, private_window, GroupChatMode.FAST_BANTER, "u2", "这个 Python 接口为什么失败？", 1002.0
    )
    direct_turn = arbiter.evaluate(
        "room",
        AddressivityScore(1.0, AddressivityLevel.STRONG, True),
        private_window,
        GroupChatMode.FAST_BANTER,
        "u2",
        "小助手帮我",
        1003.0,
    )

    assert "private_topic" in private_window.scene_tags
    assert private_turn.should_speak is False
    assert "Private-topic boundary" in private_turn.reason
    assert technical_turn.private_topic is True
    assert technical_turn.should_speak is True
    assert technical_turn.willingness_score >= technical_turn.threshold
    assert "Private-topic boundary" not in technical_turn.reason
    assert direct_turn.should_speak is True


def test_wts_exposes_content_value_and_accumulated_fatigue(arbiter, sample_telemetrics):
    relevant = AddressivityScore(
        0.39,
        AddressivityLevel.WEAK,
        False,
        topic_relevance=1.0,
    )
    fresh = arbiter.evaluate(
        "busy", relevant, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1",
        "这个 Python 接口为什么报错？", 1000.0,
    )
    for timestamp in (701.0, 702.0, 703.0, 704.0, 705.0, 706.0, 707.0):
        arbiter.record_bot_spoke("busy", timestamp=timestamp)
    tired = arbiter.evaluate(
        "busy", relevant, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1",
        "这个 Python 接口为什么报错？", 1000.0,
    )

    assert fresh.should_speak is True
    assert fresh.professionalism > 0
    assert fresh.question_value > 0
    assert tired.fatigue_penalty > 0
    assert tired.should_speak is False


def test_wts_uses_participation_and_question_value(arbiter, sample_telemetrics):
    ambient = AddressivityScore(0.30, AddressivityLevel.WEAK, False, topic_relevance=0.2)
    crowded = RoomTelemetrics(
        **{**sample_telemetrics.__dict__, "unique_speakers": 5}
    )
    quiet = arbiter.evaluate(
        "room", ambient, sample_telemetrics, GroupChatMode.FAST_BANTER, "u1",
        "随便聊聊天气", 1000.0,
    )
    asked = arbiter.evaluate(
        "room", ambient, crowded, GroupChatMode.FAST_BANTER, "u1",
        "这个接口为什么失败？", 1000.0,
    )
    assert asked.question_value > quiet.question_value
    assert asked.participation > quiet.participation
    assert asked.willingness_score > quiet.willingness_score
