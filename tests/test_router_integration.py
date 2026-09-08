"""Router integration contracts across async enrichment and persona admission."""
import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core.persona_engine import turn_is_addressed, turn_is_continuation
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_plugin_lifecycle import _plugin


def setup_turn(plugin):
    runtime = plugin._get_or_create_runtime("test:GroupMessage:room", group_id="room", umo="test:GroupMessage:room", bot_id="bot")
    node = runtime.dag.add_message("m1", "alice", "follow up", timestamp=10)
    turn = TurnContext(runtime.session_key, "alice", node.text, (MessageSnapshot("m1", "alice", node.text),), (), runtime.epoch, 0, 10, False)
    return runtime, node, turn


def test_routed_addressee_controls_persona_admission():
    plugin = _plugin()
    runtime, node, turn = setup_turn(plugin)
    node.metadata["routing"] = {"addressee_ids": ["bot"], "addressee_confidence": .8, "bot_addressee_confidence": .8}
    assert turn_is_addressed(runtime, turn, 10)
    node.metadata["routing"].update(addressee_ids=["bob"])
    assert not turn_is_addressed(runtime, replace(turn, explicit=True), 10)


def test_continuation_requires_same_topic_and_no_other_recipient():
    plugin = _plugin()
    runtime, node, turn = setup_turn(plugin)
    runtime.last_interlocutor, runtime.last_model_send = "alice", 9
    runtime.last_bot_node = runtime.dag.add_message("b1", "bot", "answer", timestamp=9)
    runtime.last_bot_node.metadata["routing"] = {"topic_id": "one"}
    node.metadata["routing"] = {"topic_id": "two"}
    assert not turn_is_continuation(runtime, turn, 10)
    node.metadata["routing"]["topic_id"] = "one"
    assert turn_is_continuation(runtime, turn, 10)


@pytest.mark.asyncio
async def test_embedding_readiness_does_not_wait_for_state_lock():
    plugin = _plugin()
    runtime, node, _ = setup_turn(plugin)
    plugin.embeddings.enabled = True
    async def embed(text):
        return plugin.embeddings.remember(text, [1, 0])
    plugin.embeddings.embed = embed
    async with runtime.state_lock:
        task = plugin._schedule_neural_embed(runtime.session_key, node)
        await asyncio.wait_for(task.routing_ready.wait(), .2)
        assert not task.done()
    await task
    assert "routing" in node.metadata
    await plugin.terminate()


@pytest.mark.asyncio
async def test_reset_during_embedding_does_not_restore_routing():
    plugin = _plugin()
    runtime, node, _ = setup_turn(plugin)
    entered, release = asyncio.Event(), asyncio.Event()
    async def embed(text):
        entered.set()
        await release.wait()
        return [1, 0]
    plugin.embeddings.embed = embed
    plugin.thread_router.route = Mock()
    task = asyncio.create_task(plugin._warm_neural_embedding(runtime.session_key, node.msg_id, node.text, runtime.epoch))
    await entered.wait()
    runtime.epoch += 1
    release.set()
    await task
    plugin.thread_router.route.assert_not_called()
    assert "routing" not in node.metadata
    await plugin.terminate()
