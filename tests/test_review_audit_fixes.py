"""Review revisions must describe stable evidence, including absent snapshots."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks
from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations


def review_store():
    kv = {}

    async def get(key, default):
        return deepcopy(kv.get(key, default))

    async def put(key, value):
        kv[key] = deepcopy(value)

    nodes = {mid: ConversationNode(mid, "user", "message", 10,
                                  metadata={"routing": {"topic_id": "topic"}})
             for mid in ("first", "second", "third")}
    plugin = SimpleNamespace(get_kv_data=get, put_kv_data=put,
                             dags={"s": SimpleNamespace(nodes=nodes)})
    return TopicAnnotations(plugin), kv








@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], "text", 3])
async def test_non_object_annotation_body_is_rejected(body):
    store, kv = review_store()
    with pytest.raises(ValueError, match="invalid annotation fields"):
        await store.save(body)
    assert not kv


@pytest.mark.parametrize("other_topic,expected_events", [("", 0), ("different", 0), ("topic", 1)])
def test_temporal_topic_association_requires_unambiguous_window(other_topic, expected_events):
    store, _ = review_store()
    store.plugin.dags["s"].nodes = {
        "first": ConversationNode("first", "user", "message", 10,
                                  metadata={"routing": {"topic_id": "topic"}}),
        "second": ConversationNode("second", "user", "message", 11,
                                   metadata={"routing": {"topic_id": other_topic}}),
    }
    event = {"session_id": "s", "ts": 12}
    blocks = replay_topic_blocks(store.plugin, [event], "s")
    assert sum(len(block["events"]) for block in blocks) == expected_events
