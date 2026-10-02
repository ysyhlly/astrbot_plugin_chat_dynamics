import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace


def fixture():
    kv = {}
    async def get(key, default):
        return deepcopy(kv.get(key, default))
    async def put(key, value):
        await asyncio.sleep(0)
        kv[key] = deepcopy(value)
    node = ConversationNode("m", "u", "text", 1, metadata={"routing": {"topic_id": "t"}})
    plugin = SimpleNamespace(get_kv_data=get, put_kv_data=put,
                             dags={"s": SimpleNamespace(nodes={"m": node})})
    return TopicAnnotations(plugin), kv


def label(**kwargs):
    return dict(session_key="s", msg_id="m", expected_topic="CORRECT", error_type="correct", **kwargs)


@pytest.mark.asyncio
async def test_projection_failure_recovery_and_repeated_manual_save():
    store, kv = fixture()
    put = store.plugin.put_kv_data
    async def fail_projection(key, value):
        if key == store.key("s"):
            raise OSError("interrupted legacy projection")
        await put(key, value)
    store.plugin.put_kv_data = fail_projection
    with pytest.raises(OSError):
        await store.save(label())
    assert store.state_key("s") in kv
    store.plugin.put_kv_data = put
    restarted = TopicAnnotations(store.plugin)
    assert (await restarted.read("s"))["records"][0]["msg_id"] == "m"
    assert (await restarted.reconcile())["repaired"] == 1
    await restarted.save(label())
    assert len(kv[store.key("s")]) == 1


@pytest.mark.asyncio
async def test_old_revision_concurrent_writer_has_one_winner():
    store, _ = fixture()
    results = await asyncio.gather(*(store.save(label(expected_revision=store.revision(None)))
                                     for i in range(2)), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1


@pytest.mark.asyncio
async def test_annotation_preserves_frozen_decision_after_reroute():
    store, _ = fixture()
    node = store.plugin.dags["s"].nodes["m"]
    frozen = build_routing_trace(routing={"topic_id": "original"})
    node.metadata["decision_trace"] = deepcopy(frozen)
    node.metadata["routing"] = {"topic_id": "later"}
    result = await store.save(label())
    assert result["record"]["predicted_topic"] == "original"
    assert result["record"]["decision_trace"]["routing"] == frozen["routing"]
