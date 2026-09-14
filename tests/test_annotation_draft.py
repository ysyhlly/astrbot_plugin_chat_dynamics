"""AI 预标注：草稿是什么、不是什么，以及采纳之后记录里留下什么。

草稿永远不能变成标注 —— 学习层的真值之所以是人工，是因为回复决策本身就是模型判断，
同一种判断再当一遍真值就是自己给自己判卷。所以这里钉住三件事：模型看不到本体的判定、
没被问到的 msg_id 不会被存下来、以及只有人按下保存才会有记录（并写明采纳了草稿）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.annotation_draft import (
    build_batch, build_prompt, parse_drafts,
)
from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.tests.test_topic_annotations import fixture_plugin, label

FENCE = chr(96) * 3


def node(msg_id, text, *, user="user", bot=False, mentions=(), reply_to=""):
    metadata = {"is_bot": bot} if bot else {}
    return ConversationNode(msg_id, user, text, 1, metadata=metadata,
                            mentioned_users=list(mentions), reply_to_id=reply_to or None)


def reply_for(msg_id, **overrides):
    row = {"msg_id": msg_id, "expected_reply": True, "bot_targeted": False,
           "confidence": 0.8, "reason": "直接提问"}
    row.update(overrides)
    return row


def reply(*rows):
    return json.dumps({"rows": list(rows)}, ensure_ascii=False)


# ---- 批次 ---------------------------------------------------------------

def test_bot_messages_are_context_and_never_draft_targets():
    nodes = [node("m1", "在吗"), node("m2", "在的", user="bot", bot=True), node("m3", "帮我看看")]

    batch, stats = build_batch(nodes, {}, limit=5)

    assert [item["msg_id"] for item in batch] == ["m1", "m2", "m3"]
    assert batch[1]["from_bot"] is True and batch[1]["draft_this"] is False
    assert [item["msg_id"] for item in batch if item["draft_this"]] == ["m1", "m3"]
    assert stats == {"window": 3, "draftable": 2, "asked": 2}


def test_an_already_labelled_message_stays_as_context_only():
    nodes = [node("m1", "在吗"), node("m2", "帮我看看")]

    batch, stats = build_batch(nodes, {"m1": {"expected_reply": True}}, limit=5)

    assert [item["msg_id"] for item in batch if item["draft_this"]] == ["m2"]
    assert stats["draftable"] == 1


def test_only_the_newest_unlabelled_messages_are_asked_about():
    nodes = [node("m" + str(index), "消息 " + str(index)) for index in range(6)]

    _batch, stats = build_batch(nodes, {}, limit=2)

    assert stats["asked"] == 2


def test_messages_without_text_are_not_drafted():
    nodes = [node("m1", "   "), node("m2", "有内容")]

    _batch, stats = build_batch(nodes, {}, limit=5)

    assert stats["draftable"] == 1


def test_the_prompt_never_carries_what_the_host_decided():
    batch, _stats = build_batch([node("m1", "在吗")], {}, limit=5)

    system, user = build_prompt(batch)

    assert "draft_this" in user and "在吗" in user
    for hidden in ("participation", "should_reply", "outcome", "level"):
        assert hidden not in user
    assert "JSON" in system


# ---- 读回模型输出 --------------------------------------------------------

def test_a_valid_reply_becomes_one_draft_per_asked_message():
    batch, _stats = build_batch([node("m1", "在吗")], {}, limit=5)

    parsed = parse_drafts(reply(reply_for("m1")), batch)

    assert parsed is not None
    draft = parsed["drafts"]["m1"]
    assert draft["expected_reply"] is True and draft["bot_targeted"] is False
    assert draft["confidence"] == 0.8 and draft["reason"] == "直接提问"
    assert parsed["drafted"] == 1


def test_an_id_that_was_not_asked_about_is_reported_rather_than_stored():
    batch, _stats = build_batch([node("m2", "帮我看看")], {}, limit=5)

    parsed = parse_drafts(reply(reply_for("m2"), reply_for("m9")), batch)

    assert list(parsed["drafts"]) == ["m2"]
    assert parsed["invented"] == ["m9"]


def test_a_bot_message_cannot_be_drafted_even_if_the_model_answers_for_it():
    batch, _stats = build_batch([node("m1", "在的", bot=True)], {}, limit=5)

    assert parse_drafts(reply(reply_for("m1")), batch) is None


def test_a_non_boolean_answer_is_undecided_rather_than_false():
    batch, _stats = build_batch([node("m1", "在吗")], {}, limit=5)

    parsed = parse_drafts(reply({"msg_id": "m1", "expected_reply": "yes",
                                 "bot_targeted": None, "confidence": 0.5, "reason": ""}), batch)

    assert parsed is None


def test_one_usable_field_is_enough_for_a_draft():
    batch, _stats = build_batch([node("m1", "在吗")], {}, limit=5)

    parsed = parse_drafts(reply({"msg_id": "m1", "expected_reply": True,
                                 "confidence": 2, "reason": "x"}), batch)

    assert parsed["drafts"]["m1"] == {"msg_id": "m1", "expected_reply": True,
                                      "confidence": 1.0, "reason": "x"}


def test_prose_is_refused_and_a_fenced_reply_is_read():
    batch, _stats = build_batch([node("m1", "在吗")], {}, limit=5)

    assert parse_drafts("这条应该回。", batch) is None
    fenced = FENCE + "json\n" + reply(reply_for("m1")) + "\n" + FENCE
    assert parse_drafts(fenced, batch) is not None


# ---- 采纳之后 ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_draft_is_not_a_label():
    plugin, kv = fixture_plugin()
    store = TopicAnnotations(plugin)

    await store.save_drafts("a", {"drafts": {"m": {"expected_reply": True, "confidence": 0.9}}})

    data = await store.read("a")
    assert data["records"] == [], "草稿不能出现在标注里"
    assert data["drafts"]["m"]["expected_reply"] is True
    assert store.key("a") not in kv


@pytest.mark.asyncio
async def test_accepting_the_draft_is_written_into_the_record():
    plugin, kv = fixture_plugin()
    store = TopicAnnotations(plugin)
    await store.save_drafts("a", {"drafts": {"m": {"expected_reply": True, "bot_targeted": False,
                                          "confidence": 0.9, "reason": "直接提问"}}})

    await store.save(label(expected_reply=True, bot_targeted=False))

    row = (await store.read("a"))["records"][0]
    assert row["label_source"] == "human"
    assert row["accepted_from"] == "ai"
    assert row["draft_confidence"] == 0.9
    assert (await store.read("a"))["metrics"]["ai_assisted"] == 1
    assert "采纳了模型草稿" in (await store.read("a"))["metrics"]["sample_note"]


@pytest.mark.asyncio
async def test_changing_one_value_drops_the_ai_marker():
    plugin, _kv = fixture_plugin()
    store = TopicAnnotations(plugin)
    await store.save_drafts("a", {"drafts": {"m": {"expected_reply": True, "bot_targeted": False}}})

    await store.save(label(expected_reply=False, bot_targeted=False))

    row = (await store.read("a"))["records"][0]
    assert "accepted_from" not in row
    assert row["label_source"] == "human"
    assert (await store.read("a"))["metrics"]["ai_assisted"] == 0


@pytest.mark.asyncio
async def test_a_label_with_no_draft_is_plainly_human():
    plugin, _kv = fixture_plugin()
    store = TopicAnnotations(plugin)

    await store.save(label(expected_reply=True))

    row = (await store.read("a"))["records"][0]
    assert row["label_source"] == "human" and "accepted_from" not in row


# ---- 运行期（状态机与错误分流） -----------------------------------------

class _Llm:
    """只回答我们让它回答的东西：模型在这里是可控输入，不是被测对象。"""

    def __init__(self, reply="", error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def configured_provider(self, purpose="reply"):
        return "provider-" + purpose

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.reply


def draft_runtime(*, nodes=(), enabled=True, reply="", error=None, records=(), limit=20):
    from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin

    plugin = ChatDynamicsPlugin.__new__(ChatDynamicsPlugin)
    kv = {}

    async def get(key, default):
        return kv.get(key, default)

    async def put(key, value):
        kv[key] = value

    dag = SimpleNamespace(get_recent_nodes=lambda count: list(nodes))
    store_plugin = SimpleNamespace(get_kv_data=get, put_kv_data=put, dags={"a": dag},
                                  console_show_message_content=False, _shutting_down=False)
    store = TopicAnnotations(store_plugin)
    kv[store.key("a")] = list(records)
    plugin.dags = {"a": dag}
    plugin.topic_annotations = store
    plugin.llm = _Llm(reply, error)
    plugin.annotation_draft_enabled = enabled
    plugin.annotation_draft_limit = limit
    plugin.annotation_draft_timeout = 30.0
    plugin.metrics = []
    plugin._metric = lambda name, amount=1: plugin.metrics.append(name)
    plugin._bounded_text = lambda value, size: str(value)[:size]
    return plugin, kv


@pytest.mark.asyncio
async def test_the_disabled_draft_never_calls_the_model():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")], enabled=False)

    payload = await plugin.annotation_draft_payload("a")

    assert payload["state"] == "disabled"
    assert "annotation_draft_enabled" in payload["reason"]
    assert plugin.llm.calls == []


@pytest.mark.asyncio
async def test_an_unknown_session_is_its_own_answer():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")])

    payload = await plugin.annotation_draft_payload("nope")

    assert payload["state"] == "no_session"
    assert plugin.llm.calls == []


@pytest.mark.asyncio
async def test_a_window_that_is_already_labelled_is_not_drafted_again():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                               records=[{**label(msg_id="m1"), "predicted_topic": "t"}])

    payload = await plugin.annotation_draft_payload("a")

    assert payload["state"] == "nothing_to_draft"
    assert plugin.llm.calls == []


@pytest.mark.asyncio
async def test_a_successful_draft_is_stored_and_reported():
    plugin, kv = draft_runtime(nodes=[node("m1", "在吗"), node("m2", "在的", bot=True)],
                               reply=reply(reply_for("m1")))

    payload = await plugin.annotation_draft_payload("a")

    assert payload["state"] == "fresh"
    assert payload["provider_id"] == "provider-draft"
    assert payload["stats"]["asked"] == 1
    assert list(payload["drafts"]) == ["m1"]
    assert plugin.llm.calls[0]["purpose"] == "draft"
    assert plugin.llm.calls[0]["timeout"] == 30.0
    assert "在吗" in plugin.llm.calls[0]["prompt"]
    assert kv[plugin.topic_annotations.draft_key("a")]["drafts"]["m1"]["expected_reply"] is True
    assert kv[plugin.topic_annotations.key("a")] == [], "草稿不能写进标注键"
    assert plugin.metrics == ["annotation_draft_succeeded"]


@pytest.mark.asyncio
async def test_a_model_that_answers_with_prose_is_a_failure_not_a_draft():
    plugin, kv = draft_runtime(nodes=[node("m1", "在吗")], reply="这条应该回。")

    payload = await plugin.annotation_draft_payload("a")

    assert payload["state"] == "failed"
    assert plugin.metrics == ["annotation_draft_failed"]
    assert plugin.topic_annotations.draft_key("a") not in kv


@pytest.mark.asyncio
async def test_an_unavailable_provider_is_reported_separately():
    from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMUnavailable

    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")], error=LLMUnavailable("no provider"))

    payload = await plugin.annotation_draft_payload("a")

    assert payload["state"] == "unavailable"
    assert "LLMUnavailable" in payload["reason"]
    assert plugin.metrics == ["annotation_draft_unavailable"]

