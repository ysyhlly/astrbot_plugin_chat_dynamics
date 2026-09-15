import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.annotation_review import AnnotationReview
from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations


async def runtime():
    kv = {}
    async def get(key, default):
        return deepcopy(kv.get(key, default))
    async def put(key, value):
        await asyncio.sleep(0)
        kv[key] = deepcopy(value)
    node = ConversationNode("m", "u", "body", 1, metadata={"routing": {"topic_id": "t"}})
    host = SimpleNamespace(dags={"s": SimpleNamespace(nodes={"m": node})},
                           get_kv_data=get, put_kv_data=put)
    store = host.topic_annotations = TopicAnnotations(host)
    await store.save_drafts("s", {"drafts": {"m": {"expected_reply": True}}})
    body = {"action": "accept", "session_key": "s", "msg_ids": ["m"], "request_id": "r",
            "revisions": {"m": {"annotation_revision": store.revision(None),
                                   "draft_revision": store.revision({"expected_reply": True})}}}
    return host, AnnotationReview(host), body


@pytest.mark.asyncio
async def test_retry_after_removed_draft_returns_original_saved_ids():
    host, review, body = await runtime()
    result = await review.annotation_drafts_apply(body)
    assert result["saved_ids"] == ["m"]
    assert not (await host.topic_annotations.read_drafts("s"))["drafts"]
    host.dags.clear()
    assert await review.annotation_drafts_apply(body) == result
    assert await host.topic_annotations.read_request("s", "r") == result


@pytest.mark.asyncio
async def test_projection_interruption_retry_preserves_later_human_edit():
    host, review, body = await runtime()
    store = host.topic_annotations
    original = host.put_kv_data
    entered, release = asyncio.Event(), asyncio.Event()
    async def interrupted(key, value):
        if key == store.key("s"):
            entered.set()
            await release.wait()
            raise OSError("projection interrupted")
        await original(key, value)
    host.put_kv_data = interrupted
    task = asyncio.create_task(review.annotation_drafts_apply(body))
    await entered.wait()
    release.set()
    with pytest.raises(OSError):
        await task
    host.put_kv_data = original
    await store.save({"session_key": "s", "msg_id": "m", "expected_topic": "CORRECT",
                      "error_type": "correct", "expected_reply": False})
    retried = await review.annotation_drafts_apply(body)
    assert retried["saved_ids"] == ["m"]
    assert (await store.read("s"))["records"][0]["expected_reply"] is False


@pytest.mark.asyncio
async def test_two_reviewers_with_same_baseline_have_one_winner():
    host, review, body = await runtime()
    results = await asyncio.gather(review.annotation_drafts_apply(body),
                                   review.annotation_drafts_apply({**body, "request_id": "second"}))
    assert sum(result["saved"] for result in results) == 1
    assert sum(len(result["failed"]) for result in results) == 1
    assert len((await host.topic_annotations.read("s"))["records"]) == 1


@pytest.mark.asyncio
async def test_retry_cleanup_preserves_newer_draft_and_rejects_changed_input():
    host, review, body = await runtime()
    store = host.topic_annotations
    original_remove = store.remove_drafts
    entered, release = asyncio.Event(), asyncio.Event()
    async def interrupted_remove(*args, **kwargs):
        entered.set()
        await release.wait()
        raise OSError("interrupted before draft cleanup")
    store.remove_drafts = interrupted_remove
    task = asyncio.create_task(review.annotation_drafts_apply(body))
    await entered.wait()
    release.set()
    with pytest.raises(OSError):
        await task
    assert len((await store.read("s"))["records"]) == 1
    # Simulate an independently persisted draft revision after the successful
    # item commit; cleanup must use the receipt's version, not this reread.
    newer = {"expected_reply": False, "confidence": .95}
    raw = await host.get_kv_data(store.draft_key("s"), {})
    raw["drafts"]["m"] = newer
    await host.put_kv_data(store.draft_key("s"), raw)
    store.remove_drafts = original_remove
    with pytest.raises(ValueError, match="different input"):
        await review.annotation_drafts_apply({**body, "expected_topic": "NEW"})
    assert (await store.read_drafts("s"))["drafts"]["m"] == newer
    result = await review.annotation_drafts_apply(body)
    assert result["saved_ids"] == ["m"]
    assert (await store.read_drafts("s"))["drafts"]["m"] == newer
