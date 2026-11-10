import asyncio
import copy

import pytest

from .test_plugin_lifecycle import At, MockEvent, _plugin
from ..core.config import parse_runtime_config
from ..core.platform_bridge import SendResult
from ..core.runtime_persistence import export_runtime_state, restore_runtime_state
from ..core.time_service import VirtualClock

PEER = "3789469063"


@pytest.fixture
def setup_peer():
    clock = VirtualClock(initial_time=5000)
    plugin = _plugin({"peer_bot_ids": [PEER], "peer_bot_max_rounds": 3}, clock)
    runtime = plugin._get_or_create_runtime("mock:GroupMessage:g", group_id="g", bot_id="bot_42")
    return plugin, runtime, clock


@pytest.mark.asyncio
async def test_three_delivered_rounds_block_next_even_at_ingress(setup_peer):
    plugin, runtime, _ = setup_peer
    for i in range(3):
        event = MockEvent("hello", sender_id=PEER, group_id="g", message_id=f"in{i}")
        first = await plugin._send_owned(runtime, event, "first")
        tail = await plugin._send_owned(runtime, copy.copy(event), "tail")
        assert first.success and tail.success
    fourth = MockEvent("hello", sender_id=PEER, group_id="g", message_id="in3", components=[At("bot_42")])
    assert not (await plugin._send_owned(runtime, fourth, "blocked")).success
    await plugin.on_group_message(fourth)
    assert fourth.is_stopped and fourth._chat_dynamics_blocks_native and not fourth.replies_sent
    assert len(runtime.peer_bot_rounds[PEER]["sources"]) == 3
    assert not plugin.context.agent_prompts
    await plugin.terminate()


@pytest.mark.asyncio
async def test_concurrent_sends_do_not_exceed_limit_and_failure_is_free(setup_peer):
    plugin, runtime, _ = setup_peer

    async def fail(_event):
        return SendResult(False, error="synthetic_failure")

    failed = MockEvent("hello", sender_id=PEER, group_id="g", message_id="failed")
    assert not (await plugin._send_owned(runtime, failed, "no", send_effect=fail)).success

    async def slow_ok(_event):
        await asyncio.sleep(0)
        return SendResult(True)

    events = [MockEvent("hello", sender_id=PEER, group_id="g", message_id=f"c{i}") for i in range(8)]
    results = await asyncio.gather(*(plugin._send_owned(runtime, e, "yes", send_effect=slow_ok) for e in events))
    assert sum(r.success for r in results) == 3
    assert not runtime.peer_bot_send_lock.locked()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_idle_reset_is_extended_by_blocked_peer_only_and_sessions_are_isolated(setup_peer):
    plugin, runtime, clock = setup_peer
    for i in range(3):
        await plugin._send_owned(runtime, MockEvent("hi", sender_id=PEER, message_id=str(i)), "ok")
    human = MockEvent("hi", sender_id="human", message_id="h")
    assert (await plugin._send_owned(runtime, human, "ok")).success
    other = plugin._get_or_create_runtime("mock:GroupMessage:other", group_id="other")
    peer = MockEvent("hi", sender_id=PEER, message_id="new")
    assert plugin._peer_reply_allowed(other, peer)
    await clock.advance(1799)
    assert not plugin._peer_reply_allowed(runtime, peer, touch=True)
    await clock.advance(1799)
    assert not plugin._peer_reply_allowed(runtime, peer)
    await clock.advance(1)
    assert plugin._peer_reply_allowed(runtime, peer)
    assert (await plugin._send_owned(runtime, peer, "new conversation")).success
    assert len(runtime.peer_bot_rounds[PEER]["sources"]) == 1
    await plugin.terminate()


@pytest.mark.asyncio
async def test_peer_counter_survives_snapshot_and_explicit_reset_clears_it(setup_peer):
    plugin, runtime, clock = setup_peer
    for i in range(3):
        await plugin._send_owned(runtime, MockEvent("hi", sender_id=PEER, message_id=str(i)), "ok")
    target = _plugin({"peer_bot_ids": [PEER]}, clock)
    restore_runtime_state(target, export_runtime_state(plugin))
    restored = target._sessions[runtime.session_key]
    peer = MockEvent("hi", sender_id=PEER, message_id="new")
    assert not target._peer_reply_allowed(restored, peer)
    restored.reset_conversation_state()
    assert target._peer_reply_allowed(restored, peer)
    await target.terminate()
    await plugin.terminate()


@pytest.mark.parametrize("value", [0, -1, 1.5, 101, "bad"])
def test_invalid_round_count_falls_back_to_three(value):
    cfg, warnings = parse_runtime_config({"peer_bot_max_rounds": value})
    assert cfg.peer_bot_max_rounds == 3
    if value != "bad":
        assert any("peer_bot_max_rounds" in w for w in warnings)


@pytest.mark.asyncio
async def test_cancelled_send_releases_lock_and_does_not_count(setup_peer):
    plugin, runtime, _ = setup_peer
    entered = asyncio.Event()

    async def hang(_event):
        entered.set()
        await asyncio.Event().wait()

    peer = MockEvent("hi", sender_id=PEER, message_id="cancelled")
    task = asyncio.create_task(plugin._send_owned(runtime, peer, "hi", send_effect=hang))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.peer_bot_send_lock.locked()
    assert not runtime.peer_bot_rounds[PEER]["sources"]
    assert (await plugin._send_owned(runtime, peer, "retry")).success
    await plugin.terminate()


@pytest.mark.asyncio
async def test_limited_peer_at_uses_owned_delivery_instead_of_native_fast_path(setup_peer):
    plugin, runtime, clock = setup_peer
    flushed = []

    async def capture(result):
        flushed.append(result)

    plugin.on_turn_flushed = capture
    plugin._is_fast_path_turn = lambda *_: True
    event = MockEvent("hello", sender_id=PEER, group_id="g", message_id="first", components=[At("bot_42")])
    await plugin.on_group_message(event)
    await clock.advance(5)
    assert event._chat_dynamics_blocks_native
    assert not plugin._native_context_by_event
    assert len(flushed) == 1
    assert not flushed[0].metadata.get("native_pipeline")
    assert event._chat_dynamics_peer_round_source == "first"
    await plugin.terminate()
