"""Regression coverage for persona DAG identity and platform reply identity."""

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.persona_engine import _platform_reply_id, snapshot_turn
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult
from .test_persona_model import BridgeDouble
from .test_plugin_lifecycle import MockEvent, _plugin


def _parsed(message_id: str = "", text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        sender_id="user_1",
        text=text,
        reply_to_id="",
        media_component_types=[],
    )


def _snapshot_fixture(count: int):
    dag = ConversationDAG(session_id="mock:room")
    turn_id = "turn_1000_user_1"
    nodes = [
        dag.add_message(
            f"{turn_id}_{index}",
            "user_1",
            f"part-{index}",
            timestamp=1.0 + index,
            metadata={"turn_id": turn_id, "turn_index": index},
        )
        for index in range(count)
    ]
    events = [_parsed(text=f"part-{index}") for index in range(count)]
    result = SimpleNamespace(
        user_id="user_1",
        consolidated_text="\n".join(f"part-{index}" for index in range(count)),
        raw_events=events,
        messages=[SimpleNamespace(text=f"part-{index}", timestamp=1.0 + index) for index in range(count)],
        start_time=1.0,
        metadata={},
        last_event=events[-1],
    )
    runtime = SimpleNamespace(
        dag=dag,
        session_key="mock:room",
        bot_id="bot_42",
        epoch=0,
        user_revisions={},
    )
    return runtime, result, nodes, events


def test_snapshot_no_platform_id_uses_existing_single_dag_node():
    runtime, result, nodes, events = _snapshot_fixture(1)

    item = snapshot_turn(runtime, result, [nodes[0].msg_id], events, True, {}, False)

    assert [message.message_id for message in item.context.messages] == [nodes[0].msg_id]
    assert all(":" not in message.message_id for message in item.context.messages)
    assert item.context.allowed_ids <= set(runtime.dag.nodes)


def test_snapshot_no_platform_id_uses_each_existing_multi_message_node():
    runtime, result, nodes, events = _snapshot_fixture(2)

    item = snapshot_turn(runtime, result, [node.msg_id for node in nodes], events, True, {}, False)

    assert [message.message_id for message in item.context.messages] == [node.msg_id for node in nodes]
    assert item.context.allowed_ids <= set(runtime.dag.nodes)


def test_snapshot_mapping_failure_does_not_drop_fragments_or_build_partial_context():
    runtime, result, nodes, events = _snapshot_fixture(2)

    assert snapshot_turn(runtime, result, [nodes[0].msg_id], events, True, {}, False) is None
    assert runtime.model_diagnostic == {
        "reason_code": "turn_identity_invalid",
        "detail": "mapping_length_mismatch",
        "canonical_count": 1,
        "parsed_count": 2,
        "raw_count": 2,
    }

    assert snapshot_turn(
        runtime,
        result,
        [nodes[0].msg_id, "missing-node"],
        events,
        True,
        {},
        False,
    ) is None
    assert runtime.model_diagnostic["detail"] == "node_missing"


def test_platform_reply_requires_explicit_true_marker_for_history():
    runtime, result, nodes, events = _snapshot_fixture(1)
    item = snapshot_turn(runtime, result, [nodes[0].msg_id], events, True, {}, False)
    assert item is not None

    historical = runtime.dag.add_message("bot_123_456", "bot_42", "历史回答", timestamp=0.5)
    assert _platform_reply_id(runtime, item, historical.msg_id) is None
    historical.metadata["platform_message_id"] = False
    assert _platform_reply_id(runtime, item, historical.msg_id) is None
    historical.metadata["platform_message_id"] = True
    assert _platform_reply_id(runtime, item, historical.msg_id) == historical.msg_id


@pytest.fixture
def persona_plugin(monkeypatch):
    monkeypatch.setattr("astrbot_plugin_chat_dynamics.core.agent_bridge.AstrBotAgentBridge.check", lambda self: True)
    plugin = _plugin({"decision_mode": "persona_model", "base_thinking_delay": 0})
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge

    async def decide(**kwargs):
        payload = kwargs["prompt"]
        import json

        ids = json.loads(payload)["conversation"]["messages"]
        return SimpleNamespace(completion_text=json.dumps({
            "action": "reply",
            "state": "focused",
            "target_message_ids": [ids[0]["message_id"]],
            "response_goal": "回应当前请求",
            "length": "brief",
            "reason_code": "relevant_request",
        }, ensure_ascii=False))

    plugin.context.llm_generate = decide
    return plugin


async def _drain(plugin):
    await asyncio.sleep(0)
    tasks = [runtime.generation_task for runtime in plugin._sessions.values() if runtime.generation_task]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 2)


async def _run_persona(plugin, event, fragments, send_results):
    plugin.pacer.persona_fragments = lambda _text: list(fragments)
    plugin.pacer.inter_burst_interval = 0
    plugin.time_service.sleep = lambda _delay: asyncio.sleep(0)
    calls = []

    async def send(_runtime, _event, text, *, reply_to_id=None):
        calls.append((text, reply_to_id))
        return send_results[len(calls) - 1]

    plugin._send_owned = send
    await plugin.on_group_message(event)
    await plugin.debounce.flush(session_id=event.unified_msg_origin)
    await _drain(plugin)
    return calls, plugin._sessions[event.unified_msg_origin]


@pytest.mark.asyncio
async def test_persona_real_message_id_chains_platform_replies(persona_plugin):
    event = MockEvent("请求", message_id="source-real", is_at_or_wake_command=True)
    calls, runtime = await _run_persona(
        persona_plugin,
        event,
        ["first", "second", "third"],
        [SendResult(True, "bot-real-1"), SendResult(True, "bot-real-2"), SendResult(True, "bot-real-3")],
    )

    assert [reply_to_id for _, reply_to_id in calls] == ["source-real", "bot-real-1", "bot-real-2"]
    assert runtime.last_bot_node is not None
    assert runtime.last_bot_node.reply_to_id == "bot-real-2"
    assert [runtime.dag.get_node(message_id).reply_to_id for message_id in ("bot-real-1", "bot-real-2", "bot-real-3")] == [
        "source-real", "bot-real-1", "bot-real-2"
    ]
    await persona_plugin.terminate()


@pytest.mark.asyncio
async def test_persona_fallback_id_never_becomes_platform_reply(persona_plugin):
    event = MockEvent("无平台 ID", message_id="", is_at_or_wake_command=True)
    calls, runtime = await _run_persona(
        persona_plugin,
        event,
        ["first", "second"],
        [SendResult(True), SendResult(True)],
    )

    assert [reply_to_id for _, reply_to_id in calls] == [None, None]
    bot_nodes = [node for node in runtime.dag.nodes.values() if node.user_id == runtime.bot_id]
    assert len(bot_nodes) == 2
    assert bot_nodes[1].reply_to_id == bot_nodes[0].msg_id
    assert bot_nodes[0].reply_to_id in runtime.dag.nodes
    assert bot_nodes[0].metadata["platform_message_id"] is False
    await persona_plugin.terminate()


@pytest.mark.asyncio
async def test_persona_failed_send_does_not_advance_reply_or_dag(persona_plugin):
    event = MockEvent("失败请求", message_id="source-real", is_at_or_wake_command=True)
    calls, runtime = await _run_persona(
        persona_plugin,
        event,
        ["first", "second"],
        [SendResult(False, error="rejected"), SendResult(True, "should-not-send")],
    )

    assert calls == [("first", "source-real")]
    assert runtime.last_bot_node is None
    assert all(node.user_id != runtime.bot_id for node in runtime.dag.nodes.values())
    assert not persona_plugin.persona_engine.bridge.commits
    await persona_plugin.terminate()
