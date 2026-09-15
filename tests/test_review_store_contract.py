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
async def test_clear_epoch_rejects_late_generation_even_empty():
    store, _ = fixture()
    started, release = asyncio.Event(), asyncio.Event()
    async def generation():
        epoch = (await store.read_drafts("s")).get("draft_epoch", 0)
        started.set()
        await release.wait()
        return await store.save_drafts("s", {"drafts": {"m": {}}}, expected_epoch=epoch)
    task = asyncio.create_task(generation())
    await started.wait()
    await store.clear_drafts("s")
    release.set()
    assert await task is None
    assert (await store.read_drafts("s"))["draft_epoch"] == 1


@pytest.mark.asyncio
async def test_human_and_dismissed_never_overwritten_by_refresh():
    store, _ = fixture()
    await store.save_drafts("s", {"drafts": {"m": {}, "x": {}}})
    await store.remove_drafts("s", ["x"])
    await store.save(label())
    await store.save_drafts("s", {"drafts": {"m": {}, "x": {}}})
    assert not (await store.read_drafts("s"))["drafts"]
    await store.save_drafts("s", {"drafts": {"m": {}, "x": {}}}, regenerate_dismissed=True)
    assert set((await store.read_drafts("s"))["drafts"]) == {"x"}


@pytest.mark.asyncio
async def test_persistent_idempotency_and_projection_failure_recovery():
    store, kv = fixture()
    put = store.plugin.put_kv_data
    async def fail_projection(key, value):
        if key == store.key("s"):
            raise OSError("interrupted legacy projection")
        await put(key, value)
    store.plugin.put_kv_data = fail_projection
    with pytest.raises(OSError):
        await store.save(label(), request_id="r")
    assert store.state_key("s") in kv
    store.plugin.put_kv_data = put
    restarted = TopicAnnotations(store.plugin)
    result = await restarted.save(label(), request_id="r")
    assert result == await restarted.read_request("s", "r")
    assert len(kv[store.key("s")]) == 1
    assert (await restarted.reconcile())["repaired"] == 1


@pytest.mark.asyncio
async def test_old_revision_concurrent_writer_has_one_winner():
    store, _ = fixture()
    results = await asyncio.gather(*(store.save(label(expected_revision=store.revision(None)), request_id=str(i))
                                     for i in range(2)), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1


@pytest.mark.asyncio
async def test_index_more_than_200_and_interrupted_draft_write_discoverable():
    store, _ = fixture()
    for i in range(201):
        await store.save_drafts(str(i), {"drafts": {"m": {}}})
    assert len(await TopicAnnotations(store.plugin).known_sessions()) == 201
    put = store.plugin.put_kv_data
    async def fail(key, value):
        if key == store.draft_key("failed"):
            raise OSError("disk")
        await put(key, value)
    store.plugin.put_kv_data = fail
    with pytest.raises(OSError):
        await store.save_drafts("failed", {"drafts": {"m": {}}})
    assert "failed" in await store.known_sessions()


@pytest.mark.asyncio
async def test_clear_and_remove_request_retries_are_stable():
    store, _ = fixture()
    await store.save_drafts("s", {"drafts": {"m": {}}})
    assert await store.remove_drafts("s", ["m"], request_id="remove") == 1
    assert await store.remove_drafts("s", ["m"], request_id="remove") == 1
    first = await store.clear_drafts("s", request_id="clear")
    assert first == await store.clear_drafts("s", request_id="clear")
    assert first == await store.read_request("s", "clear")


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


@pytest.mark.asyncio
async def test_revision_mismatch_does_not_poison_dismissal_history():
    store, _ = fixture()
    await store.save_drafts("s", {"drafts": {"m": {"confidence": .8}}})
    assert await store.remove_drafts("s", ["m"], revisions={"m": "old"}) == 0
    await store.save_drafts("s", {"drafts": {}}, merge=True)
    assert "m" in (await store.read_drafts("s"))["drafts"]
    assert "m" not in (await store.read_drafts("s"))["dismissed_ids"]


@pytest.mark.asyncio
async def test_archived_requests_still_replay_and_reject_conflicting_input():
    store, kv = fixture()
    for i in range(260):
        await store.record_request("s", str(i), {"value": i}, {"saved": i})
    assert len(kv[store.state_key("s")]["requests"]) <= 257
    assert await store.read_request("s", "0") == {"saved": 0}
    with pytest.raises(ValueError):
        await store.record_request("s", "0", {"value": "changed"}, {})


@pytest.mark.asyncio
async def test_remove_and_clear_reject_same_id_for_different_operation():
    store, _ = fixture()
    await store.clear_drafts("s", request_id="same")
    with pytest.raises(ValueError):
        await store.remove_drafts("s", ["m"], request_id="same")
