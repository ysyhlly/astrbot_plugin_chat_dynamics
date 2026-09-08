"""Unit tests for R1 Turn-Taking & Debounce Buffer Engine.

Validates all 13 test specifications (TC-01 through TC-13) using VirtualClock
for zero-latency, 100% deterministic test execution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, List

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer, DebounceResult
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock


@pytest.mark.asyncio
async def test_discard_session_invalidates_old_turn_but_allows_new_input():
    clock = VirtualClock(initial_time=100.0)
    buffer = DebounceBuffer(time_service=clock, base_cooldown=2.0)
    flushed = []

    async def callback(result):
        flushed.append(result.consolidated_text)

    await buffer.ingest("room-a", "alice", "old", object(), callback)
    assert await buffer.discard("room-a") == 1
    await clock.advance(3.0)
    assert flushed == []

    await buffer.ingest("room-a", "alice", "new", object(), callback)
    await clock.advance(3.0)
    assert flushed == ["new"]
    await buffer.close(flush=False)


# ---------------------------------------------------------------------------
# Mock Event & Recorder Harness
# ---------------------------------------------------------------------------

@dataclass
class MockMessageObj:
    sender: Any
    group_id: str
    message_id: str
    message: list
    time: float


@dataclass
class MockSender:
    id: str
    nickname: str


class MockAstrMessageEvent:
    """Mock platform event mirroring AstrMessageEvent."""

    def __init__(
        self,
        message_str: str,
        sender_id: str = "user_1",
        group_id: str = "group_1",
        message_id: str = "msg_1",
    ):
        self.message_str = message_str
        self._sender_id = sender_id
        self._group_id = group_id
        self.message_id = message_id
        self.is_stopped = False
        self.message_obj = MockMessageObj(
            sender=MockSender(id=sender_id, nickname=sender_id),
            group_id=group_id,
            message_id=message_id,
            message=[],
            time=0.0,
        )

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._group_id

    def stop_event(self) -> None:
        self.is_stopped = True


class FlushRecorder:
    """Thread-safe accumulator for flushed DebounceResult instances."""

    def __init__(self):
        self.results: List[DebounceResult] = []

    async def on_flush(self, result: DebounceResult) -> None:
        self.results.append(result)


# ---------------------------------------------------------------------------
# Pytest Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def virtual_clock():
    return VirtualClock(initial_time=1000.0)


@pytest.fixture
def recorder():
    return FlushRecorder()


@pytest.fixture
def debounce_buffer(virtual_clock):
    return DebounceBuffer(
        time_service=virtual_clock,
        base_cooldown=3.5,
        extended_cooldown=6.5,
        max_cap=12.0,
    )


# ---------------------------------------------------------------------------
# Unit Test Cases TC-01 to TC-13
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debounce_happy_path_single_complete_message(debounce_buffer, virtual_clock, recorder):
    """TC-01: Verify a single complete message flushes after exact base cooldown (3.5s)."""
    event = MockAstrMessageEvent("今天天气真好。", sender_id="user_a", group_id="group_1")
    await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)

    # 3.0s elapsed: timer should not have fired yet
    await virtual_clock.advance(3.0)
    assert len(recorder.results) == 0

    # 0.6s additional elapsed (total 3.6s > 3.5s): flush must have fired
    await virtual_clock.advance(0.6)
    assert len(recorder.results) == 1

    res = recorder.results[0]
    assert res.session_id == "group_1"
    assert res.user_id == "user_a"
    assert res.consolidated_text == "今天天气真好。"
    assert len(res.messages) == 1
    assert len(res.raw_events) == 1
    assert res.first_event is not None
    assert res.last_event is not None
    assert res.start_time == 1000.0
    assert res.end_time == 1000.0
    assert res.duration == 0.0
    assert res.message_count == 1
    assert res.was_extended is False


@pytest.mark.asyncio
async def test_debounce_multi_message_burst_merged(debounce_buffer, virtual_clock, recorder):
    """TC-02: Verify 3 rapid messages within 2s reset the timer and merge into 1 turn."""
    e1 = MockAstrMessageEvent("你好", sender_id="user_a", group_id="group_1", message_id="m1")
    e2 = MockAstrMessageEvent("我想问个问题", sender_id="user_a", group_id="group_1", message_id="m2")
    e3 = MockAstrMessageEvent("关于Python异步编程的", sender_id="user_a", group_id="group_1", message_id="m3")

    # t = 1000.0
    await debounce_buffer.ingest("group_1", "user_a", e1.message_str, e1, recorder.on_flush)

    # t = 1000.8
    await virtual_clock.advance(0.8)
    await debounce_buffer.ingest("group_1", "user_a", e2.message_str, e2, recorder.on_flush)

    # t = 1001.8
    await virtual_clock.advance(1.0)
    await debounce_buffer.ingest("group_1", "user_a", e3.message_str, e3, recorder.on_flush)

    # From 1001.8, cooldown 3.5s means target is 1005.3.
    # At t = 1005.0 (advance 3.2s from 1001.8): should NOT have flushed
    await virtual_clock.advance(3.2)
    assert len(recorder.results) == 0

    # At t = 1005.4 (advance 0.4s): should flush
    await virtual_clock.advance(0.4)
    assert len(recorder.results) == 1

    res = recorder.results[0]
    assert res.user_id == "user_a"
    assert len(res.messages) == 3
    assert len(res.raw_events) == 3
    assert "你好" in res.consolidated_text
    assert "我想问个问题" in res.consolidated_text
    assert "关于Python异步编程的" in res.consolidated_text


@pytest.mark.asyncio
async def test_debounce_incompleteness_trailing_conjunction(debounce_buffer, virtual_clock, recorder):
    """TC-03: Verify trailing conjunction '因为...' extends window to extended cooldown (6.5s)."""
    event = MockAstrMessageEvent("我之所以没有提交PR，因为...", sender_id="user_a", group_id="group_1")
    await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)

    # Advance 3.5s: regular base cooldown would have fired, but incomplete message must wait
    await virtual_clock.advance(3.5)
    assert len(recorder.results) == 0

    # Advance to 6.0s total: still waiting
    await virtual_clock.advance(2.5)
    assert len(recorder.results) == 0

    # Advance to 6.6s total: extended cooldown expires
    await virtual_clock.advance(0.6)
    assert len(recorder.results) == 1
    assert recorder.results[0].consolidated_text == "我之所以没有提交PR，因为..."
    assert recorder.results[0].was_extended is True


@pytest.mark.asyncio
async def test_debounce_incompleteness_trailing_comma(debounce_buffer, virtual_clock, recorder):
    """TC-04: Verify trailing comma extends window to extended cooldown (6.5s)."""
    event = MockAstrMessageEvent("首先打开控制台，", sender_id="user_a", group_id="group_1")
    await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)

    await virtual_clock.advance(4.0)
    assert len(recorder.results) == 0

    await virtual_clock.advance(2.6)
    assert len(recorder.results) == 1
    assert recorder.results[0].was_extended is True


@pytest.mark.asyncio
async def test_debounce_incomplete_followed_by_complete_message(debounce_buffer, virtual_clock, recorder):
    """TC-05: Incomplete message followed by complete sentence resets timer to base cooldown (3.5s) from second message."""
    e1 = MockAstrMessageEvent("我觉得不行，但是", sender_id="user_a", group_id="group_1")
    e2 = MockAstrMessageEvent("如果重构一下代码就可以了。", sender_id="user_a", group_id="group_1")

    # t = 1000.0 (incomplete, extended window to 1006.5)
    await debounce_buffer.ingest("group_1", "user_a", e1.message_str, e1, recorder.on_flush)

    # t = 1002.0 (complete arrives, target becomes 1002.0 + 3.5 = 1005.5)
    await virtual_clock.advance(2.0)
    await debounce_buffer.ingest("group_1", "user_a", e2.message_str, e2, recorder.on_flush)

    # At t = 1005.0: should NOT have flushed
    await virtual_clock.advance(3.0)
    assert len(recorder.results) == 0

    # At t = 1005.6: should flush (at 1005.5, before 1006.5)
    await virtual_clock.advance(0.6)
    assert len(recorder.results) == 1
    assert len(recorder.results[0].messages) == 2


@pytest.mark.asyncio
async def test_debounce_hard_cap_truncation(debounce_buffer, virtual_clock, recorder):
    """TC-06: Continuous stream of incomplete messages flushes at 12.0s maximum."""
    # First message at t = 1000.0 -> max hard cap is 1012.0
    times = [0.0, 3.0, 3.0, 3.0, 2.0]  # Total elapsed relative to 1000: 0, 3, 6, 9, 11
    for idx, dt in enumerate(times):
        if dt > 0:
            await virtual_clock.advance(dt)
        event = MockAstrMessageEvent(f"第{idx+1}点，因为...", sender_id="user_a", group_id="group_1")
        await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)

    # Current virtual time is 1011.0. Hard cap is 1012.0.
    # At t = 1011.9: should not have flushed
    await virtual_clock.advance(0.9)
    assert len(recorder.results) == 0

    # At t = 1012.1: must have flushed due to 12.0s hard cap
    await virtual_clock.advance(0.2)
    assert len(recorder.results) == 1
    assert len(recorder.results[0].messages) == 5


@pytest.mark.asyncio
async def test_debounce_interleaved_users_isolation(debounce_buffer, virtual_clock, recorder):
    """TC-07: User A and User B in same group have independent debounce windows."""
    ea1 = MockAstrMessageEvent("User A message 1", sender_id="user_a", group_id="group_1")
    eb1 = MockAstrMessageEvent("User B message 1", sender_id="user_b", group_id="group_1")
    ea2 = MockAstrMessageEvent("User A message 2", sender_id="user_a", group_id="group_1")

    # t = 1000.0: User A sends (due 1003.5)
    await debounce_buffer.ingest("group_1", "user_a", ea1.message_str, ea1, recorder.on_flush)

    # t = 1001.0: User B sends (due 1004.5)
    await virtual_clock.advance(1.0)
    await debounce_buffer.ingest("group_1", "user_b", eb1.message_str, eb1, recorder.on_flush)

    # t = 1002.0: User A sends again (A resets to 1002.0 + 3.5 = 1005.5)
    await virtual_clock.advance(1.0)
    await debounce_buffer.ingest("group_1", "user_a", ea2.message_str, ea2, recorder.on_flush)

    # Advance to t = 1004.6: User B should flush (due 1004.5), User A should still wait (due 1005.5)
    await virtual_clock.advance(2.6)
    assert len(recorder.results) == 1
    assert recorder.results[0].user_id == "user_b"
    assert len(recorder.results[0].messages) == 1

    # Advance to t = 1005.6: User A should flush
    await virtual_clock.advance(1.0)
    assert len(recorder.results) == 2
    assert recorder.results[1].user_id == "user_a"
    assert len(recorder.results[1].messages) == 2


@pytest.mark.asyncio
async def test_debounce_interleaved_sessions_isolation(debounce_buffer, virtual_clock, recorder):
    """TC-08: Same user chatting across multiple sessions has isolated buffers."""
    eg1 = MockAstrMessageEvent("Msg in Group 1", sender_id="user_a", group_id="group_1")
    eg2 = MockAstrMessageEvent("Msg in Group 2", sender_id="user_a", group_id="group_2")

    # t = 1000.0: Send to Group 1 (due 1003.5)
    await debounce_buffer.ingest("group_1", "user_a", eg1.message_str, eg1, recorder.on_flush)

    # t = 1001.0: Send to Group 2 (due 1004.5)
    await virtual_clock.advance(1.0)
    await debounce_buffer.ingest("group_2", "user_a", eg2.message_str, eg2, recorder.on_flush)

    # Advance to 1003.6: Group 1 flushes, Group 2 waiting
    await virtual_clock.advance(2.6)
    assert len(recorder.results) == 1
    assert recorder.results[0].session_id == "group_1"

    # Advance to 1004.6: Group 2 flushes
    await virtual_clock.advance(1.0)
    assert len(recorder.results) == 2
    assert recorder.results[1].session_id == "group_2"


@pytest.mark.asyncio
async def test_debounce_edge_case_empty_and_whitespace(debounce_buffer, virtual_clock, recorder):
    """TC-09: Empty or whitespace-only messages are ignored and do not create timers."""
    e_empty = MockAstrMessageEvent("", sender_id="user_a", group_id="group_1")
    e_space = MockAstrMessageEvent("   \t\n  ", sender_id="user_a", group_id="group_1")

    await debounce_buffer.ingest("group_1", "user_a", e_empty.message_str, e_empty, recorder.on_flush)
    await debounce_buffer.ingest("group_1", "user_a", e_space.message_str, e_space, recorder.on_flush)

    await virtual_clock.advance(10.0)
    assert len(recorder.results) == 0
    assert virtual_clock.pending_timers_count == 0


@pytest.mark.asyncio
async def test_debounce_edge_case_emoji_only_no_extension(debounce_buffer, virtual_clock, recorder):
    """TC-10: Emoji-only messages are treated as complete reactions and flush at base cooldown."""
    event = MockAstrMessageEvent("👍🎉🚀", sender_id="user_a", group_id="group_1")
    await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)

    await virtual_clock.advance(3.0)
    assert len(recorder.results) == 0

    await virtual_clock.advance(0.6)
    assert len(recorder.results) == 1
    assert recorder.results[0].consolidated_text == "👍🎉🚀"


@pytest.mark.asyncio
async def test_debounce_rapid_cancel_and_restart_stress(debounce_buffer, virtual_clock, recorder):
    """TC-11: 20 rapid messages arriving with minimal delta reset cleanly without leaking tasks."""
    for i in range(20):
        ev = MockAstrMessageEvent(f"Fragment {i}", sender_id="user_a", group_id="group_1", message_id=f"m_{i}")
        await debounce_buffer.ingest("group_1", "user_a", ev.message_str, ev, recorder.on_flush)
        await virtual_clock.advance(0.01)

    # 20 * 0.01 = 0.2s elapsed. Cooldown is 3.5s from last message.
    await virtual_clock.advance(3.0)
    assert len(recorder.results) == 0

    await virtual_clock.advance(0.6)
    assert len(recorder.results) == 1
    assert len(recorder.results[0].messages) == 20
    assert virtual_clock.pending_timers_count == 0


@pytest.mark.asyncio
async def test_debounce_flush_on_shutdown(debounce_buffer, virtual_clock, recorder):
    """TC-12: Calling flush_all cancels all active timers and flushes all pending buffers immediately."""
    ea = MockAstrMessageEvent("Pending A", sender_id="user_a", group_id="group_1")
    eb = MockAstrMessageEvent("Pending B", sender_id="user_b", group_id="group_1")

    await debounce_buffer.ingest("group_1", "user_a", ea.message_str, ea, recorder.on_flush)
    await virtual_clock.advance(1.0)
    await debounce_buffer.ingest("group_1", "user_b", eb.message_str, eb, recorder.on_flush)

    # Shutdown invoked while timers are still pending
    flushed = await debounce_buffer.flush_all()
    assert len(flushed) == 2
    assert virtual_clock.pending_timers_count == 0

    # Advancing time further produces no redundant flushes
    await virtual_clock.advance(10.0)
    # The callback was either called by flush_all or results were returned
    total_flushed_count = len(recorder.results) + len(flushed) if not recorder.results else len(recorder.results)
    assert total_flushed_count == 2


@pytest.mark.asyncio
async def test_debounce_concurrency_lock_during_flush(debounce_buffer, virtual_clock):
    """TC-13: Verify releasing slot.lock before awaiting callback allows new message to start Turn 2 during flush."""
    flushed_results: List[DebounceResult] = []

    async def slow_on_flush(res: DebounceResult) -> None:
        flushed_results.append(res)
        # Simulate downstream async processing
        await virtual_clock.sleep(1.0)

    # Ingest Turn 1 message at t = 1000.0
    ev1 = MockAstrMessageEvent("Turn 1 message", sender_id="user_a", group_id="group_1", message_id="m1")
    await debounce_buffer.ingest("group_1", "user_a", ev1.message_str, ev1, slow_on_flush)

    # Advance 3.6s -> triggers flush of Turn 1 at t = 1003.5.
    # The timer worker pops Turn 1, releases slot.lock, and awaits slow_on_flush.
    await virtual_clock.advance(3.6)
    assert len(flushed_results) == 1
    assert flushed_results[0].consolidated_text == "Turn 1 message"

    # At t = 1003.6, while slow_on_flush is still active (ends at 1004.5),
    # ingest Turn 2 message from the same user:
    ev2 = MockAstrMessageEvent("Turn 2 message", sender_id="user_a", group_id="group_1", message_id="m2")
    await debounce_buffer.ingest("group_1", "user_a", ev2.message_str, ev2, slow_on_flush)

    # Advance 1.0s to let slow_on_flush of Turn 1 complete (virtual time reaches 1004.6).
    await virtual_clock.advance(1.0)
    # Turn 2 should still be waiting in its debounce window (started at 1003.6, due at 1003.6 + 3.5 = 1007.1)
    assert len(flushed_results) == 1

    # Advance 3.0s more (to 1007.6 > 1007.1): Turn 2 must now flush!
    await virtual_clock.advance(3.0)
    assert len(flushed_results) == 2
    assert flushed_results[1].consolidated_text == "Turn 2 message"


# ---------------------------------------------------------------------------
# Additional Edge Case & Infrastructure Tests
# ---------------------------------------------------------------------------

def test_virtual_clock_negative_advance_raises():
    """Verify VirtualClock rejects negative time advancement."""
    vc = VirtualClock(100.0)
    with pytest.raises(ValueError, match="Cannot advance virtual clock backwards"):
        vc.advance_sync(-5.0)


@pytest.mark.asyncio
async def test_virtual_clock_async_negative_advance_raises(virtual_clock):
    """Verify async advance rejects negative seconds."""
    with pytest.raises(ValueError, match="Cannot advance virtual clock backwards"):
        await virtual_clock.advance(-1.0)


def test_virtual_clock_advance_sync_and_reset():
    """Verify synchronous advance and reset on VirtualClock."""
    vc = VirtualClock(500.0)
    assert vc.time() == 500.0
    vc.advance_sync(25.5)
    assert vc.time() == 525.5
    vc.reset(1000.0)
    assert vc.time() == 1000.0
    assert vc.pending_timers_count == 0


@pytest.mark.asyncio
async def test_system_clock_basic():
    """Verify SystemClock monotonicity and sleep behavior."""
    from astrbot_plugin_chat_dynamics.core.time_service import SystemClock

    sc = SystemClock()
    t1 = sc.time()
    assert t1 > 0
    assert sc.now() >= t1
    await sc.sleep(0)
    await sc.sleep(-1)


def test_incompleteness_detector_36_golden_benchmarks():
    """Verify all 36 golden test benchmark cases from M1 Explorer 2."""
    from astrbot_plugin_chat_dynamics.core.incompleteness import IncompletenessDetector

    detector = IncompletenessDetector(threshold=0.50)

    test_cases = [
        # ZH Conjunctions
        ("我想去，但是", True),
        ("我觉得这个不行，因为……", True),
        ("明天吃火锅，然后", True),
        ("知其所以然", False),
        ("理所当然", False),
        ("好得不过如此", False),
        ("然后呢", False),
        ("所以呢？", False),
        # EN Conjunctions
        ("I wanted to call you, but", True),
        ("We can meet at 5 or...", True),
        ("He did not come because", True),
        ("I will do it if", True),
        ("Wait, so", True),
        ("I think so.", False),
        ("See you then!", False),
        # Hanging Punctuation
        ("我不确定...", True),
        ("等等。。", True),
        ("我想说的是——", True),
        ("apples,", True),
        ("配置如下：", True),
        ("clause 1;", True),
        ("hello ;)", False),
        ("好的~", False),
        # Autonomous / acknowledgments
        ("好的", False),
        ("ok", False),
        ("收到", False),
        ("Wait", True),
        ("稍等一下", True),
        ("1. 首先配置数据库", True),
        ("快点来吧", False),
        ("你怎么知道呢", False),
        ("我刚才看了一下", True),
        ("关于昨天的那个方案", True),
        # Unclosed syntax
        ('He said, "I will come', True),
        ("It's fine, don't worry", False),
        ("（这是第一部分", True),
        ("```python\ndef foo():", True),
        ("See `main.py for details", True),
    ]

    for text, expected in test_cases:
        actual = detector.check_incompleteness(text)
        assert actual == expected, f"Failed on '{text}': expected {expected}, got {actual}"


@pytest.mark.asyncio
async def test_debounce_prune_idle_slots(debounce_buffer, virtual_clock, recorder):
    """Verify pruning of old idle slots to prevent memory leaks."""
    event = MockAstrMessageEvent("Message 1", sender_id="user_a", group_id="group_1")
    await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)

    # Flush slot
    await virtual_clock.advance(3.6)
    assert len(recorder.results) == 1

    # Right after flush, slot is idle but not older than 300s
    pruned = debounce_buffer.prune_idle_slots(max_idle_seconds=300.0)
    assert pruned == 0

    # Advance beyond 300s
    await virtual_clock.advance(305.0)
    pruned = debounce_buffer.prune_idle_slots(max_idle_seconds=300.0)
    assert pruned == 1
    assert debounce_buffer.is_active("group_1", "user_a") is False


@pytest.mark.asyncio
async def test_debounce_close_discard_does_not_flush(debounce_buffer, virtual_clock, recorder):
    event = MockAstrMessageEvent("Still typing", sender_id="user_a", group_id="group_1")
    await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)
    assert debounce_buffer.get_pending_count() == 1

    await debounce_buffer.close(flush=False)
    await virtual_clock.advance(10.0)
    assert recorder.results == []
    assert debounce_buffer.get_pending_count() == 0


@pytest.mark.asyncio
async def test_debounce_close_prevents_subsequent_ingest(debounce_buffer, recorder):
    """Verify close() marks buffer closed and rejects new messages."""
    await debounce_buffer.close()
    with pytest.raises(RuntimeError, match="DebounceBuffer has been closed"):
        event = MockAstrMessageEvent("Too late", sender_id="user_a", group_id="group_1")
        await debounce_buffer.ingest("group_1", "user_a", event.message_str, event, recorder.on_flush)


@pytest.mark.asyncio
async def test_debounce_flush_specific_filter(debounce_buffer, virtual_clock, recorder):
    """Verify flush() with specific session_id or user_id filter."""
    e1 = MockAstrMessageEvent("User A Group 1", sender_id="user_a", group_id="group_1")
    e2 = MockAstrMessageEvent("User B Group 1", sender_id="user_b", group_id="group_1")
    e3 = MockAstrMessageEvent("User A Group 2", sender_id="user_a", group_id="group_2")

    await debounce_buffer.ingest("group_1", "user_a", e1.message_str, e1, recorder.on_flush)
    await debounce_buffer.ingest("group_1", "user_b", e2.message_str, e2, recorder.on_flush)
    await debounce_buffer.ingest("group_2", "user_a", e3.message_str, e3, recorder.on_flush)

    assert debounce_buffer.get_pending_count() == 3

    # Flush only group_2
    flushed_g2 = await debounce_buffer.flush(session_id="group_2")
    assert len(flushed_g2) == 1
    assert flushed_g2[0].session_id == "group_2"
    assert debounce_buffer.get_pending_count() == 2

    # Flush only user_a in group_1
    flushed_u_a = await debounce_buffer.flush(session_id="group_1", user_id="user_a")
    assert len(flushed_u_a) == 1
    assert flushed_u_a[0].user_id == "user_a"
    assert debounce_buffer.get_pending_count() == 1


@pytest.mark.asyncio
async def test_manual_flush_waits_for_callback_completion():
    buffer = DebounceBuffer(base_cooldown=30.0)
    completed = asyncio.Event()

    async def callback(_result):
        await asyncio.sleep(0)
        completed.set()

    event = MockAstrMessageEvent("hello")
    await buffer.ingest("g", "u", "hello", event, callback)
    await buffer.flush_all()
    assert completed.is_set()
    assert not buffer._background_tasks


@pytest.mark.asyncio
async def test_close_flush_waits_for_callback_completion():
    buffer = DebounceBuffer(base_cooldown=30.0)
    completed = asyncio.Event()

    async def callback(_result):
        await asyncio.sleep(0)
        completed.set()

    event = MockAstrMessageEvent("hello")
    await buffer.ingest("g", "u", "hello", event, callback)
    await buffer.close(flush=True)
    assert completed.is_set()
    assert not buffer._background_tasks


@pytest.mark.asyncio
async def test_virtual_clock_compacts_cancelled_timers():
    clock = VirtualClock()
    tasks = [asyncio.create_task(clock.sleep(100.0)) for _ in range(130)]
    await asyncio.sleep(0)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert clock.pending_timers_count == 0
    assert len(clock._timer_heap) < 64
