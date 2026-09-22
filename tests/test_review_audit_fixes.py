"""Review revisions must describe stable evidence, including absent snapshots."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import topic_annotations
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
@pytest.mark.parametrize("bound", ["MAX_DRAFT_CONTEXTS", "MAX_DRAFT_CONTEXT_TOTAL_BYTES"])
async def test_freeing_snapshot_capacity_keeps_published_revision(monkeypatch, bound):
    store, _ = review_store()
    monkeypatch.setattr(topic_annotations, bound, 0)
    await store.save_drafts("s", {"drafts": {"first": {"expected_reply": True}}})
    old = (await store.read_drafts("s"))["drafts"]["first"]
    assert "context" not in old
    revision = store.revision(old)
    monkeypatch.setattr(topic_annotations, bound, 10000)
    await store.save_drafts("s", {"drafts": {"second": {}}}, merge=True)
    current = (await store.read_drafts("s"))["drafts"]
    assert current["first"] == old
    assert "context" in current["second"]
    result = await store.save({"session_key": "s", "msg_id": "first",
                              "expected_topic": "CORRECT", "error_type": "correct"},
                             draft_revision=revision)
    assert result["saved"]


@pytest.mark.asyncio
async def test_removing_snapshot_does_not_backfill_existing_draft(monkeypatch):
    store, _ = review_store()
    monkeypatch.setattr(topic_annotations, "MAX_DRAFT_CONTEXTS", 1)
    await store.save_drafts("s", {"drafts": {"first": {}, "second": {}}})
    old = (await store.read_drafts("s"))["drafts"]["second"]
    assert "context" not in old
    await store.remove_drafts("s", ["first"])
    await store.save_drafts("s", {"drafts": {"third": {}}}, merge=True)
    current = (await store.read_drafts("s"))["drafts"]
    assert current["second"] == old
    assert "context" in current["third"]


@pytest.mark.asyncio
async def test_legacy_missing_snapshot_stays_frozen_but_regeneration_has_new_revision():
    store, kv = review_store()
    old = {"expected_reply": True}
    kv[store.draft_key("s")] = {"drafts": {"first": deepcopy(old)}}
    revision = store.revision(old)
    await store.save_drafts("s", {"drafts": {}}, merge=True)
    assert (await store.read_drafts("s"))["drafts"]["first"] == old
    await store.save_drafts("s", {"drafts": {"first": deepcopy(old)}}, merge=True)
    regenerated = (await store.read_drafts("s"))["drafts"]["first"]
    assert "context" in regenerated
    assert store.revision(regenerated) != revision
    with pytest.raises(ValueError):
        await store.save({"session_key": "s", "msg_id": "first",
                          "expected_topic": "CORRECT", "error_type": "correct"},
                         draft_revision=revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], "text", 3])
@pytest.mark.parametrize("request_id", [None, "request"])
async def test_non_object_annotation_body_is_rejected_before_idempotency(body, request_id):
    store, kv = review_store()
    with pytest.raises(ValueError, match="invalid annotation fields"):
        await store.save(body, request_id=request_id)
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
