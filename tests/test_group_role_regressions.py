from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.persona_engine import snapshot_turn
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult, parse_group_event
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.tests.test_persona_model import BridgeDouble
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key


def test_persona_turn_context_marks_plain_text_nickname_as_explicit():
    plugin = _plugin({"bot_names": ["团子"]})
    session_key = _session_key("nickname-explicit")
    runtime = plugin._get_or_create_runtime(
        session_key,
        group_id="nickname-explicit",
        umo=session_key,
        bot_id="bot_42",
    )
    parsed = SimpleNamespace(
        message_id="user-1",
        sender_id="user-1",
        text="团子，帮我看一下这个问题",
        reply_to_id=None,
        media_component_types=(),
        is_at_or_wake=False,
        self_id="bot_42",
        mentions=[],
    )
    node = runtime.dag.add_message("user-1", "user-1", parsed.text, timestamp=1.0)
    result = SimpleNamespace(
        user_id="user-1",
        consolidated_text=parsed.text,
        raw_events=[parsed],
        last_event=parsed,
        start_time=1.0,
        metadata={},
    )

    explicit = plugin._looks_like_strong_address(parsed, runtime)
    turn = snapshot_turn(runtime, result, [node.msg_id], [parsed], explicit, {}, shadow=False)

    assert explicit is True
    assert turn.context.explicit is True


@pytest.mark.asyncio
async def test_nickname_plain_group_message_falls_back_to_persona_reply_with_dag_identity(monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_chat_dynamics.core.agent_bridge.AstrBotAgentBridge.check",
        lambda _self: True,
    )
    plugin = _plugin(
        {
            "bot_names": ["团子"],
            "decision_mode": "persona_model",
            "base_thinking_delay": 0,
        }
    )
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge

    async def invalid_decision(**_kwargs):
        return SimpleNamespace(completion_text="not json")

    plugin.context.llm_generate = invalid_decision
    plugin.pacer.persona_fragments = lambda text: [text]
    plugin.pacer.inter_burst_interval = 0
    plugin.time_service.sleep = lambda _delay: asyncio.sleep(0)
    sends = []

    async def send(_runtime, _event, text, *, reply_to_id=None):
        sends.append((text, reply_to_id))
        return SendResult(True, "bot-nickname-reply")

    plugin._send_owned = send
    event = MockEvent(
        "团子，帮我看一下这个问题",
        group_id="nickname-persona-fallback",
        message_id="nickname-incoming",
        is_at_or_wake_command=False,
    )

    await plugin.on_group_message(event)
    await plugin.debounce.flush(session_id=event.unified_msg_origin)
    await asyncio.sleep(0)
    runtime = plugin._sessions[event.unified_msg_origin]
    if runtime.generation_task:
        await asyncio.wait_for(runtime.generation_task, 2)

    assert sends == [(bridge.response, "nickname-incoming")]
    incoming = runtime.dag.get_node("nickname-incoming")
    assert incoming is not None
    bot_node = runtime.last_bot_node
    assert bot_node is not None
    assert bot_node.msg_id == "bot-nickname-reply"
    assert bot_node.reply_to_id == incoming.msg_id
    assert bot_node.parent_ids == {incoming.msg_id}
    await plugin.terminate()


@pytest.mark.asyncio
async def test_same_virtual_time_no_id_flushes_get_distinct_turn_ids():
    clock = VirtualClock(initial_time=1000.0)
    plugin = _plugin({"vibe_llm_enabled": False}, clock=clock)
    session_key = _session_key("turn-sequence")
    runtime = plugin._get_or_create_runtime(
        session_key,
        group_id="turn-sequence",
        umo=session_key,
        bot_id="bot_42",
    )

    for text in ("相同内容", "相同内容"):
        event = MockEvent(text, group_id="turn-sequence", message_id="")
        await plugin._flush_single_event(
            parse_group_event(event),
            event,
            runtime,
            native_pipeline=True,
            epoch=runtime.epoch,
        )

    nodes = runtime.dag.get_recent_nodes(limit=4)
    assert len(nodes) == 2
    assert [node.metadata["turn_id"] for node in nodes] == ["turn_1_user_1", "turn_2_user_1"]
    assert len({node.msg_id for node in nodes}) == 2
    assert all(node.metadata["platform_message_id"] is False for node in nodes)
    await plugin.terminate()


@pytest.mark.asyncio
async def test_flush_after_member_discard_stays_current():
    plugin = _plugin({"vibe_llm_enabled": False})
    session_key = _session_key("fast-stop")
    runtime = plugin._get_or_create_runtime(
        session_key,
        group_id="fast-stop",
        umo=session_key,
        bot_id="bot_42",
    )
    event = MockEvent("怎么写快速排序算法", group_id="fast-stop", sender_id="alice", message_id="q1")
    parsed = parse_group_event(event)
    await plugin.debounce.discard(session_key, user_id=parsed.sender_id)
    await plugin._flush_single_event(
        parsed,
        event,
        runtime,
        native_pipeline=True,
        epoch=runtime.epoch,
    )
    nodes = runtime.dag.get_recent_nodes(limit=4)
    assert nodes
    assert nodes[-1].text == "怎么写快速排序算法"
    await plugin.terminate()


@pytest.mark.asyncio
async def test_missing_message_id_keeps_distinct_same_text_events(monkeypatch):
    plugin = _plugin()
    ingested = []

    async def ingest(**kwargs):
        ingested.append(kwargs)

    monkeypatch.setattr(plugin.debounce, "ingest", ingest)
    first = MockEvent("相同内容", message_id="")
    second = MockEvent("相同内容", message_id="")

    await plugin.on_group_message(first)
    await plugin.on_group_message(second)

    assert [item["event"] for item in ingested] == [first, second]
    runtime = plugin._sessions[_session_key("group_100")]
    assert not runtime.fallback_fingerprint_set
    assert plugin._metrics["duplicate_ignored"] == 0


async def _dispatch_legacy_fragments(plugin, returned_ids, source_message_id="incoming-1"):
    session_key = _session_key("legacy-fragments")
    runtime = plugin._get_or_create_runtime(
        session_key,
        group_id="legacy-fragments",
        umo=session_key,
        bot_id="bot_42",
    )
    trigger_id = source_message_id or "turn_1000_user_1_0"
    trigger = runtime.dag.add_message(trigger_id, "user-1", "请求", timestamp=1.0)
    event = MockEvent("请求", group_id="legacy-fragments", message_id=source_message_id)
    captured = []

    async def generate(*_args, **_kwargs):
        return "ignored"

    async def send(_runtime, _event, fragment, *, reply_to_id=None):
        index = len(captured)
        captured.append((fragment, reply_to_id))
        return SendResult(True, returned_ids[index])

    plugin._run_native_reply = generate
    plugin._send_owned = send
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["first", "second", "third"]
    plugin.pacer.calculate_typing_delay = lambda *_args, **_kwargs: 0.0
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 0.0
    plugin.time_service.sleep = lambda _seconds: asyncio.sleep(0)

    await plugin._dispatch_bot_response(runtime, trigger, GroupChatMode.CHILL_FADE, event, revision=runtime.revision)
    return runtime, captured


@pytest.mark.asyncio
async def test_legacy_fragments_reply_to_previous_real_platform_ids(monkeypatch):
    runtime, captured = await _dispatch_legacy_fragments(_plugin(), ["out-1", "out-2", "out-3"])

    assert [reply_to for _fragment, reply_to in captured] == ["incoming-1", "out-1", "out-2"]
    bot_nodes = [node for node in runtime.dag.nodes.values() if node.user_id == "bot_42"]
    assert [node.msg_id for node in bot_nodes] == ["out-1", "out-2", "out-3"]
    assert [node.reply_to_id for node in bot_nodes] == ["incoming-1", "out-1", "out-2"]


@pytest.mark.asyncio
async def test_legacy_fallback_ids_link_dag_but_never_platform_replies(monkeypatch):
    runtime, captured = await _dispatch_legacy_fragments(_plugin(), [None, None, None])

    assert [reply_to for _fragment, reply_to in captured] == ["incoming-1", None, None]
    bot_nodes = [node for node in runtime.dag.nodes.values() if node.user_id == "bot_42"]
    assert all(node.msg_id.startswith("bot_") for node in bot_nodes)
    assert [node.reply_to_id for node in bot_nodes] == [
        "incoming-1",
        bot_nodes[0].msg_id,
        bot_nodes[1].msg_id,
    ]


@pytest.mark.asyncio
async def test_legacy_without_source_platform_id_has_no_fake_initial_reply(monkeypatch):
    runtime, captured = await _dispatch_legacy_fragments(_plugin(), [None, None, None], source_message_id="")

    assert [reply_to for _fragment, reply_to in captured] == [None, None, None]
    bot_nodes = [node for node in runtime.dag.nodes.values() if node.user_id == "bot_42"]
    assert bot_nodes[0].reply_to_id == "turn_1000_user_1_0"
    assert bot_nodes[1].reply_to_id == bot_nodes[0].msg_id


class _NativeResult:
    def __init__(self, text: str, message_id: str | None = None):
        self.text = text
        self.message_id = message_id

    def get_plain_text(self) -> str:
        return self.text


@pytest.mark.asyncio
async def test_native_decorated_first_preserves_explicit_host_platform_id():
    plugin = _plugin()
    key = _session_key("native-decorated-id")
    runtime = plugin._get_or_create_runtime(
        key,
        group_id="native-decorated-id",
        umo=key,
        bot_id="bot_42",
    )
    event = MockEvent("请求", group_id="native-decorated-id", message_id="incoming-3")
    event.set_result(_NativeResult("native response", "bot_123_456"))
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["first"]

    async def host_send(_chain):
        event.replies_sent.append("first")
        return "bot_123_456"

    event.send = host_send

    await plugin.on_decorating_result(event)
    assert event.get_result() == "first"
    await event.send(event.get_result())
    await plugin.after_message_sent(event)

    node = runtime.last_bot_node
    assert node is not None
    assert node.msg_id == "bot_123_456"
    assert node.metadata["platform_message_id"] is True
    await plugin.terminate()


async def _run_native_tail(plugin, first_message_id, returned_ids):
    session_key = _session_key("native-fragments")
    runtime = plugin._get_or_create_runtime(
        session_key,
        group_id="native-fragments",
        umo=session_key,
        bot_id="bot_42",
    )
    trigger = runtime.dag.add_message("incoming-2", "user-1", "请求", timestamp=1.0)
    event = MockEvent("请求", group_id="native-fragments", message_id="incoming-2")
    event.set_result(_NativeResult("native response"))
    runtime.native_trigger_node = trigger
    runtime.native_vibe_mode = GroupChatMode.CHILL_FADE
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["first", "second", "third"]

    async def host_send(_chain):
        return first_message_id

    event.send = host_send
    captured = []

    async def send(_runtime, _event, fragment, *, reply_to_id=None):
        index = len(captured)
        captured.append((fragment, reply_to_id))
        return SendResult(True, returned_ids[index])

    plugin._send_owned = send
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 0.0
    plugin.time_service.sleep = lambda _seconds: asyncio.sleep(0)

    await plugin.on_decorating_result(event)
    await event.send(event.get_result())
    await plugin.after_message_sent(event)
    return runtime, captured


@pytest.mark.asyncio
async def test_native_tail_uses_real_id_from_host_first_send(monkeypatch):
    runtime, captured = await _run_native_tail(_plugin(), "native-first", ["native-second", "native-third"])

    assert [reply_to for _fragment, reply_to in captured] == ["native-first", "native-second"]
    bot_nodes = [node for node in runtime.dag.nodes.values() if node.user_id == "bot_42"]
    assert [node.msg_id for node in bot_nodes] == ["native-first", "native-second", "native-third"]
    assert [node.reply_to_id for node in bot_nodes] == ["incoming-2", "native-first", "native-second"]


@pytest.mark.asyncio
async def test_native_tail_without_platform_ids_has_no_fake_reply_target(monkeypatch):
    runtime, captured = await _run_native_tail(_plugin(), None, [None, None])

    assert [reply_to for _fragment, reply_to in captured] == [None, None]
    bot_nodes = [node for node in runtime.dag.nodes.values() if node.user_id == "bot_42"]
    assert all(node.msg_id.startswith("bot_") for node in bot_nodes)
    assert [node.reply_to_id for node in bot_nodes] == [
        "incoming-2",
        bot_nodes[0].msg_id,
        bot_nodes[1].msg_id,
    ]
