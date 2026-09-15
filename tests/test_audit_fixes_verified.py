"""Regression tests for the defects that survived verification.

Each test here pins one *confirmed* finding from the code-quality audit and the
evidence that decided it. Items the audit reported but that do not reproduce are
documented in AUDIT_VERIFICATION.md instead of being encoded as tests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.platform_bridge import build_plain_chain
from astrbot_plugin_chat_dynamics.core.topic_reranker import TopicReranker

from .test_member_stop import _prepare_native
from .test_plugin_lifecycle import MockEvent, _plugin


class _StubAdapter:
    """Minimal LLMAdapter double: one canned completion per call."""

    def __init__(self, output):
        self.output = output
        self.calls = 0

    async def generate(self, **_kwargs):
        self.calls += 1
        return self.output


@pytest.mark.asyncio
async def test_topic_title_accepts_one_presentation_fence():
    """DEF-05: a fenced title must parse; prose carrying JSON must still not."""
    adapter = _StubAdapter("\u0060\u0060\u0060json\n{\"title\":\"项目排期讨论\"}\n\u0060\u0060\u0060")
    reranker = TopicReranker(adapter, enabled=True)
    assert await reranker.title(umo="umo", messages=["甲: 明天排期", "乙: 好"]) == "项目排期讨论"

    prose = _StubAdapter('好的，标题是 {"title":"项目排期讨论"}')
    assert await TopicReranker(prose, enabled=True).title(umo="umo", messages=["x"]) == ""


def test_prune_clears_inferred_parent_pointers():
    """DEF-03: eviction must not leave a child claiming a parent that is gone."""
    dag = ConversationDAG(session_id="s", max_nodes=10, ttl_seconds=0.0)
    dag.add_message("p1", "alice", "问题", timestamp=100.0)
    child = dag.add_message("c1", "bob", "回答", timestamp=101.0)
    child.metadata["routing"] = {"parent_message_id": "", "parent_confidence": 0.0}
    assert dag.link_inferred_reply("c1", "p1", confidence=0.9, reason="inferred_reply")
    assert child.metadata["inferred_parent_id"] == "p1"
    assert child.metadata["routing"]["parent_message_id"] == "p1"

    dag.prune(max_nodes=1, ttl_seconds=0.0, current_time=102.0)

    assert dag.get_node("p1") is None
    assert dag.get_node("c1") is child
    assert "inferred_parent_id" not in child.metadata
    assert "inferred_confidence" not in child.metadata
    assert child.metadata["edge_metadata"] == {}
    assert child.metadata["routing"]["parent_message_id"] == ""
    assert child.metadata["routing"]["parent_confidence"] == 0.0


@pytest.mark.asyncio
async def test_cancelled_persona_worker_releases_queued_admission():
    """DEF-10: a torn-down worker must not strand the permits of its queue."""
    plugin = _plugin({"decision_mode": "persona_model", "base_thinking_delay": 0.0})
    try:
        engine = plugin.persona_engine
        engine.valid = lambda runtime, item: True
        gate = asyncio.Event()

        async def block(runtime, item):
            await gate.wait()

        engine.process = block
        runtime = plugin._registry.get_or_create("sess-admission", group_id="g", umo="u")
        capacity = runtime.model_admission._value
        for index in range(3):
            await runtime.model_admission.acquire()
            runtime.model_queue.append(SimpleNamespace(index=index))
        assert runtime.model_admission._value == capacity - 3

        worker = asyncio.create_task(engine.run(runtime))
        runtime.generation_task = worker
        for _ in range(20):
            await asyncio.sleep(0)
            if runtime.active_model_turn is not None and len(runtime.model_queue) == 2:
                break
        assert len(runtime.model_queue) == 2

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

        assert not runtime.model_queue
        assert runtime.model_admission._value == capacity
        assert runtime.generation_task is None
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_invalidated_native_tail_still_records_delivered_id():
    """AUDIT-MAIN-02: a delivered tail is real even when its batch is invalidated."""
    plugin = _plugin({"base_thinking_delay": 0.0})
    try:
        event, runtime = await _prepare_native(plugin, group_id="tail-sent-id")
        sent_ids = []

        async def send(chain):
            sent_ids.append(f"id-{len(sent_ids) + 1}")
            return sent_ids[-1]

        event.send = send
        plugin.pacer.shape_and_fragment = lambda *args, **kwargs: ["首段", "尾段"]
        event.set_result(build_plain_chain("host result"))
        await plugin.on_decorating_result(event)
        await event.send(event.get_result())

        original = plugin._send_owned

        async def send_then_invalidate(rt, ev, text, **kwargs):
            result = await original(rt, ev, text, **kwargs)
            if result.success:
                rt.invalidate_followups_for_user("alice")
            return result

        plugin._send_owned = send_then_invalidate
        await plugin.after_message_sent(event)

        assert plugin._metrics.get("followup_dropped", 0) >= 1
        assert len(sent_ids) == 2
        assert sent_ids[-1] in runtime.sent_id_set
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_terminate_releases_sessions_when_final_save_is_cancelled():
    """AUDIT-MAIN-12: a cancelled shutdown must still hand back session state."""
    plugin = _plugin({"base_thinking_delay": 0.0})
    event = MockEvent("普通消息", sender_id="alice", group_id="term-cancel", message_id="term-1")
    await plugin.on_group_message(event)
    assert plugin._sessions

    async def cancelled_save():
        raise asyncio.CancelledError()

    plugin._save_panel_runtime = cancelled_save
    with pytest.raises(asyncio.CancelledError):
        await plugin.terminate()

    assert not plugin._sessions
    assert not plugin._registry.known_ids()
    assert not plugin._in_flight


@pytest.mark.asyncio
async def test_dynamics_cool_reports_capacity_instead_of_raising():
    """AUDIT-MAIN-08: an unschedulable runtime is reported, never raised."""
    plugin = _plugin({"base_thinking_delay": 0.0})
    try:
        plugin._registry.max_sessions = 1
        busy = MockEvent("消息", sender_id="alice", group_id="busy-room", message_id="busy-1")
        await plugin.on_group_message(busy)
        plugin._in_flight.add(busy.unified_msg_origin)

        cold = MockEvent("/dynamics cool 5", sender_id="admin", group_id="cold-room", message_id="cold-1")
        await plugin.cmd_dynamics(cold, action="cool", param="5")

        assert cold.replies_sent == ["会话数量已达上限，暂时无法为本群开启冷却。"]
        assert cold.unified_msg_origin not in plugin._sessions
    finally:
        plugin._in_flight.clear()
        await plugin.terminate()
