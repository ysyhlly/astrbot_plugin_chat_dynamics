import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
from astrbot_plugin_chat_dynamics.core import web_api


def fixture_plugin():
    kv = {}
    async def get(key, default):
        return kv.get(key, default)
    async def put(key, value):
        await asyncio.sleep(0)
        kv[key] = value
    node = ConversationNode("m", "user", "private body", 1, metadata={"routing": {"topic_id": "t", "topic_confidence": .6}})
    plugin = SimpleNamespace(get_kv_data=get, put_kv_data=put, dags={"a": SimpleNamespace(nodes={"m": node}), "b": SimpleNamespace(nodes={"m": node})}, console_show_message_content=False, _shutting_down=False)
    return plugin, kv


def label(session="a", **overrides):
    return {"session_key": session, "msg_id": "m", "expected_topic": "NEW", "error_type": "topic_merge", **overrides}


@pytest.mark.asyncio
async def test_persist_isolate_replace_and_metrics():
    plugin, kv = fixture_plugin()
    store = TopicAnnotations(plugin)
    await store.save(label())
    await store.save(label("b", expected_topic="UNKNOWN", error_type="premature_assignment"))
    reloaded = TopicAnnotations(plugin)
    assert (await reloaded.read("a"))["metrics"]["confusion"] == [{"predicted": "t", "expected": "NEW", "count": 1}]
    await reloaded.save(label(expected_topic="CORRECT"))
    assert (await reloaded.read("a"))["metrics"]["error_counts"] == {"correct": 1}
    assert (await reloaded.read("b"))["metrics"]["error_counts"] == {"premature_assignment": 1}
    assert "private body" not in str(kv)
    assert plugin.dags["a"].nodes["m"].metadata["routing"]["topic_id"] == "t"


@pytest.mark.asyncio
async def test_concurrent_annotations_preserved():
    plugin, _ = fixture_plugin()
    plugin.dags["a"].nodes["m2"] = ConversationNode("m2", "u", "second", 2)
    store = TopicAnnotations(plugin)
    await asyncio.gather(store.save(label()), store.save(label(msg_id="m2")))
    assert (await store.read("a"))["metrics"]["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"msg_id": "absent"}, {"expected_topic": "foreign"}, {"error_type": "bad"}, {"expected_topic": []}, {"extra": "x"}])
async def test_invalid_annotation(overrides):
    plugin, kv = fixture_plugin()
    with pytest.raises(ValueError):
        await TopicAnnotations(plugin).save(label(**overrides))
    assert kv == {}


@pytest.mark.asyncio
async def test_api_redacts_previously_stored_text(monkeypatch, offline_web_responses):
    plugin, _ = fixture_plugin()
    plugin.console_show_message_content = True
    api = web_api.ConsoleWebAPI(plugin)
    await api.topic_annotations.save(label())
    plugin.console_show_message_content = False
    monkeypatch.setattr(web_api, "_query_param", lambda name: "a")
    result = await api.annotations_get()
    assert result["ok"] and "text" not in result["data"]["records"][0]
    async def body():
        return label(msg_id="absent")
    monkeypatch.setattr(web_api, "_json_body", body)
    assert (await api.annotations_post())["status_code"] == 400
