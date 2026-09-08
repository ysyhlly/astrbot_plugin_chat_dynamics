from __future__ import annotations

import asyncio
from collections import deque

import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityLevel
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import FollowupBatch, SessionRuntime
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import At, MockEvent, _plugin, _session_key


def test_clear_hovers_for_user_preserves_other_authors_and_latest_pointer():
    runtime = SessionRuntime("session", "group", "umo")
    runtime.dag = ConversationDAG(session_id="session")
    first = runtime.dag.add_message("hover-a", "alice", "架构方案", timestamp=1.0)
    second = runtime.dag.add_message("hover-b", "bob", "部署细节", timestamp=2.0)
    runtime.remember_hover(first, now=1.0)
    runtime.remember_hover(second, now=2.0)

    runtime.clear_hovers_for_user("bob")

    assert list(runtime.pending_hovers) == [first]
    assert runtime.pending_hover is first


def test_hover_from_another_user_still_resolves_a_later_turn():
    plugin = _plugin()
    key = _session_key("hover-social")
    runtime = plugin._get_or_create_runtime(key, group_id="hover-social", umo=key, bot_id="bot_42")
    bot = runtime.dag.add_message("bot", "bot_42", "架构方案", timestamp=1.0)
    runtime.last_bot_node = bot
    alice_hover = runtime.dag.add_message("hover-a", "alice", "架构方案初步想法", timestamp=2.0)
    bob_hover = runtime.dag.add_message("hover-b", "bob", "部署细节", timestamp=3.0)
    runtime.remember_hover(alice_hover, now=2.0)
    runtime.remember_hover(bob_hover, now=3.0)

    runtime.clear_hovers_for_user("bob")
    candidate = runtime.dag.add_message("alice-followup", "alice", "架构方案继续说明", timestamp=10.0)
    score = plugin.addressivity_router.compute_addressivity(
        candidate,
        runtime.dag,
        last_bot_node=bot,
        bot_id=runtime.bot_id,
        prior_hover=runtime.pending_hover,
        prior_hovers=list(runtime.pending_hovers),
    )

    assert list(runtime.pending_hovers) == [alice_hover]
    assert score.level == AddressivityLevel.STRONG


async def _prepare_native_batch(clock: VirtualClock, *, user_id: str = "alice"):
    plugin = _plugin({"base_thinking_delay": 0.0}, clock=clock)
    sent_fragments = []

    async def send(_runtime, _event, fragment, *, reply_to_id=None):
        sent_fragments.append(fragment)
        return SendResult(True, f"out-{len(sent_fragments)}")

    plugin._send_owned = send
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["首段", "尾段一", "尾段二"]
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 5.0
    event = MockEvent(
        "小助手 请回答这个问题",
        sender_id=user_id,
        group_id="native-social",
        message_id="native-in",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(event)
    event.set_result("native response")
    await plugin.on_decorating_result(event)
    runtime = plugin._sessions[event.unified_msg_origin]
    assert event.get_result() == "首段"
    await event.send(event.get_result())
    native_context = plugin._native_context_by_event[(event.unified_msg_origin, id(event))]
    assert native_context.fragments == ("首段", "尾段一", "尾段二")
    return plugin, runtime, event, sent_fragments


async def _start_tail_and_wait_for_sleep(plugin, event):
    task = asyncio.create_task(plugin.after_message_sent(event))
    for _ in range(8):
        await asyncio.sleep(0)
        if plugin.time_service.pending_timers_count:
            break
    assert plugin.time_service.pending_timers_count >= 1
    return task


@pytest.mark.asyncio
async def test_same_user_strong_ingress_keeps_first_native_segment_only():
    clock = VirtualClock(initial_time=100.0)
    plugin, runtime, event, sent_fragments = await _prepare_native_batch(clock)
    tail = await _start_tail_and_wait_for_sleep(plugin, event)

    interrupt = MockEvent(
        "小助手 继续回答这个问题",
        sender_id="alice",
        group_id="native-social",
        message_id="native-interrupt",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(interrupt)
    await clock.advance(5.0)
    await tail

    assert sent_fragments == []
    assert [node.text for node in runtime.dag.get_recent_nodes(limit=8) if node.user_id == "bot_42"] == ["首段"]
    assert not runtime.followup_queue
    assert runtime.active_followup_batch is None
    await plugin.terminate()


@pytest.mark.asyncio
async def test_other_user_strong_ingress_does_not_cancel_native_tail():
    clock = VirtualClock(initial_time=100.0)
    plugin, runtime, event, sent_fragments = await _prepare_native_batch(clock)
    tail = await _start_tail_and_wait_for_sleep(plugin, event)

    other = MockEvent(
        "小助手 继续回答这个问题",
        sender_id="bob",
        group_id="native-social",
        message_id="native-other",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(other)
    await clock.advance(5.0)
    await clock.advance(5.0)
    await tail

    assert sent_fragments == ["尾段一", "尾段二"]
    assert [node.text for node in runtime.dag.get_recent_nodes(limit=8) if node.user_id == "bot_42"] == [
        "首段",
        "尾段一",
        "尾段二",
    ]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_same_user_weak_ingress_does_not_cancel_native_tail():
    clock = VirtualClock(initial_time=100.0)
    plugin, runtime, event, sent_fragments = await _prepare_native_batch(clock)
    tail = await _start_tail_and_wait_for_sleep(plugin, event)

    weak = MockEvent(
        "这是一条普通群聊消息",
        sender_id="alice",
        group_id="native-social",
        message_id="native-weak",
    )
    await plugin.on_group_message(weak)
    await clock.advance(5.0)
    await clock.advance(5.0)
    await tail

    assert sent_fragments == ["尾段一", "尾段二"]
    assert runtime.active_followup_batch is None
    await plugin.terminate()


@pytest.mark.asyncio
async def test_strong_ingress_from_another_umo_does_not_cancel_native_tail():
    clock = VirtualClock(initial_time=100.0)
    plugin, runtime, event, sent_fragments = await _prepare_native_batch(clock)
    tail = await _start_tail_and_wait_for_sleep(plugin, event)

    other_umo = MockEvent(
        "小助手 继续回答这个问题",
        sender_id="alice",
        group_id="native-social",
        message_id="native-other-umo",
        components=[At(qq="bot_42")],
    )
    other_umo.unified_msg_origin = "mock:GroupMessage:other-umo"
    await plugin.on_group_message(other_umo)
    await clock.advance(5.0)
    await clock.advance(5.0)
    await tail

    assert sent_fragments == ["尾段一", "尾段二"]
    assert runtime.active_followup_batch is None
    await plugin.terminate()


@pytest.mark.asyncio
async def test_duplicate_strong_ingress_hook_does_not_cancel_its_existing_batch():
    clock = VirtualClock(initial_time=100.0)
    plugin, runtime, event, sent_fragments = await _prepare_native_batch(clock)
    await plugin.on_group_message(event)
    assert (event.unified_msg_origin, id(event)) in plugin._native_context_by_event
    tail = asyncio.create_task(plugin.after_message_sent(event))
    await clock.advance(5.0)
    await clock.advance(5.0)
    await tail

    assert sent_fragments == ["尾段一", "尾段二"]
    assert plugin._metrics["duplicate_ignored"] >= 1
    await plugin.terminate()


def test_followup_invalidation_is_user_scoped_and_tokenized():
    runtime = SessionRuntime("session", "group", "umo")
    runtime.dag = ConversationDAG(session_id="session")
    alice = runtime.dag.add_message("alice", "alice", "请求", timestamp=1.0)
    bob = runtime.dag.add_message("bob", "bob", "请求", timestamp=2.0)
    alice_batch = FollowupBatch(
        fragments=deque(["alice-tail"]),
        trigger_node=alice,
        trigger_user_id="alice",
        delivery_token=7,
    )
    bob_batch = FollowupBatch(
        fragments=deque(["bob-tail"]),
        trigger_node=bob,
        trigger_user_id="bob",
        delivery_token=8,
    )
    runtime.followup_queue.extend([alice_batch, bob_batch])

    assert runtime.invalidate_followups_for_user("alice") == 1
    assert alice_batch.invalidated is True
    assert alice_batch.delivery_token == 8
    assert list(alice_batch.fragments) == []
    assert list(runtime.followup_queue) == [bob_batch]


@pytest.mark.asyncio
async def test_overlapping_native_callbacks_keep_each_users_tail_and_parent_chain():
    clock = VirtualClock(initial_time=100.0)
    plugin = _plugin({"base_thinking_delay": 0.0}, clock=clock)
    sent_by_event = {}

    async def send(_runtime, event, fragment, *, reply_to_id=None):
        sent_by_event.setdefault(event.message_id, []).append((fragment, reply_to_id))
        return SendResult(True, f"out-{event.message_id}-{len(sent_by_event[event.message_id])}")

    plugin._send_owned = send
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 5.0

    def shape(text, **_kwargs):
        if text == "alice native response":
            return ["Alice首段", "Alice尾段"]
        if text == "bob native response":
            return ["Bob首段", "Bob尾段"]
        return [text]

    plugin.pacer.shape_and_fragment = shape
    alice = MockEvent(
        "小助手 Alice的问题内容",
        sender_id="alice",
        group_id="native-overlap",
        message_id="alice-native-in",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(alice)
    alice.set_result("alice native response")
    await plugin.on_decorating_result(alice)
    runtime = plugin._sessions[alice.unified_msg_origin]
    assert alice.get_result() == "Alice首段"
    await alice.send(alice.get_result())
    alice_tail = await _start_tail_and_wait_for_sleep(plugin, alice)

    bob = MockEvent(
        "小助手 Bob的问题内容",
        sender_id="bob",
        group_id="native-overlap",
        message_id="bob-native-in",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(bob)
    bob.set_result("bob native response")
    await plugin.on_decorating_result(bob)
    assert bob.get_result() == "Bob首段"
    await bob.send(bob.get_result())
    bob_tail = asyncio.create_task(plugin.after_message_sent(bob))
    for _ in range(16):
        await asyncio.sleep(0)
        if len(runtime.active_followup_batches) == 2:
            break
    assert len(runtime.active_followup_batches) == 2

    await clock.advance(5.0)
    await clock.advance(5.0)
    await clock.advance(5.0)
    await clock.advance(5.0)
    await asyncio.gather(alice_tail, bob_tail)

    assert [item[0] for item in sent_by_event[alice.message_id]] == ["Alice尾段"]
    assert [item[0] for item in sent_by_event[bob.message_id]] == ["Bob尾段"]
    dag = runtime.dag
    alice_bots = [node for node in dag.nodes.values() if node.user_id == "bot_42" and node.text.startswith("Alice")]
    bob_bots = [node for node in dag.nodes.values() if node.user_id == "bot_42" and node.text.startswith("Bob")]
    assert len(alice_bots) == 2
    assert len(bob_bots) == 2
    assert alice_bots[1].parent_ids == {alice_bots[0].msg_id}
    assert bob_bots[1].parent_ids == {bob_bots[0].msg_id}
    assert runtime.active_followup_batches == {}
    assert runtime.active_followup_batch is None
    await plugin.terminate()


@pytest.mark.asyncio
async def test_same_user_interrupt_cancels_its_active_tail_but_preserves_other_user():
    clock = VirtualClock(initial_time=100.0)
    plugin = _plugin({"base_thinking_delay": 0.0}, clock=clock)
    sent_by_event = {}

    async def send(_runtime, event, fragment, *, reply_to_id=None):
        sent_by_event.setdefault(event.message_id, []).append((fragment, reply_to_id))
        return SendResult(True, f"out-{event.message_id}-{len(sent_by_event[event.message_id])}")

    plugin._send_owned = send
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 5.0
    plugin.pacer.shape_and_fragment = lambda text, **_kwargs: {
        "alice native response": ["Alice首段", "Alice尾段"],
        "bob native response": ["Bob首段", "Bob尾段"],
    }.get(text, [text])
    alice = MockEvent(
        "小助手 Alice的问题内容",
        sender_id="alice",
        group_id="native-cancel-overlap",
        message_id="alice-cancel-in",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(alice)
    alice.set_result("alice native response")
    await plugin.on_decorating_result(alice)
    runtime = plugin._sessions[alice.unified_msg_origin]
    await alice.send(alice.get_result())
    alice_tail = await _start_tail_and_wait_for_sleep(plugin, alice)

    bob = MockEvent(
        "小助手 Bob的问题内容",
        sender_id="bob",
        group_id="native-cancel-overlap",
        message_id="bob-cancel-in",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(bob)
    bob.set_result("bob native response")
    await plugin.on_decorating_result(bob)
    await bob.send(bob.get_result())
    bob_tail = asyncio.create_task(plugin.after_message_sent(bob))
    for _ in range(16):
        await asyncio.sleep(0)
        if len(runtime.active_followup_batches) == 2:
            break
    assert len(runtime.active_followup_batches) == 2

    interrupt = MockEvent(
        "小助手 Alice的新问题内容",
        sender_id="alice",
        group_id="native-cancel-overlap",
        message_id="alice-cancel-new",
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(interrupt)
    assert any(batch.invalidated for batch in runtime.active_followup_batches.values())

    await clock.advance(5.0)
    await clock.advance(5.0)
    await asyncio.gather(alice_tail, bob_tail)

    assert sent_by_event.get(alice.message_id, []) == []
    assert [item[0] for item in sent_by_event[bob.message_id]] == ["Bob尾段"]
    assert runtime.active_followup_batches == {}
    await plugin.terminate()


@pytest.mark.asyncio
async def test_followup_registry_is_cleared_by_reset_and_terminate():
    clock = VirtualClock(initial_time=100.0)
    plugin = _plugin(clock=clock)
    key = _session_key("registry-cleanup")
    runtime = plugin._get_or_create_runtime(key, group_id="registry-cleanup", umo=key, bot_id="bot_42")
    runtime.register_active_followup_batch(
        FollowupBatch(
            fragments=deque(["reset-tail"]),
            epoch=runtime.epoch,
            delivery_token=runtime.next_followup_delivery_token(),
        )
    )

    await plugin._reset_session_state_async(key)
    assert runtime.active_followup_batches == {}
    assert runtime.active_followup_batch is None

    runtime.register_active_followup_batch(
        FollowupBatch(
            fragments=deque(["terminate-tail"]),
            epoch=runtime.epoch,
            delivery_token=runtime.next_followup_delivery_token(),
        )
    )
    await plugin.terminate()
    assert runtime.active_followup_batches == {}
    assert runtime.active_followup_batch is None
