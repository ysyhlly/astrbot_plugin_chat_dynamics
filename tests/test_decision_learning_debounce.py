"""Prediction windows must survive infrastructure failures without duplicate sends."""
import asyncio

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock


@pytest.mark.asyncio
async def test_failed_timer_does_not_send_and_manual_flush_isolates_callback_failure():
    clock = VirtualClock(initial_time=100)

    async def broken_sleep(delay):
        raise OSError('timer service unavailable')

    clock.sleep = broken_sleep
    buffer = DebounceBuffer(time_service=clock)
    calls = []

    async def failed_delivery(result):
        calls.append(result.consolidated_text)
        raise RuntimeError('delivery unavailable')

    await buffer.ingest('room', 'user', '我還想補充', object(), failed_delivery)
    await asyncio.sleep(0)
    assert calls == []
    assert len(await buffer.flush_all()) == 1
    assert calls == ['我還想補充']
    assert await buffer.flush_all() == []
    await buffer.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_and_cancels_inflight_manual_flush():
    buffer = DebounceBuffer(time_service=VirtualClock(initial_time=100))
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def waiting_for_decision(result):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await buffer.ingest('room', 'user', '請等一下', object(), waiting_for_decision)
    task = asyncio.create_task(buffer.flush_all())
    await entered.wait()
    await buffer.close(flush=False)
    await task
    assert cancelled.is_set()
    assert not buffer._background_tasks


@pytest.mark.asyncio
async def test_hard_cap_failure_does_not_replay_turn_on_next_message():
    clock = VirtualClock(initial_time=100)
    buffer = DebounceBuffer(time_service=clock, max_cap=0)
    calls = []

    async def failed_decision(result):
        calls.append(result.consolidated_text)
        raise ValueError('invalid model answer')

    await buffer.ingest('room', 'user', '第一段', object(), failed_decision)
    await buffer.ingest('room', 'user', '新的一段', object(), failed_decision)
    assert calls == ['第一段', '新的一段']
    assert buffer.get_pending_count() == 0
    await buffer.close(flush=False)
