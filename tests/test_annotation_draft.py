"""AI 预标注：草稿是什么、不是什么，以及采纳之后记录里留下什么。

草稿永远不能变成标注 —— 学习层的真值之所以是人工，是因为回复决策本身就是模型判断，
同一种判断再当一遍真值就是自己给自己判卷。所以这里钉住三件事：模型看不到本体的判定、
没被问到的 msg_id 不会被存下来、以及只有人按下保存才会有记录（并写明采纳了草稿）。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.annotation_draft import (
    build_batch, build_prompt, parse_drafts,
)
from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
from astrbot_plugin_chat_dynamics.core.time_service import SystemClock
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.tests.test_topic_annotations import fixture_plugin, label

FENCE = chr(96) * 3


BOT_ID = "bot"


def node(msg_id, text, *, user="user", bot=False, mentions=(), reply_to=""):
    """A node authored by the bot is just a node whose author is the bot id."""
    return ConversationNode(msg_id, BOT_ID if bot else user, text, 1, metadata={},
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
    nodes = [node("m1", "在吗"), node("m2", "在的", bot=True), node("m3", "帮我看看")]

    batch, stats = build_batch(nodes, {}, limit=5, bot_id=BOT_ID)

    assert [item["msg_id"] for item in batch] == ["m1", "m2", "m3"]
    assert batch[1]["from_bot"] is True and batch[1]["draft_this"] is False
    assert [item["msg_id"] for item in batch if item["draft_this"]] == ["m1", "m3"]
    assert stats == {"window": 3, "draftable": 2, "asked": 2}


def test_a_bot_node_is_excluded_by_identity_not_by_a_metadata_key():
    """没有任何生产代码写节点的 is_bot 字段，所以归属只能由 bot_id 判定。"""
    nodes = [node("m1", "在吗"), node("m2", "在的", user="bot_9"), node("m3", "帮我看看")]

    batch, stats = build_batch(nodes, {}, limit=5, bot_id="bot_9")

    assert batch[1]["from_bot"] is True and batch[1]["draft_this"] is False
    assert [item["msg_id"] for item in batch if item["draft_this"]] == ["m1", "m3"]
    assert stats == {"window": 3, "draftable": 2, "asked": 2}


def test_an_unidentified_bot_node_is_still_draftable_without_bot_id():
    """没有 bot_id 时不能凭猜测排除，否则会把人类消息一起丢掉。"""
    batch, _stats = build_batch([node("m1", "在的", user="bot_9")], {}, limit=5)

    assert batch[0]["from_bot"] is False and batch[0]["draft_this"] is True


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

def test_a_numeric_msg_id_is_the_same_message_without_quotes():
    """消息平台常用数字 id；模型把引号丢掉不该静默丢掉整条草稿。"""
    batch, _stats = build_batch([node("12345", "在吗")], {}, limit=5)

    parsed = parse_drafts(reply(reply_for(12345)), batch)

    assert parsed is not None
    assert list(parsed["drafts"]) == ["12345"]


def test_a_bot_message_cannot_be_drafted_even_if_the_model_answers_for_it():
    batch, _stats = build_batch([node("m1", "在的", bot=True)], {}, limit=5,
                                bot_id=BOT_ID)

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
    # The approval payload converts DAG (monotonic) stamps at the presentation
    # boundary, so the double needs the clock a real plugin always has.
    plugin._time_service = SystemClock()
    # Draft ownership is the bot identity, which a live plugin reads from the session.
    plugin._sessions = {"a": SimpleNamespace(bot_id=BOT_ID)}
    kv = {}

    async def get(key, default):
        return kv.get(key, default)

    async def put(key, value):
        kv[key] = value

    # `nodes` is what the approval page and TopicAnnotations.save() look a
    # message up by; get_recent_nodes is what draft generation walks.
    dag = SimpleNamespace(get_recent_nodes=lambda count: list(nodes),
                          nodes={item.msg_id: item for item in nodes})
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
async def test_the_running_plugin_never_drafts_its_own_replies():
    """运行期路径：机器人自己发的消息不进草稿集，不占配额。"""
    plugin, _kv = draft_runtime(
        nodes=[node("m1", "在吗"), node("m2", "在的", user="bot_9")],
        reply=reply(reply_for("m1")))
    plugin._sessions = {"a": SimpleNamespace(bot_id="bot_9")}

    payload = await plugin.annotation_draft_payload("a")

    assert payload["state"] == "fresh"
    assert payload["stats"]["draftable"] == 1 and payload["stats"]["asked"] == 1
    assert list(payload["drafts"]) == ["m1"]


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



# ---- 接口层 -------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_endpoint_requires_a_session_and_returns_the_draft(monkeypatch, offline_web_responses):
    from astrbot_plugin_chat_dynamics.core import web_api
    from astrbot_plugin_chat_dynamics.core.web_api import ConsoleWebAPI

    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")], reply=reply(reply_for("m1")))
    plugin._shutting_down = False
    api = ConsoleWebAPI(plugin)

    async def empty_body():
        return {}

    monkeypatch.setattr(web_api, "_json_body", empty_body)
    assert (await api.annotation_draft())["status_code"] == 400

    async def with_session():
        return {"session_key": "a"}

    monkeypatch.setattr(web_api, "_json_body", with_session)
    result = await api.annotation_draft()

    assert result["ok"] is True
    assert result["data"]["state"] == "fresh"
    assert list(result["data"]["drafts"]) == ["m1"]


@pytest.mark.asyncio
async def test_the_endpoint_refuses_while_shutting_down(monkeypatch, offline_web_responses):
    from astrbot_plugin_chat_dynamics.core import web_api
    from astrbot_plugin_chat_dynamics.core.web_api import ConsoleWebAPI

    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")], reply=reply(reply_for("m1")))
    plugin._shutting_down = True
    api = ConsoleWebAPI(plugin)

    async def with_session():
        return {"session_key": "a"}

    monkeypatch.setattr(web_api, "_json_body", with_session)
    result = await api.annotation_draft()

    assert result["status_code"] == 503
    assert plugin.llm.calls == []


@pytest.mark.asyncio
async def test_an_unexpected_failure_is_a_503_not_a_traceback(monkeypatch, offline_web_responses):
    from astrbot_plugin_chat_dynamics.core import web_api
    from astrbot_plugin_chat_dynamics.core.web_api import ConsoleWebAPI

    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")])
    plugin._shutting_down = False

    async def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    plugin.annotation_draft_payload = explode
    api = ConsoleWebAPI(plugin)

    async def with_session():
        return {"session_key": "a"}

    monkeypatch.setattr(web_api, "_json_body", with_session)
    result = await api.annotation_draft()

    assert result["status_code"] == 503

# ---- 接线回归 -----------------------------------------------------------

def test_the_real_plugin_wires_the_store_the_draft_path_reads():
    """main.py 的草稿路径读 self.topic_annotations；接口层必须共享同一个实例。

    曾经插件类从不创建这个属性（只有 ConsoleWebAPI 自己 new 了一个），
    于是每次点「生成 AI 草稿」都在 annotation_draft_payload 里 AttributeError，
    前端只看到笼统的 503。这里用真实插件钉住接线。
    """
    from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockContext

    plugin = ChatDynamicsPlugin(MockContext(), {"decision_mode": "legacy", "enable": True})

    assert isinstance(plugin.topic_annotations, TopicAnnotations)
    assert plugin._web.topic_annotations is plugin.topic_annotations

# ---- 审批页：列表与批量处理 ---------------------------------------------

@pytest.mark.asyncio
async def test_the_approval_list_gathers_pending_drafts_without_plaintext():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                                reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")

    payload = await plugin.annotation_drafts_payload("")

    assert payload["total_drafts"] == 1 and payload["content_hidden"] is True
    session = payload["sessions"][0]
    assert session["session_key"] == "a" and session["provider_id"] == "provider-draft"
    item = session["items"][0]
    assert item["msg_id"] == "m1" and item["saveable"] is True
    assert item["annotated"] is False and item["text"] == ""
    assert item["expected_reply"] is True and item["confidence"] == 0.8


@pytest.mark.asyncio
async def test_the_approval_list_reports_wall_clock_timestamps():
    """列表里的时间要能当日期渲染：DAG 存的是单调时钟，页面要的是日历时间。"""
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                                reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")

    item = (await plugin.annotation_drafts_payload("a"))["sessions"][0]["items"][0]

    assert item["ts"] > 1e9, "单调时钟被直接下发给了页面"
    assert abs(item["ts"] - time.time()) < 86400


@pytest.mark.asyncio
async def test_the_approval_list_shows_text_only_under_the_content_switch():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                                reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")
    plugin.console_show_message_content = True

    payload = await plugin.annotation_drafts_payload("a")

    assert payload["content_hidden"] is False
    assert payload["sessions"][0]["items"][0]["text"] == "在吗"


@pytest.mark.asyncio
async def test_a_draft_whose_message_left_the_window_is_listed_but_not_saveable():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                                reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")
    plugin.dags["a"].nodes = {}

    payload = await plugin.annotation_drafts_payload("a")

    item = payload["sessions"][0]["items"][0]
    assert item["saveable"] is False


@pytest.mark.asyncio
async def test_accepting_a_draft_writes_a_label_and_drops_the_draft():
    plugin, kv = draft_runtime(nodes=[node("m1", "在吗")],
                               reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")

    result = await plugin.annotation_drafts_apply(
        {"action": "accept", "session_key": "a", "msg_ids": ["m1"]})

    assert result == {"saved": 1, "skipped": [], "failed": []}
    rows = kv[plugin.topic_annotations.key("a")]
    assert len(rows) == 1
    # Provenance is decided by save(), not by the caller: the label values are
    # the draft's, so the record has to say the model proposed them.
    assert rows[0]["label_source"] == "human" and rows[0]["accepted_from"] == "ai"
    assert rows[0]["expected_reply"] is True and rows[0]["bot_targeted"] is False
    assert kv[plugin.topic_annotations.draft_key("a")]["drafts"] == {}


@pytest.mark.asyncio
async def test_accepting_with_the_new_topic_choice_keeps_that_call():
    plugin, kv = draft_runtime(nodes=[node("m1", "在吗")],
                               reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")

    result = await plugin.annotation_drafts_apply(
        {"action": "accept", "session_key": "a", "msg_ids": ["m1"],
         "expected_topic": "NEW"})

    assert result["saved"] == 1
    row = kv[plugin.topic_annotations.key("a")][0]
    assert row["expected_topic"] == "NEW" and row["error_type"] == "topic_merge"


@pytest.mark.asyncio
async def test_a_draft_whose_message_left_the_window_is_skipped_not_saved():
    """重启或超出保留窗口后，草稿还在但消息没了 —— 只能跳过并说明。"""
    plugin, kv = draft_runtime(nodes=[node("m1", "在吗")],
                               reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")
    plugin.dags["a"].nodes = {}

    result = await plugin.annotation_drafts_apply(
        {"action": "accept", "session_key": "a", "msg_ids": ["m1"]})

    assert result["saved"] == 0 and result["failed"] == []
    assert result["skipped"][0]["msg_id"] == "m1"
    assert "保留窗口" in result["skipped"][0]["error"]
    assert kv[plugin.topic_annotations.key("a")] == [], "跳过的采纳不能留下标注"
    assert list(kv[plugin.topic_annotations.draft_key("a")]["drafts"]) == ["m1"]


@pytest.mark.asyncio
async def test_a_restart_that_dropped_the_graph_is_reported_as_such():
    plugin, kv = draft_runtime(nodes=[node("m1", "在吗")],
                               reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")
    plugin.dags.clear()
    plugin.topic_annotations.plugin.dags.clear()

    result = await plugin.annotation_drafts_apply(
        {"action": "accept", "session_key": "a", "msg_ids": ["m1"]})

    assert result["saved"] == 0 and result["failed"] == []
    assert "插件重启" in result["skipped"][0]["error"]
    assert kv[plugin.topic_annotations.key("a")] == []

@pytest.mark.asyncio
async def test_the_draft_index_keeps_drafts_visible_when_the_graph_is_gone():
    """重启后会话没新消息时，dag 整个消失；草稿仍要能被看到并清掉。"""
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                                reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")
    assert await plugin.topic_annotations.known_sessions() == ["a"]
    plugin.dags.clear()
    plugin.topic_annotations.plugin.dags.clear()

    payload = await plugin.annotation_drafts_payload("")

    assert [session["session_key"] for session in payload["sessions"]] == ["a"]
    item = payload["sessions"][0]["items"][0]
    assert item["saveable"] is False and item["stale_reason"] == "session_gone"


@pytest.mark.asyncio
async def test_evicted_message_and_missing_session_are_different_reasons():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                                reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")
    plugin.dags["a"].nodes = {}

    payload = await plugin.annotation_drafts_payload("a")

    assert payload["sessions"][0]["items"][0]["stale_reason"] == "evicted"


@pytest.mark.asyncio
async def test_a_session_leaves_the_index_once_it_has_no_drafts_left():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗"), node("m2", "在的")],
                                reply=reply(reply_for("m1"), reply_for("m2")))
    await plugin.annotation_draft_payload("a")

    await plugin.annotation_drafts_apply(
        {"action": "dismiss", "session_key": "a", "msg_ids": ["m1"]})
    assert await plugin.topic_annotations.known_sessions() == ["a"], "还有草稿就还在索引里"

    await plugin.annotation_drafts_apply(
        {"action": "dismiss", "session_key": "a", "msg_ids": ["m2"]})

    assert await plugin.topic_annotations.known_sessions() == []


@pytest.mark.asyncio
async def test_clearing_a_session_also_drops_it_from_the_index():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")],
                                reply=reply(reply_for("m1")))
    await plugin.annotation_draft_payload("a")

    await plugin.annotation_drafts_apply(
        {"action": "clear_session", "session_key": "a"})

    assert await plugin.topic_annotations.known_sessions() == []
    assert (await plugin.annotation_drafts_payload(""))["sessions"] == []


@pytest.mark.asyncio
async def test_dismissing_and_clearing_never_write_labels():
    plugin, kv = draft_runtime(nodes=[node("m1", "在吗"), node("m2", "在的")],
                               reply=reply(reply_for("m1"), reply_for("m2")))
    await plugin.annotation_draft_payload("a")

    dismissed = await plugin.annotation_drafts_apply(
        {"action": "dismiss", "session_key": "a", "msg_ids": ["m1"]})
    assert dismissed == {"removed": 1}
    assert list(kv[plugin.topic_annotations.draft_key("a")]["drafts"]) == ["m2"]
    assert kv[plugin.topic_annotations.key("a")] == []

    cleared = await plugin.annotation_drafts_apply(
        {"action": "clear_session", "session_key": "a"})
    assert cleared == {"cleared": True}
    assert kv[plugin.topic_annotations.draft_key("a")] == {}


@pytest.mark.asyncio
async def test_review_actions_reject_input_they_cannot_honour():
    plugin, _kv = draft_runtime(nodes=[node("m1", "在吗")])

    for body in (
        {"action": "accept", "session_key": "", "msg_ids": ["m1"]},
        {"action": "accept", "session_key": "a"},
        {"action": "nope", "session_key": "a", "msg_ids": ["m1"]},
        {"action": "accept", "session_key": "a", "msg_ids": ["m1"],
         "expected_topic": "WRONG"},
    ):
        with pytest.raises(ValueError):
            await plugin.annotation_drafts_apply(body)


@pytest.mark.asyncio
async def test_the_approval_endpoints_list_and_accept(monkeypatch, offline_web_responses):
    from astrbot_plugin_chat_dynamics.core import web_api
    from astrbot_plugin_chat_dynamics.core.web_api import ConsoleWebAPI

    plugin, kv = draft_runtime(nodes=[node("m1", "在吗")],
                               reply=reply(reply_for("m1")))
    plugin._shutting_down = False
    await plugin.annotation_draft_payload("a")
    api = ConsoleWebAPI(plugin)
    monkeypatch.setattr(web_api, "_query_param", lambda name: "")

    listed = await api.annotation_drafts_get()

    assert listed["ok"] is True and listed["data"]["total_drafts"] == 1

    async def accept_body():
        return {"action": "accept", "session_key": "a", "msg_ids": ["m1"]}

    monkeypatch.setattr(web_api, "_json_body", accept_body)
    applied = await api.annotation_drafts_post()

    assert applied["ok"] is True and applied["data"]["saved"] == 1
    assert kv[plugin.topic_annotations.key("a")][0]["accepted_from"] == "ai"

    async def empty_ids():
        return {"action": "dismiss", "session_key": "a", "msg_ids": []}

    monkeypatch.setattr(web_api, "_json_body", empty_ids)
    assert (await api.annotation_drafts_post())["status_code"] == 400


