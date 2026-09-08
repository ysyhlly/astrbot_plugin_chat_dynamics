"""Unit tests for Milestone M3: Telemetrics & Vibe Analyzer.

Validates:
1. MPM sliding window tracking and calculation accuracy.
2. Token density, emoji/sticker detection, and punctuation formality scoring.
3. GroupChatMode state classification (`fast_banter`, `serious_inquiry`, `chill_fade`).
4. Schmitt-trigger hysteresis preventing flap around boundary thresholds.
"""

from __future__ import annotations

import pytest

from astrbot_plugin_chat_dynamics.core.telemetrics import TelemetricsTracker
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode, VibeAnalyzer, parse_mode_label


# ---------------------------------------------------------------------------
# Telemetrics Tracker Tests
# ---------------------------------------------------------------------------


def test_telemetrics_separates_unicode_emoji_media_and_local_labels():
    tracker = TelemetricsTracker(window_seconds=60.0)
    tracker.record_message("room", "哈哈 😂", timestamp=10.0)
    tracker.record_message("room", "[CQ:image,file=x]", timestamp=11.0)
    tracker.record_message("room", "这个 Python 接口报错了，别告诉别人", timestamp=12.0)

    metrics = tracker.get_telemetrics("room", current_time=12.0)
    assert metrics.unicode_emoji_ratio == pytest.approx(1 / 3, abs=0.001)
    assert metrics.media_ratio == pytest.approx(1 / 3, abs=0.001)
    assert metrics.emoji_ratio == pytest.approx(2 / 3, abs=0.001)
    assert "technical_help" in metrics.scene_tags
    assert "private_topic" in metrics.scene_tags
    assert tracker.get_recent_messages("room", current_time=12.0)[-1].startswith("这个 Python")


def test_telemetrics_counts_unique_speakers_and_atmosphere_splits_labels():
    vibe = VibeAnalyzer()
    vibe.record_message("room", "哈哈", timestamp=10.0, user_id="alice")
    vibe.record_message("room", "这个 Python 接口报错了", timestamp=11.0, user_id="bob")
    snapshot = vibe.get_atmosphere("room", current_time=11.0)
    assert snapshot.energy.unique_speakers == 2
    assert snapshot.energy.average_chars == snapshot.energy.token_density
    assert "technical_help" in snapshot.scene_tags
    assert snapshot.mode in GroupChatMode

def test_telemetrics_empty_session():
    """Verify empty session yields safe zero-value defaults."""
    tracker = TelemetricsTracker(window_seconds=60.0)
    metrics = tracker.get_telemetrics("empty_group", current_time=100.0)

    assert metrics.mpm == 0.0
    assert metrics.token_density == 0.0
    assert metrics.emoji_ratio == 0.0
    assert metrics.sample_size == 0


def test_telemetrics_mpm_calculation():
    """Verify MPM correctly calculates 15 messages in 60s window = 15.0 MPM."""
    tracker = TelemetricsTracker(window_seconds=60.0)
    base_time = 1000.0

    # Ingest 15 messages between t=1000.0 and t=1050.0 (within 60s of t=1060.0)
    for i in range(15):
        tracker.record_message("group_1", f"Message {i}", timestamp=base_time + i * 3.0)

    metrics = tracker.get_telemetrics("group_1", current_time=base_time + 60.0)
    assert metrics.sample_size == 15
    assert metrics.mpm == 15.0


def test_telemetrics_sliding_window_expiration():
    """Verify messages older than window_seconds are excluded from MPM."""
    tracker = TelemetricsTracker(window_seconds=60.0)
    base_time = 1000.0

    # 10 old messages at t=900..930
    for i in range(10):
        tracker.record_message("group_1", f"Old {i}", timestamp=base_time - 100.0 + i)

    # 5 fresh messages at t=980..990
    for i in range(5):
        tracker.record_message("group_1", f"New {i}", timestamp=base_time - 20.0 + i)

    # Query at t=1000.0: only the 5 fresh messages should be in window (1000 - 60 = 940 cutoff)
    metrics = tracker.get_telemetrics("group_1", current_time=base_time)
    assert metrics.sample_size == 5
    assert metrics.mpm == 5.0


def test_telemetrics_emoji_and_sticker_ratio():
    """Verify emoji ratio correctly identifies Unicode emojis and CQ face tags."""
    tracker = TelemetricsTracker(window_seconds=60.0)
    base_time = 1000.0

    tracker.record_message("group_1", "哈哈哈哈 😂", timestamp=base_time + 1.0)
    tracker.record_message("group_1", "[CQ:face,id=178] 冲冲冲", timestamp=base_time + 2.0)
    tracker.record_message("group_1", "今天天气不错", timestamp=base_time + 3.0)
    tracker.record_message("group_1", "收到收到", timestamp=base_time + 4.0)

    metrics = tracker.get_telemetrics("group_1", current_time=base_time + 10.0)
    assert metrics.sample_size == 4
    # 2 out of 4 have emojis/stickers -> 0.50
    assert metrics.emoji_ratio == 0.50


def test_telemetrics_punctuation_formality():
    """Verify punctuation formality distinguishes casual chat from structured sentences."""
    tracker = TelemetricsTracker(window_seconds=60.0)
    base_time = 1000.0

    # 2 formal structured sentences
    tracker.record_message("group_1", "请问这个函数的具体实现细节是什么？", timestamp=base_time + 1.0)
    tracker.record_message("group_1", "我们需要排查当前的性能瓶颈。", timestamp=base_time + 2.0)

    metrics = tracker.get_telemetrics("group_1", current_time=base_time + 5.0)
    assert metrics.sample_size == 2
    assert metrics.punctuation_formality == 1.0


# ---------------------------------------------------------------------------
# Vibe Analyzer & Hysteresis State Machine Tests
# ---------------------------------------------------------------------------

def test_vibe_mode_chill_fade():
    """Verify low MPM (< 3.0) or low message volume defaults to CHILL_FADE."""
    vibe = VibeAnalyzer(chill_fade_enter_mpm=3.0)
    # Only 1 sparse message
    vibe.record_message("group_1", "有人在吗", timestamp=100.0)

    mode = vibe.get_mode("group_1", current_time=120.0)
    assert mode == GroupChatMode.CHILL_FADE


def test_vibe_mode_fast_banter():
    """Verify high frequency short banter with memes triggers FAST_BANTER."""
    vibe = VibeAnalyzer(fast_banter_enter_mpm=10.0)
    base_time = 1000.0

    # Ingest 15 short messages in 40s (15 / 60 * 60 = 15 MPM)
    for i in range(15):
        vibe.record_message("group_1", f"草 2333 😂 msg {i}", timestamp=base_time + i * 2.0)

    mode = vibe.get_mode("group_1", current_time=base_time + 40.0)
    assert mode == GroupChatMode.FAST_BANTER


def test_vibe_mode_serious_inquiry():
    """Verify longer, structured messages with moderate MPM trigger SERIOUS_INQUIRY."""
    vibe = VibeAnalyzer(serious_min_density=25.0)
    base_time = 1000.0

    # Ingest 6 long formal messages
    for i in range(6):
        vibe.record_message(
            "group_1",
            f"关于第 {i} 点设计，我们需要深入分析系统架构以及并发安全方面的具体考虑，包括锁竞争与内存管理。",
            timestamp=base_time + i * 8.0,
        )

    mode = vibe.get_mode("group_1", current_time=base_time + 50.0)
    assert mode == GroupChatMode.SERIOUS_INQUIRY


def test_vibe_hysteresis_chill_fade_to_fast_banter():
    """Verify Schmitt-trigger hysteresis: from CHILL_FADE, need >= 5 MPM to wake up."""
    vibe = VibeAnalyzer(
        chill_fade_enter_mpm=3.0,
        chill_fade_exit_mpm=5.0,
        fast_banter_enter_mpm=10.0,
    )
    base_time = 1000.0

    # Start in CHILL_FADE
    vibe.record_message("group_1", "你好", timestamp=base_time)
    assert vibe.get_mode("group_1", current_time=base_time + 10.0) == GroupChatMode.CHILL_FADE

    # Add 2 messages so total messages in window is 3 (1 initial + 2 new = 3 messages = 3.0 MPM, which is < 5.0 exit threshold)
    for i in range(2):
        vibe.record_message("group_1", f"微弱回复 {i}", timestamp=base_time + 20.0 + i * 10.0)

    # MPM is 3.0 (< exit threshold 5.0) -> must STAY in CHILL_FADE
    mode_hover = vibe.get_mode("group_1", current_time=base_time + 60.0)
    assert mode_hover == GroupChatMode.CHILL_FADE

    # Now add 8 more rapid messages to push MPM to 12 (> 10.0)
    for i in range(8):
        vibe.record_message("group_1", f"加速玩梗 {i}", timestamp=base_time + 45.0 + i * 1.5)

    mode_woke = vibe.get_mode("group_1", current_time=base_time + 60.0)
    assert mode_woke == GroupChatMode.FAST_BANTER


def test_vibe_chill_does_not_flap_in_hysteresis_band():
    """MPM in [5, 7) must not bounce CHILL_FADE <-> FAST_BANTER every message."""
    vibe = VibeAnalyzer(
        fast_banter_enter_mpm=12.0,
        fast_banter_exit_mpm=7.0,
        chill_fade_enter_mpm=3.0,
        chill_fade_exit_mpm=5.0,
    )
    base_time = 4000.0
    vibe.record_message("group_1", "你好", timestamp=base_time)
    assert vibe.get_mode("group_1", current_time=base_time + 10.0) == GroupChatMode.CHILL_FADE
    for i in range(5):
        vibe.record_message("group_1", f"嗯 {i}", timestamp=base_time + 20.0 + i)
    first = vibe.get_mode("group_1", current_time=base_time + 60.0)
    second = vibe.get_mode("group_1", current_time=base_time + 61.0)
    assert first == GroupChatMode.CHILL_FADE
    assert second == GroupChatMode.CHILL_FADE


def test_vibe_fast_banter_exits_below_seven_mpm():
    """FAST_BANTER must leave when MPM falls below the 7.0 exit threshold, not wait until 3.0."""
    vibe = VibeAnalyzer(
        fast_banter_enter_mpm=12.0,
        fast_banter_exit_mpm=7.0,
        chill_fade_enter_mpm=3.0,
        chill_fade_exit_mpm=5.0,
    )
    base_time = 2000.0
    for i in range(15):
        vibe.record_message("group_1", f"草 2333 😂 {i}", timestamp=base_time + i)
    assert vibe.get_mode("group_1", current_time=base_time + 20.0) == GroupChatMode.FAST_BANTER

    # 5 messages in the last 60s → 5.0 MPM, between chill enter (3) and banter exit (7)
    for i in range(5):
        vibe.record_message("group_1", f"嗯 好的 {i}", timestamp=base_time + 80.0 + i)

    mode = vibe.get_mode("group_1", current_time=base_time + 100.0)
    assert mode != GroupChatMode.FAST_BANTER


def test_peek_mode_does_not_commit_hysteresis():
    """Dashboard/status reads must not rewrite stored vibe mode."""
    vibe = VibeAnalyzer()
    base_time = 3000.0
    for i in range(15):
        vibe.record_message("group_1", f"草 2333 😂 {i}", timestamp=base_time + i)
    committed = vibe.get_mode("group_1", current_time=base_time + 20.0)
    assert committed == GroupChatMode.FAST_BANTER

    peeked = vibe.peek_mode("group_1", current_time=base_time + 200.0)
    assert peeked == GroupChatMode.FAST_BANTER
    assert vibe._current_modes["group_1"] == GroupChatMode.FAST_BANTER


def test_parse_mode_label_from_verbose_output():
    assert parse_mode_label("fast_banter") == GroupChatMode.FAST_BANTER
    assert parse_mode_label("当前更像 serious inquiry 吧") == GroupChatMode.SERIOUS_INQUIRY
    assert parse_mode_label("群里有点冷场了") == GroupChatMode.CHILL_FADE
    assert parse_mode_label("好的，我已经收到你的消息") is None


def test_vibe_reports_llm_snapshot_source_and_count():
    vibe = VibeAnalyzer()
    vibe.get_mode("group_1", current_time=100.0)
    assert vibe.mode_source("group_1") == "telemetrics"

    vibe.mark_llm_snapshot("group_1", 120.0)
    vibe.set_mode("group_1", GroupChatMode.SERIOUS_INQUIRY, source="llm")
    assert vibe.mode_source("group_1") == "llm"
    assert vibe.llm_snapshot_count("group_1") == 1

    vibe.reset_session("group_1")
    assert vibe.mode_source("group_1") == "telemetrics"
    assert vibe.llm_snapshot_count("group_1") == 0
