from copy import deepcopy

import pytest

from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.tests.test_topic_annotations import fixture_plugin, label


@pytest.mark.asyncio
async def test_v2_snapshot_and_independent_metrics():
    plugin, kv = fixture_plugin()
    node = plugin.dags["a"].nodes["m"]
    node.metadata["routing"].update(addressee_ids=["bot"], bot_is_addressee=True)
    node.metadata["decision_trace"] = {"participation": {"should_reply": True}, "text": "secret"}
    before = deepcopy(node.metadata)
    store = TopicAnnotations(plugin)
    body = label(recipient_correct=False, bot_targeted=False, recipient_ids=["alice"],
                 subject_ids=["bot"], expected_reply=False, recipient_error_type="false_bot")
    await store.save(body)
    assert node.metadata == before
    body["recipient_ids"].append("changed")
    node.metadata["routing"]["addressee_ids"].append("changed")
    node.metadata["decision_trace"]["participation"]["should_reply"] = False
    data = await store.read("a")
    row = data["records"][0]
    assert row["annotation_schema_version"] == 2
    assert row["recipient_ids"] == ["alice"]
    assert row["decision_trace"]["recipient"]["ids"] == ["bot"]
    assert row["decision_trace"]["participation"]["should_reply"] is True
    assert data["recipient_metrics"]["incorrect"] == 1
    assert data["metrics"]["error_counts"] == {"topic_merge": 1}
    assert "secret" not in str(kv) and "private body" not in str(kv)
    row["recipient_ids"].append("read mutation")
    assert (await store.read("a"))["records"][0]["recipient_ids"] == ["alice"]


@pytest.mark.asyncio
async def test_old_records_are_not_recipient_samples():
    plugin, kv = fixture_plugin()
    store = TopicAnnotations(plugin)
    kv[store.key("a")] = [{"msg_id": "old", "predicted_topic": "t", "expected_topic": "t", "error_type": "correct"}]
    data = await store.read("a")
    assert data["metrics"]["total"] == 1
    assert data["recipient_metrics"]["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"recipient_correct": 1}, {"bot_targeted": "true"}, {"expected_reply": None},
    {"recipient_ids": "bot"}, {"recipient_ids": ["bot", "bot"]},
    {"subject_ids": [{}]}, {"subject_ids": [""]},
    {"recipient_error_type": "bad"}, {"recipient_error_type": []}, {"text": "private"},
])
async def test_invalid_v2_fields_do_not_write(extra):
    plugin, kv = fixture_plugin()
    with pytest.raises(ValueError):
        await TopicAnnotations(plugin).save(label(**extra))
    assert not kv
