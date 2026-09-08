"""End-to-end member-stop contracts across legacy, persona, and native paths."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.native_delivery import NativeDeliveryGuard
from astrbot_plugin_chat_dynamics.core.platform_bridge import (
    build_plain_chain,
    chain_plain_text,
)
from astrbot_plugin_chat_dynamics.main import _NativeEventContext

from .test_persona_model import BridgeDouble
from .test_plugin_lifecycle import MockEvent, _plugin, _session_key


class NativeEvent(MockEvent):
    """Keep native results as MessageChains so the event guard sees identity."""

    def plain_result(self, text: str):
        return build_plain_chain(text)


async def _drain(plugin) -> None:
    """Wait for plugin-owned tasks without assuming a particular task count."""
    for _ in range(100):
        pending = [task for task in list(plugin._background_tasks) if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            continue
        await asyncio.sleep(0)
        if not any(not task.done() for task in plugin._background_tasks):
            return


def _stop_event(source: MockEvent, sender_id: str, message_id: str = "stop") -> MockEvent:
    event = MockEvent(
        "/dynamics_stop",
        sender_id=sender_id,
        group_id=source.group_id,
        message_id=message_id,
        is_admin_user=False,
    )
    event.unified_msg_origin = source.unified_msg_origin
    return event


def _bot_texts(runtime) -> list[str]:
    if runtime.dag is None:
        return []
    return [node.text for node in runtime.dag.nodes.values() if node.user_id == runtime.bot_id]


@pytest.mark.asyncio
async def test_stop_is_limited_to_sender_and_umo_and_preserves_session_state():
    plugin = _plugin({"debounce_base_cooldown": 10.0})
    try:
        alice = MockEvent("普通消息", sender_id="alice", group_id="room", message_id="alice-1")
        bob = MockEvent("另一条消息", sender_id="bob", group_id="room", message_id="bob-1")
        foreign = MockEvent("异平台消息", sender_id="alice", group_id="room", message_id="foreign-1")
        alice.unified_msg_origin = "platform-a:GroupMessage:room"
        bob.unified_msg_origin = alice.unified_msg_origin
        foreign.unified_msg_origin = "platform-b:GroupMessage:room"
        await plugin.on_group_message(alice)
        await plugin.on_group_message(bob)
        await plugin.on_group_message(foreign)

        key = alice.unified_msg_origin
        runtime = plugin._sessions[key]
        history = runtime.dag.add_message("history", "bob", "保留的历史", timestamp=plugin.time_service.time())
        runtime.epoch = 7
        plugin.arbiter.trigger_cooling(key, duration_seconds=120, current_time=plugin.time_service.time())
        owner_revision = runtime.user_revisions.get("alice", 0)
        bob_revision = runtime.user_revisions.get("bob", 0)
        foreign_runtime = plugin._sessions[foreign.unified_msg_origin]
        foreign_revision = foreign_runtime.user_revisions.get("alice", 0)

        await plugin.cmd_dynamics_stop(_stop_event(alice, "alice"))

        assert plugin.debounce.get_pending_count(key, user_id="alice") == 0
        assert plugin.debounce.get_pending_count(key, user_id="bob") == 1
        assert plugin.debounce.get_pending_count(foreign.unified_msg_origin, user_id="alice") == 1
        assert runtime.user_revisions.get("bob", 0) == bob_revision
        assert runtime.user_revisions.get("alice", 0) == owner_revision + 1
        assert runtime.epoch == 7
        assert runtime.dag.get_node(history.msg_id) is history
        assert plugin.arbiter.is_in_deep_cooling(key, current_time=plugin.time_service.time())
        assert foreign_runtime.user_revisions.get("alice", 0) == foreign_revision

        unknown = MockEvent("/dynamics_stop", sender_id="alice", group_id="unknown", message_id="unknown-stop")
        await plugin.cmd_dynamics_stop(unknown)
        assert _session_key("unknown") not in plugin._sessions
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_persona_stop_blocks_active_member_and_releases_queued_quota(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    plugin = _plugin(
        {
            "decision_mode": "persona_model",
            "debounce_base_cooldown": 10.0,
            "base_thinking_delay": 0.0,
        }
    )
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_reply():
        entered.set()
        await release.wait()

    bridge.before_reply = blocked_reply

    async def decide(**kwargs):
        payload = json.loads(kwargs["prompt"])
        ids = [item["message_id"] for item in payload["conversation"]["messages"]]
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "action": "reply",
                    "state": "focused",
                    "target_message_ids": ids,
                    "response_goal": "回应当前请求",
                    "length": "brief",
                    "reason_code": "member_stop_test",
                }
            )
        )

    plugin.context.llm_generate = decide
    first = MockEvent("帮我处理第一个", sender_id="alice", group_id="persona-stop", message_id="a1", is_at_or_wake_command=True)
    queued_alice = MockEvent("帮我处理排队项", sender_id="alice", group_id="persona-stop", message_id="a2", is_at_or_wake_command=True)
    queued_bob = MockEvent("帮我处理 Bob 请求", sender_id="bob", group_id="persona-stop", message_id="b1", is_at_or_wake_command=True)
    try:
        await plugin.on_group_message(first)
        await plugin.debounce.flush(session_id=first.unified_msg_origin)
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        await plugin.on_group_message(queued_alice)
        await plugin.debounce.flush(session_id=queued_alice.unified_msg_origin)
        await plugin.on_group_message(queued_bob)
        await plugin.debounce.flush(session_id=queued_bob.unified_msg_origin)
        runtime = plugin._sessions[first.unified_msg_origin]
        assert len(runtime.model_queue) == 2
        before_stop = runtime.model_admission._value

        await plugin.cmd_dynamics_stop(_stop_event(first, "alice", "stop-alice"))
        assert len(runtime.model_queue) == 1
        assert runtime.model_queue[0].context.author == "bob"
        assert runtime.model_admission._value == before_stop + 1

        release.set()
        await _drain(plugin)
        assert not first.replies_sent
        assert not queued_alice.replies_sent
        assert queued_bob.replies_sent
        assert runtime.model_admission._value == 9
    finally:
        release.set()
        await plugin.terminate()


@pytest.mark.asyncio
async def test_legacy_stop_drops_blocked_reply_and_allows_next_request():
    plugin = _plugin(
        {
            "pipeline_mode": "exclusive",
            "base_thinking_delay": 0.0,
            "chars_per_second": 100.0,
        }
    )
    plugin.pacer.min_typing_delay = 0.0
    plugin.pacer.max_typing_delay = 0.0
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def blocked_llm(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            started.set()
            await release.wait()
            text = "旧回复不应送达"
        else:
            text = "新请求恢复后的回复"
        return SimpleNamespace(completion_text=text)

    plugin.context.tool_loop_agent = blocked_llm
    old = MockEvent("小助手请处理旧请求", sender_id="alice", group_id="legacy-stop", message_id="old", is_at_or_wake_command=True)
    new = MockEvent("小助手请处理新请求", sender_id="alice", group_id="legacy-stop", message_id="new", is_at_or_wake_command=True)
    try:
        await plugin.on_group_message(old)
        await plugin.debounce.flush(session_id=old.unified_msg_origin)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert len(calls) == 1
        assert not release.is_set()
        await plugin.cmd_dynamics_stop(_stop_event(old, "alice", "stop-legacy"))
        await plugin.on_group_message(new)
        await plugin.debounce.flush(session_id=new.unified_msg_origin)
        release.set()
        await _drain(plugin)

        assert not old.replies_sent
        assert any("新请求恢复后的回复" in text for text in new.replies_sent)
        assert all("旧回复不应送达" not in text for text in old.replies_sent + new.replies_sent)
    finally:
        release.set()
        await plugin.terminate()


async def _prepare_native(plugin, *, group_id: str = "native-stop", sender_id: str = "alice"):
    event = NativeEvent(
        "小助手直接回复",
        sender_id=sender_id,
        group_id=group_id,
        message_id=f"trigger-{group_id}",
        is_at_or_wake_command=True,
    )
    await plugin.on_group_message(event)
    return event, plugin._sessions[event.unified_msg_origin]


@pytest.mark.asyncio
async def test_native_failed_head_after_hook_does_not_create_bot_state_or_tail():
    plugin = _plugin({"base_thinking_delay": 0.0})
    try:
        event, runtime = await _prepare_native(plugin, group_id="native-fail")
        network = []

        async def fail_send(chain):
            network.append(chain_plain_text(chain))
            raise RuntimeError("native transport failed")

        event.send = fail_send
        plugin.pacer.shape_and_fragment = lambda *args, **kwargs: ["首段", "尾段"]
        event.set_result(build_plain_chain("host result"))
        await plugin.on_decorating_result(event)
        with pytest.raises(RuntimeError, match="native transport failed"):
            await event.send(event.get_result())
        await plugin.after_message_sent(event)

        assert network == ["首段"]
        assert _bot_texts(runtime) == []
        assert runtime.last_bot_node is None
        assert not runtime.followup_queue
        assert not plugin.arbiter.has_bot_spoken(event.unified_msg_origin)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_native_guard_install_failure_fails_open_without_context_or_state(monkeypatch):
    plugin = _plugin({"base_thinking_delay": 0.0})
    try:
        event, runtime = await _prepare_native(plugin, group_id="native-install-fail")
        network = []

        async def send(chain):
            network.append(chain_plain_text(chain))
            return "head-id"

        event.send = send
        plugin.pacer.shape_and_fragment = lambda *args, **kwargs: ["首段", "尾段"]
        event.set_result(build_plain_chain("host result"))
        monkeypatch.setattr(NativeDeliveryGuard, "install", lambda self: False)

        context_key = (event.unified_msg_origin, id(event))
        await plugin.on_decorating_result(event)

        assert context_key not in plugin._native_context_by_event
        assert event.send is send
        await event.send(event.get_result())
        await plugin.after_message_sent(event)

        assert network == ["首段"]
        assert _bot_texts(runtime) == []
        assert runtime.last_bot_node is None
        assert not runtime.followup_queue
        assert not plugin.arbiter.has_bot_spoken(event.unified_msg_origin)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_native_context_eviction_restores_evicted_event_and_keeps_other_guard():
    plugin = _plugin({"base_thinking_delay": 0.0})
    try:
        first, runtime = await _prepare_native(plugin, group_id="native-evict")
        async def first_send(_chain):
            return "first-id"

        first.send = first_send
        first_host_send = first.send
        first.set_result(build_plain_chain("first host result"))
        await plugin.on_decorating_result(first)
        first_key = (first.unified_msg_origin, id(first))
        assert first_key in plugin._native_context_by_event
        assert first.send is not first_host_send

        second = NativeEvent(
            "小助手直接回复",
            sender_id="bob",
            group_id="native-evict",
            message_id="trigger-native-evict-second",
            is_at_or_wake_command=True,
        )
        second.unified_msg_origin = first.unified_msg_origin
        await plugin.on_group_message(second)
        async def second_send(_chain):
            return "second-id"

        second.send = second_send
        second_host_send = second.send
        second.set_result(build_plain_chain("second host result"))
        await plugin.on_decorating_result(second)
        second_key = (second.unified_msg_origin, id(second))
        assert second_key in plugin._native_context_by_event
        assert second.send is not second_host_send

        for index in range(4096 - len(plugin._native_context_by_event)):
            plugin._native_context_by_event[("native-evict-filler", index)] = _NativeEventContext()

        third = NativeEvent(
            "小助手直接回复",
            sender_id="carol",
            group_id="native-evict",
            message_id="trigger-native-evict-third",
            is_at_or_wake_command=True,
        )
        third.unified_msg_origin = first.unified_msg_origin
        third.set_result(build_plain_chain("third host result"))
        await plugin.on_decorating_result(third)

        assert first_key not in plugin._native_context_by_event
        assert first.send is first_host_send
        assert second_key in plugin._native_context_by_event
        assert second.send is not second_host_send
        assert plugin._metrics["native_context_evicted"] >= 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_native_stop_after_decorate_blocks_host_network_before_send():
    plugin = _plugin({"base_thinking_delay": 0.0})
    try:
        event, runtime = await _prepare_native(plugin, group_id="native-before")
        network = []

        async def send(chain):
            network.append(chain_plain_text(chain))
            return "head-id"

        event.send = send
        plugin.pacer.shape_and_fragment = lambda *args, **kwargs: ["首段", "尾段"]
        event.set_result(build_plain_chain("host result"))
        await plugin.on_decorating_result(event)
        await plugin.cmd_dynamics_stop(_stop_event(event, "alice", "stop-native-before"))
        await event.send(event.get_result())
        await plugin.after_message_sent(event)

        assert network == []
        assert _bot_texts(runtime) == []
        assert not runtime.followup_queue
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_member_stop_ack_sends_without_recording_bot_state_through_hooks():
    plugin = _plugin({"base_thinking_delay": 0.0})
    try:
        source = MockEvent(
            "普通消息",
            sender_id="alice",
            group_id="native-command-ack",
            message_id="source-command-ack",
        )
        await plugin.on_group_message(source)
        runtime = plugin._sessions[source.unified_msg_origin]
        runtime.dag.add_message(
            "history-command-ack",
            "alice",
            "已有历史",
            timestamp=plugin.time_service.time(),
        )
        before_nodes = list(runtime.dag.nodes)
        before_spoke = plugin.arbiter.has_bot_spoken(source.unified_msg_origin)

        stop = _stop_event(source, "alice", "stop-command-ack")
        stop.set_result(build_plain_chain("command result"))
        await plugin.on_decorating_result(stop)
        await plugin.after_message_sent(stop)
        await plugin.cmd_dynamics_stop(stop)
        await plugin.on_decorating_result(stop)
        await plugin.after_message_sent(stop)

        assert stop.replies_sent == ["已停止你尚未发送的回复内容。"]
        assert list(runtime.dag.nodes) == before_nodes
        assert plugin.arbiter.has_bot_spoken(source.unified_msg_origin) == before_spoke
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_native_stop_after_send_started_keeps_successful_head_but_drops_tail():
    plugin = _plugin({"base_thinking_delay": 0.0})
    started = asyncio.Event()
    release = asyncio.Event()
    network = []

    async def send(chain):
        network.append(chain_plain_text(chain))
        started.set()
        await release.wait()
        return "head-id"

    try:
        event, runtime = await _prepare_native(plugin, group_id="native-started")
        event.send = send
        plugin.pacer.shape_and_fragment = lambda *args, **kwargs: ["首段", "尾段"]
        event.set_result(build_plain_chain("host result"))
        await plugin.on_decorating_result(event)
        host_send = asyncio.create_task(event.send(event.get_result()))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await plugin.cmd_dynamics_stop(_stop_event(event, "alice", "stop-native-started"))
        release.set()
        await host_send
        await plugin.after_message_sent(event)

        assert network == ["首段"]
        assert _bot_texts(runtime) == ["首段"]
        assert runtime.last_bot_node is not None
        assert not runtime.followup_queue
    finally:
        release.set()
        await plugin.terminate()
