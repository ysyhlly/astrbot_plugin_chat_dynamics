"""M1 Challenger Empirical Stress & Race Condition Verification Suite.

Adversarially stress-tests core/debounce.py and core/time_service.py against:
1. High-concurrency interleaved message arrival (15 users, 6 sessions = 90 slots).
2. Rapid message arrival during active flush (simultaneous arrival while on_flush is executing).
3. Precision timer boundary conditions (t=3.499s vs t=3.501s, 6.499s vs 6.501s, hard cap 12.0s).
4. Real-world SystemClock wall-clock stress, task leak verification, and strict FIFO ordering.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer, DebounceResult
from astrbot_plugin_chat_dynamics.core.time_service import SystemClock, VirtualClock


# ---------------------------------------------------------------------------
# Helpers & Recorders
# ---------------------------------------------------------------------------

@dataclass
class SimpleMockEvent:
    text: str
    sender_id: str
    group_id: str
    seq: int


class ConcurrentRecorder:
    """Thread-safe / task-safe recorder for accumulating flushed results."""

    def __init__(self):
        self.results: List[DebounceResult] = []
        self.lock = asyncio.Lock()
        self.call_log: List[Tuple[float, str, str, int]] = []  # (timestamp, session, user, count)

    async def on_flush(self, result: DebounceResult) -> None:
        async with self.lock:
            self.results.append(result)
            self.call_log.append(
                (result.end_time, result.session_id, result.user_id, len(result.messages))
            )


# ---------------------------------------------------------------------------
# Scope 1: High-Concurrency Interleaved Traffic (15 Users x 6 Sessions = 90 Slots)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_concurrency_interleaved_users_and_sessions():
    """Empirically verify 15 users across 6 sessions (90 concurrent slots).

    Stress-tests:
    - 90 concurrent asynchronous ingest streams
    - Rapid intra-turn bursts (0.3s-0.8s) followed by inter-turn pauses (>4.0s)
    - Zero message drops or duplicates
    - Strict session and user isolation (no cross-talk)
    - Strict FIFO sequence preservation per (session, user)
    - Zero timer or task leaks
    """
    vc = VirtualClock(initial_time=2000.0)
    recorder = ConcurrentRecorder()
    debounce = DebounceBuffer(
        time_service=vc,
        base_cooldown=3.5,
        extended_cooldown=6.5,
        max_cap=12.0,
    )

    num_sessions = 6
    num_users = 15
    # Each slot sends 2 separate turns:
    # Turn 1: 3 rapid messages (dt=0.5s) -> flushes at 1.0 + 3.5 = 4.5s
    # Pause: advance 5.0s
    # Turn 2: 2 rapid messages (dt=0.4s) -> flushes at 5.0 + 0.4 + 3.5 = 8.9s
    # Total messages per slot = 5. Total across 90 slots = 450 messages.

    sent_tracker: Dict[Tuple[str, str], List[int]] = {}

    for s_idx in range(num_sessions):
        for u_idx in range(num_users):
            s_id = f"session_{s_idx}"
            u_id = f"user_{u_idx}"
            sent_tracker[(s_id, u_id)] = []

    async def simulate_slot(s_id: str, u_id: str):
        # Turn 1: 3 messages
        for seq in range(1, 4):
            ev = SimpleMockEvent(text=f"{s_id}:{u_id}:msg_{seq}", sender_id=u_id, group_id=s_id, seq=seq)
            sent_tracker[(s_id, u_id)].append(seq)
            await debounce.ingest(s_id, u_id, ev.text, ev, recorder.on_flush)
            if seq < 3:
                await vc.sleep(0.5)

        # Wait for Turn 1 to flush
        await vc.sleep(4.5)

        # Turn 2: 2 messages
        for seq in range(4, 6):
            ev = SimpleMockEvent(text=f"{s_id}:{u_id}:msg_{seq}", sender_id=u_id, group_id=s_id, seq=seq)
            sent_tracker[(s_id, u_id)].append(seq)
            await debounce.ingest(s_id, u_id, ev.text, ev, recorder.on_flush)
            if seq < 5:
                await vc.sleep(0.4)

        # Wait for Turn 2 to flush
        await vc.sleep(4.5)

    # Launch all 90 slots concurrently
    tasks = [
        asyncio.create_task(simulate_slot(f"session_{s}", f"user_{u}"))
        for s in range(num_sessions)
        for u in range(num_users)
    ]

    # Advance clock in small increments to step through all concurrent timers
    while any(not t.done() for t in tasks):
        await vc.advance(0.2)

    await asyncio.gather(*tasks)
    # Give a final advance to flush any pending timers
    await vc.advance(5.0)

    # Verification 1: Exactly 180 results (90 slots * 2 turns)
    assert len(recorder.results) == 90 * 2, f"Expected 180 flushed turns, got {len(recorder.results)}"

    # Verification 2: Total message preservation (450 messages)
    total_messages = sum(len(res.messages) for res in recorder.results)
    assert total_messages == 90 * 5, f"Expected 450 total messages, got {total_messages}"

    # Verification 3: Strict FIFO ordering & exact user/session isolation
    flushed_by_slot: Dict[Tuple[str, str], List[int]] = {k: [] for k in sent_tracker}
    for res in recorder.results:
        slot_key = (res.session_id, res.user_id)
        assert slot_key in flushed_by_slot, f"Unexpected slot {slot_key}"
        for item in res.messages:
            seq = item.event.seq
            flushed_by_slot[slot_key].append(seq)

    for slot_key, sent_seqs in sent_tracker.items():
        received_seqs = flushed_by_slot[slot_key]
        assert received_seqs == sent_seqs == [1, 2, 3, 4, 5], (
            f"FIFO violation or message loss in slot {slot_key}: sent {sent_seqs} vs received {received_seqs}"
        )

    # Verification 4: Zero pending timers in VirtualClock
    assert vc.pending_timers_count == 0


# ---------------------------------------------------------------------------
# Scope 2: Rapid Message Arrival During Active Flush
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rapid_message_arrival_during_active_flush():
    """Verify that messages arriving while on_flush is actively executing are safely isolated into Turn 2.

    Stress-tests:
    - on_flush holds execution for 2.0s of simulated downstream processing
    - While on_flush is running, 3 new messages from the same user arrive
    - Turn 1 contains ONLY message 1
    - Turn 2 contains ONLY messages 2, 3, 4
    - Turn 1 completes without cancellation or premature abort
    - Turn 2 flushes independently after its own cooldown
    - Reentrancy: on_flush itself ingests a message without deadlocking
    """
    vc = VirtualClock(initial_time=3000.0)
    recorder = ConcurrentRecorder()
    debounce = DebounceBuffer(time_service=vc, base_cooldown=3.5)

    flush_active = False
    concurrent_arrival_observed = False
    turn1_completed = False

    async def slow_on_flush(result: DebounceResult) -> None:
        nonlocal flush_active, turn1_completed
        flush_active = True
        recorder.results.append(result)
        # Downstream async work (e.g. LLM API call taking 2.0s)
        await vc.sleep(2.0)
        flush_active = False
        turn1_completed = True

    # 1. Ingest Turn 1 message at t = 3000.0
    ev1 = SimpleMockEvent("Turn 1 - Msg 1", "user_x", "group_1", 1)
    await debounce.ingest("group_1", "user_x", ev1.text, ev1, slow_on_flush)

    # 2. Advance to t = 3003.5 -> Turn 1 flushes, entering slow_on_flush
    await vc.advance(3.5)
    assert flush_active is True
    assert len(recorder.results) == 1
    assert recorder.results[0].consolidated_text == "Turn 1 - Msg 1"

    # 3. While slow_on_flush is actively executing (t=3003.5 to 3005.5),
    # inject 3 rapid messages from the same user
    await vc.advance(0.5)  # t = 3004.0 (flush still active)
    assert flush_active is True
    ev2 = SimpleMockEvent("Turn 2 - Msg 2", "user_x", "group_1", 2)
    await debounce.ingest("group_1", "user_x", ev2.text, ev2, slow_on_flush)

    await vc.advance(0.3)  # t = 3004.3
    assert flush_active is True
    ev3 = SimpleMockEvent("Turn 2 - Msg 3", "user_x", "group_1", 3)
    await debounce.ingest("group_1", "user_x", ev3.text, ev3, slow_on_flush)

    await vc.advance(0.2)  # t = 3004.5
    assert flush_active is True
    ev4 = SimpleMockEvent("Turn 2 - Msg 4", "user_x", "group_1", 4)
    await debounce.ingest("group_1", "user_x", ev4.text, ev4, slow_on_flush)

    concurrent_arrival_observed = True

    # 4. Advance past slow_on_flush completion: t = 3005.6 (> 3005.5)
    await vc.advance(1.1)
    assert flush_active is False
    assert turn1_completed is True
    # Turn 2 should NOT have flushed yet (latest message was at 3004.5, cooldown 3.5s -> due at 3008.0)
    assert len(recorder.results) == 1

    # 5. Advance to t = 3007.9 (just before Turn 2 cooldown expires)
    await vc.advance(2.3)
    assert len(recorder.results) == 1

    # 6. Advance to t = 3008.1 -> Turn 2 flushes!
    await vc.advance(0.2)
    assert len(recorder.results) == 2

    # Verification:
    assert concurrent_arrival_observed is True
    res1 = recorder.results[0]
    res2 = recorder.results[1]

    assert len(res1.messages) == 1
    assert res1.messages[0].text == "Turn 1 - Msg 1"

    assert len(res2.messages) == 3
    assert [m.text for m in res2.messages] == [
        "Turn 2 - Msg 2",
        "Turn 2 - Msg 3",
        "Turn 2 - Msg 4",
    ]
    await vc.advance(2.0)
    assert vc.pending_timers_count == 0


@pytest.mark.asyncio
async def test_reentrant_ingest_inside_on_flush():
    """Verify that calling debounce.ingest from inside on_flush does not cause deadlock or slot corruption."""
    vc = VirtualClock(initial_time=4000.0)
    recorder = ConcurrentRecorder()
    debounce = DebounceBuffer(time_service=vc, base_cooldown=3.5)

    reentrant_triggered = False

    async def reentrant_callback(result: DebounceResult) -> None:
        nonlocal reentrant_triggered
        recorder.results.append(result)
        if result.consolidated_text == "First":
            reentrant_triggered = True
            # Reentrantly ingest a follow-up message for the same user
            ev_followup = SimpleMockEvent("Synthetic Followup", result.user_id, result.session_id, 99)
            await debounce.ingest(result.session_id, result.user_id, ev_followup.text, ev_followup, reentrant_callback)

    ev = SimpleMockEvent("First", "user_reentrant", "group_1", 1)
    await debounce.ingest("group_1", "user_reentrant", ev.text, ev, reentrant_callback)

    # Advance 3.6s -> triggers first flush and reentrant ingestion
    await vc.advance(3.6)
    assert reentrant_triggered is True
    assert len(recorder.results) == 1
    assert recorder.results[0].consolidated_text == "First"

    # Advance 3.6s more -> triggers follow-up flush
    await vc.advance(3.6)
    assert len(recorder.results) == 2
    assert recorder.results[1].consolidated_text == "Synthetic Followup"
    assert vc.pending_timers_count == 0


@pytest.mark.asyncio
async def test_flush_all_during_active_on_flush():
    """Verify calling flush_all() while an on_flush is currently sleeping does not dead-lock or drop pending slots."""
    vc = VirtualClock(initial_time=5000.0)
    results_collected: List[str] = []

    async def blocking_callback(res: DebounceResult) -> None:
        results_collected.append(f"cb:{res.user_id}:{res.consolidated_text}")
        await vc.sleep(2.0)

    debounce = DebounceBuffer(time_service=vc, base_cooldown=3.5)

    # Ingest User A and User B
    ea = SimpleMockEvent("User A Msg", "user_a", "group_1", 1)
    eb = SimpleMockEvent("User B Msg", "user_b", "group_1", 2)
    await debounce.ingest("group_1", "user_a", ea.text, ea, blocking_callback)
    await vc.advance(1.0)
    await debounce.ingest("group_1", "user_b", eb.text, eb, blocking_callback)

    # Advance 2.6s (total 3.6s since user_a): User A's timer fires and starts blocking_callback
    await vc.advance(2.6)
    assert len(results_collected) == 1
    assert "user_a" in results_collected[0]

    # User B is still pending in buffer (due at 1.0 + 3.5 = 4.5s, current time is 3.6s).
    # Call flush_all() right now while user_a's callback is running!
    flush_task = asyncio.create_task(debounce.flush_all())
    await asyncio.sleep(0)
    assert flush_task.done() is False

    # Advance time to allow all callbacks to finish. flush_all now guarantees
    # callback completion before it returns.
    await vc.advance(5.0)
    manual_flushed = await flush_task
    assert len(manual_flushed) == 1
    assert manual_flushed[0].user_id == "user_b"
    assert len(results_collected) == 2
    assert vc.pending_timers_count == 0


# ---------------------------------------------------------------------------
# Scope 3: Edge-Case Timer Boundaries (t=3.499s vs t=3.501s, 6.499s vs 6.501s, 12.0s)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boundary_t_3499_resets_timer_into_single_turn():
    """Sub-millisecond boundary: Message arriving at t = 3.499s (< 3.500s base cooldown).

    Oracle:
    - At t = 3.499s: Message 2 arrives before Message 1 timer fires.
    - Timer must be reset to 3.499 + 3.500 = 6.999s.
    - At t = 3.500s: Exactly 0 turns flushed!
    - At t = 6.998s: Exactly 0 turns flushed!
    - At t = 7.000s: Exactly 1 turn flushed containing BOTH messages!
    """
    vc = VirtualClock(initial_time=1000.0)
    recorder = ConcurrentRecorder()
    debounce = DebounceBuffer(time_service=vc, base_cooldown=3.500)

    e1 = SimpleMockEvent("Msg 1", "u1", "g1", 1)
    e2 = SimpleMockEvent("Msg 2", "u1", "g1", 2)

    # t = 1000.000
    await debounce.ingest("g1", "u1", e1.text, e1, recorder.on_flush)

    # Advance 3.499s -> t = 1003.499
    await vc.advance(3.499)
    assert len(recorder.results) == 0, "Timer fired prematurely before 3.500s"

    # Ingest Message 2 at t = 1003.499
    await debounce.ingest("g1", "u1", e2.text, e2, recorder.on_flush)

    # Advance 0.001s -> t = 1003.500 (original M1 deadline)
    await vc.advance(0.001)
    assert len(recorder.results) == 0, "Timer was not reset by message at t=3.499s!"

    # Advance to t = 1006.998 (3.499s since M2)
    await vc.advance(3.498)
    assert len(recorder.results) == 0, "Flushed before reset cooldown expired"

    # Advance to t = 1007.000 (3.501s since M2)
    await vc.advance(0.002)
    assert len(recorder.results) == 1, "Failed to flush turn after reset cooldown"

    res = recorder.results[0]
    assert len(res.messages) == 2
    assert res.messages[0].text == "Msg 1"
    assert res.messages[1].text == "Msg 2"
    assert res.consolidated_text == "Msg 1\nMsg 2"
    assert vc.pending_timers_count == 0


@pytest.mark.asyncio
async def test_boundary_t_3501_splits_into_two_distinct_turns():
    """Sub-millisecond boundary: Message arriving at t = 3.501s (> 3.500s base cooldown).

    Oracle:
    - At t = 3.500s: Message 1 cooldown expires and flushes Turn 1.
    - At t = 3.501s: Message 2 arrives, creating a new Turn 2.
    - Exactly 1 turn flushed at t = 3.501s.
    - Turn 2 flushes at t = 3.501 + 3.500 = 7.001s.
    - Total: 2 distinct turns flushed.
    """
    vc = VirtualClock(initial_time=1000.0)
    recorder = ConcurrentRecorder()
    debounce = DebounceBuffer(time_service=vc, base_cooldown=3.500)

    e1 = SimpleMockEvent("Msg 1", "u1", "g1", 1)
    e2 = SimpleMockEvent("Msg 2", "u1", "g1", 2)

    # t = 1000.000
    await debounce.ingest("g1", "u1", e1.text, e1, recorder.on_flush)

    # Advance 3.501s -> t = 1003.501 (past 3.500s timer)
    await vc.advance(3.501)
    assert len(recorder.results) == 1, "Turn 1 did not flush at t=3.500s"
    assert recorder.results[0].consolidated_text == "Msg 1"

    # Ingest Message 2 at t = 1003.501
    await debounce.ingest("g1", "u1", e2.text, e2, recorder.on_flush)
    assert len(recorder.results) == 1

    # Advance 3.499s -> t = 1007.000 (3.499s since M2)
    await vc.advance(3.499)
    assert len(recorder.results) == 1

    # Advance 0.002s -> t = 1007.002 (3.501s since M2)
    await vc.advance(0.002)
    assert len(recorder.results) == 2, "Turn 2 did not flush after its cooldown"
    assert recorder.results[1].consolidated_text == "Msg 2"
    assert vc.pending_timers_count == 0


@pytest.mark.asyncio
async def test_boundary_extended_cooldown_6499_vs_6501():
    """Verify sub-millisecond boundary for incompleteness extended cooldown (6.500s).

    Case A: Message 2 arrives at t = 6.499s -> resets timer, merged into 1 turn.
    Case B: Message 2 arrives at t = 6.501s -> flushes Turn 1, starts Turn 2.
    """
    # Case A: 6.499s
    vc_a = VirtualClock(1000.0)
    rec_a = ConcurrentRecorder()
    buf_a = DebounceBuffer(time_service=vc_a, base_cooldown=3.5, extended_cooldown=6.5)

    ea1 = SimpleMockEvent("我之所以提这个PR，因为...", "u1", "g1", 1)
    ea2 = SimpleMockEvent("发现了严重的性能瓶颈。", "u1", "g1", 2)
    await buf_a.ingest("g1", "u1", ea1.text, ea1, rec_a.on_flush)

    # At 6.499s: not flushed yet
    await vc_a.advance(6.499)
    assert len(rec_a.results) == 0
    await buf_a.ingest("g1", "u1", ea2.text, ea2, rec_a.on_flush)
    # At 6.500s: still 0 flushes
    await vc_a.advance(0.001)
    assert len(rec_a.results) == 0
    # Merged turn flushes after base cooldown of 3.5s from ea2 (at 6.499 + 3.5 = 9.999s)
    await vc_a.advance(3.500)
    assert len(rec_a.results) == 1
    assert len(rec_a.results[0].messages) == 2

    # Case B: 6.501s
    vc_b = VirtualClock(1000.0)
    rec_b = ConcurrentRecorder()
    buf_b = DebounceBuffer(time_service=vc_b, base_cooldown=3.5, extended_cooldown=6.5)

    eb1 = SimpleMockEvent("我之所以提这个PR，因为...", "u1", "g1", 1)
    eb2 = SimpleMockEvent("发现了严重的性能瓶颈。", "u1", "g1", 2)
    await buf_b.ingest("g1", "u1", eb1.text, eb1, rec_b.on_flush)

    # At 6.501s: Turn 1 flushed
    await vc_b.advance(6.501)
    assert len(rec_b.results) == 1
    assert rec_b.results[0].was_extended is True

    await buf_b.ingest("g1", "u1", eb2.text, eb2, rec_b.on_flush)
    await vc_b.advance(3.6)
    assert len(rec_b.results) == 2
    assert rec_b.results[1].consolidated_text == "发现了严重的性能瓶颈。"


@pytest.mark.asyncio
async def test_boundary_hard_cap_12s_exact_boundary():
    """Verify exact 12.000s hard cap boundary.

    Stream of incomplete messages: t=0, 3, 6, 9, 11.5.
    At t = 11.999s, remaining budget is 0.001s.
    Target cooldown is min(6.5, 0.001) = 0.001s.
    Timer must flush at exactly t = 12.000s.
    A message arriving at t = 12.001s must start a fresh turn with reset 12.0s budget.
    """
    vc = VirtualClock(1000.0)
    recorder = ConcurrentRecorder()
    buf = DebounceBuffer(
        time_service=vc,
        base_cooldown=3.5,
        extended_cooldown=6.5,
        max_cap=12.0,
    )

    timestamps = [0.0, 3.0, 6.0, 9.0, 11.5]
    for idx, t in enumerate(timestamps):
        if idx > 0:
            dt = t - timestamps[idx - 1]
            await vc.advance(dt)
        ev = SimpleMockEvent(f"第{idx+1}点，但是...", "u1", "g1", idx + 1)
        await buf.ingest("g1", "u1", ev.text, ev, recorder.on_flush)

    # Current time = 1011.5
    # Advance to 1011.999 (0.499s advance)
    await vc.advance(0.499)
    assert len(recorder.results) == 0

    # Ingest message 6 at t = 1011.999 (0.001s before hard cap 1012.0)
    ev6 = SimpleMockEvent("第6点，而且...", "u1", "g1", 6)
    await buf.ingest("g1", "u1", ev6.text, ev6, recorder.on_flush)
    assert len(recorder.results) == 0

    # Advance 0.002s to t = 1012.001 (past 12.0s hard cap)
    await vc.advance(0.002)
    assert len(recorder.results) == 1
    assert len(recorder.results[0].messages) == 6
    assert recorder.results[0].duration >= 11.999

    # Ingest message 7 at t = 1012.001 -> must open fresh turn
    ev7 = SimpleMockEvent("崭新的一个话题。", "u1", "g1", 7)
    await buf.ingest("g1", "u1", ev7.text, ev7, recorder.on_flush)
    assert len(recorder.results) == 1

    # Advance 3.6s -> Turn 2 flushes cleanly
    await vc.advance(3.6)
    assert len(recorder.results) == 2
    assert len(recorder.results[1].messages) == 1
    assert recorder.results[1].consolidated_text == "崭新的一个话题。"
    assert vc.pending_timers_count == 0


# ---------------------------------------------------------------------------
# Scope 4: SystemClock Real-Time Concurrency, Task Leaks & FIFO Stress
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_clock_real_time_concurrency_and_zero_task_leaks():
    """True wall-clock concurrency stress test using SystemClock.

    Validates:
    - 12 concurrent async worker coroutines (simulating 12 distinct users)
    - High-frequency randomized message arrival (5ms - 25ms intervals)
    - 120 total messages ingested
    - Complete message preservation (no drops, no duplicates)
    - Strict per-user FIFO order verification
    - Zero orphaned asyncio Tasks in asyncio.all_tasks()
    - Zero unhandled exceptions or race condition deadlocks
    """
    sc = SystemClock()
    recorder = ConcurrentRecorder()
    debounce = DebounceBuffer(
        time_service=sc,
        base_cooldown=0.05,        # 50ms cooldown for fast real-time test
        extended_cooldown=0.08,    # 80ms
        max_cap=0.20,              # 200ms
    )

    num_users = 12
    messages_per_user = 10
    total_expected = num_users * messages_per_user  # 120

    # Capture active tasks before test
    initial_tasks = {t for t in asyncio.all_tasks() if not t.done()}

    user_sent_map: Dict[str, List[int]] = {f"user_{u}": [] for u in range(num_users)}

    async def worker(u_idx: int):
        u_id = f"user_{u_idx}"
        for seq in range(messages_per_user):
            # Incomplete message on odd sequences to trigger extended cooldown branch
            if seq % 2 == 1:
                text = f"User {u_id} frag {seq}, 但是..."
            else:
                text = f"User {u_id} frag {seq}."

            ev = SimpleMockEvent(text=text, sender_id=u_id, group_id="group_stress", seq=seq)
            user_sent_map[u_id].append(seq)
            await debounce.ingest("group_stress", u_id, ev.text, ev, recorder.on_flush)

            # Jittered arrival interval: 5ms to 20ms
            await asyncio.sleep(random.uniform(0.005, 0.020))

    # Run all 12 workers concurrently
    await asyncio.gather(*(worker(i) for i in range(num_users)))

    # Allow cooldowns to expire for all remaining slots (0.08s extended cooldown + margin)
    await asyncio.sleep(0.15)

    # Verification 1: Total message preservation (120 messages)
    flushed_messages = [item for res in recorder.results for item in res.messages]
    assert len(flushed_messages) == total_expected, (
        f"Message loss! Expected {total_expected} messages, received {len(flushed_messages)}"
    )

    # Verification 2: Strict per-user FIFO ordering across all flushed turns
    flushed_by_user: Dict[str, List[int]] = {f"user_{u}": [] for u in range(num_users)}
    for res in recorder.results:
        assert res.session_id == "group_stress"
        for item in res.messages:
            flushed_by_user[res.user_id].append(item.event.seq)

    for u_id, sent_seqs in user_sent_map.items():
        recv_seqs = flushed_by_user[u_id]
        assert recv_seqs == sent_seqs, (
            f"FIFO violation for {u_id}: sent {sent_seqs} vs received {recv_seqs}"
        )

    # Verification 3: Zero active debounces remaining
    for u in range(num_users):
        assert debounce.is_active("group_stress", f"user_{u}") is False

    # Verification 4: Zero task leaks (no stray _timer_worker tasks)
    # Wait one tick for any finished tasks to finalize done() state
    await asyncio.sleep(0.02)
    current_tasks = {t for t in asyncio.all_tasks() if not t.done()}
    leaked_tasks = [
        t for t in (current_tasks - initial_tasks)
        if "_timer_worker" in str(t) or "simulate" in str(t)
    ]
    assert len(leaked_tasks) == 0, f"Detected {len(leaked_tasks)} leaked asyncio tasks: {leaked_tasks}"

    # Clean shutdown
    await debounce.close()
