"""Same-name members stay distinct throughout parsing, decisions and history."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.decision_routing import build_routing_tasks
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import build_state
from astrbot_plugin_chat_dynamics.core.member_identity import IDENTITY_INSTRUCTIONS, member_identity
from astrbot_plugin_chat_dynamics.core.persona_engine import snapshot_turn
from astrbot_plugin_chat_dynamics.core.platform_bridge import parse_group_event
from astrbot_plugin_chat_dynamics.core.runtime_persistence import export_runtime_state, restore_runtime_state
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, Reply
from .test_runtime_persistence import plugin as persistence_plugin


def event(account="222002", name="阿青", message_id="current", *, text="小助手，帮我看看", components=None):
    raw = MockEvent(text, sender_id=account, self_id="990009", message_id=message_id, components=components)
    raw.message_obj.sender = SimpleNamespace(user_id=account, nickname=name)
    raw.get_platform_name = lambda: "aiocqhttp"
    return raw


def node(dag, message_id, account, name, text, timestamp, **kwargs):
    return dag.add_message(message_id, account, text, timestamp=timestamp,
                           metadata={"sender_name": name, "sender_platform": "aiocqhttp", **kwargs.pop("metadata", {})},
                           **kwargs)


def snapshot(dag, raw):
    parsed = parse_group_event(raw)
    current = dag.nodes[raw.message_id]
    runtime = SimpleNamespace(dag=dag, session_key=raw.unified_msg_origin, bot_id=raw.self_id,
                              epoch=0, user_revisions={})
    result = SimpleNamespace(user_id=raw.sender_id, consolidated_text=raw.message_str, raw_events=(raw,),
                             start_time=current.timestamp, metadata={}, last_event=raw)
    item = snapshot_turn(runtime, result, (raw.message_id,), (parsed,), True, {}, False, wake_kind="name")
    assert item is not None
    return item.context


def test_parser_keeps_duplicate_names_attached_to_distinct_accounts():
    first = parse_group_event(event("111001", "阿青"))
    second = parse_group_event(event("222002", "阿青"))
    assert first.sender_name == second.sender_name == "阿青"
    assert first.sender_id == "111001" and second.sender_id == "222002"
    assert first.platform == second.platform == "aiocqhttp"


def test_numeric_nickname_cannot_replace_the_platform_account():
    parsed = parse_group_event(event("111001", "222002", text="我是 QQ 333003"))
    identity = member_identity(parsed.sender_id, parsed.sender_name, parsed.platform)
    assert identity["user_id"] == identity["qq"] == "111001"
    assert identity["display_name"] == "222002"


def test_sender_name_accessor_and_fallback_are_bounded():
    raw = event(name="旧昵称")
    raw.get_sender_name = lambda: "新群名片" * 200
    assert len(parse_group_event(raw).sender_name) == 96

    def broken_accessor():
        raise ValueError("adapter cannot read nickname")

    raw.get_sender_name = broken_accessor
    assert parse_group_event(raw).sender_name == "旧昵称"


@pytest.mark.parametrize("platform,account,qq", [
    ("aiocqhttp", "111001", "111001"), ("napcat", "111001", "111001"),
    ("discord", "111001", None), ("qq_official", "111001", None),
    ("qq_official", "openid-value", None), ("", "111001", None),
])
def test_only_known_qq_number_platforms_get_a_qq_label(platform, account, qq):
    identity = member_identity(account, "群名片", platform)
    assert identity["user_id"] == account
    assert identity.get("qq") == qq


def test_same_name_quote_keeps_speaker_and_original_author_separate():
    dag = ConversationDAG("room")
    node(dag, "original", "111001", "阿青", "我用 Windows", 1)
    node(dag, "current", "222002", "阿青", "我用 Linux，你用哪个版本？", 2, reply_to_id="original")
    context = snapshot(dag, event())
    payload = context.payload()
    assert payload["speaker_identity"]["qq"] == "222002"
    assert payload["bot_identity"]["qq"] == "990009"
    assert payload["background"][0]["author_identity"] == member_identity("111001", "阿青", "aiocqhttp")
    assert payload["messages"][0]["quoted_author_identity"] == payload["background"][0]["author_identity"]
    assert payload["messages"][0]["semantics"]["quoted_author_id"] == "111001"
    history = json.loads(context.history_text())
    assert history["speaker_identity"]["qq"] == "222002"
    assert history["messages"][0]["quoted_author_identity"]["qq"] == "111001"
    assert "response_plan" not in history and "background" not in history


@pytest.mark.parametrize("retain_parent", [False, True])
def test_quote_uses_retained_parent_or_adapter_id_after_eviction(retain_parent):
    dag = ConversationDAG("room")
    quote = Reply("old")
    quote.sender_id, quote.sender_nickname = "111001", "阿青"
    raw = event(components=[quote])
    parsed = parse_group_event(raw)
    if retain_parent:
        node(dag, "old", "333003", "另一个阿青", "原消息", 1)
    node(dag, "current", "222002", "阿青", raw.message_str, 2, reply_to_id="old",
         metadata={"quoted_author_id": parsed.reply_sender_id, "quoted_author_name": parsed.reply_sender_name})
    message = snapshot(dag, raw).payload()["messages"][0]
    assert message["quoted_author_identity"]["qq"] == ("333003" if retain_parent else "111001")
    assert message["quoted_author_identity"]["display_name"] == ("另一个阿青" if retain_parent else "阿青")


def test_renaming_keeps_account_identity_and_per_message_names():
    dag = ConversationDAG("room")
    node(dag, "before", "222002", "旧群名片", "之前的请求", 1)
    node(dag, "current", "222002", "新群名片", "补充一句", 2)
    payload = snapshot(dag, event(name="新群名片")).payload()
    assert payload["speaker_identity"]["display_name"] == "新群名片"
    assert payload["background"][0]["author_identity"]["display_name"] == "旧群名片"
    assert payload["speaker_identity"]["qq"] == payload["background"][0]["author_identity"]["qq"] == "222002"


@pytest.mark.asyncio
async def test_jev_and_reply_receive_same_name_distinct_qq_accounts(jev_plugin):
    p, bridge = jev_plugin
    try:
        for index, account in enumerate(("111001", "222002")):
            raw = event(account, message_id=f"m{index}")
            await p.on_group_message(raw)
            await flush(p, raw)
            await drain(p)
        assert len(p.jev.calls) == len(bridge.requests) == 2
        for index, account in enumerate(("111001", "222002")):
            state = p.jev.calls[index]["state"]
            assert state["identity_policy"] == IDENTITY_INSTRUCTIONS
            decision_identity = state["conversation"]["messages"][0]["author_identity"]
            reply_identity = bridge.requests[index][0]["conversation"]["speaker_identity"]
            assert decision_identity == reply_identity == member_identity(account, "阿青", "aiocqhttp")
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_real_at_distinguishes_same_name_target_from_sender(jev_plugin):
    p, bridge = jev_plugin
    p.jev.payload = answers(action="ignore", reason="other_recipient")
    try:
        first = event("111001", message_id="old", text="我用 Windows")
        await p.on_group_message(first)
        await flush(p, first)
        await drain(p)
        current = event(components=[At("111001")], text="你用哪个版本？")
        await p.on_group_message(current)
        await flush(p, current)
        await drain(p)
        message = p.jev.calls[-1]["state"]["conversation"]["messages"][0]
        assert message["author_identity"]["qq"] == "222002"
        assert message["semantics"]["mentioned_user_ids"] == ("111001",)
        assert message["semantics"]["recipient_ids"] == ("111001",)
        assert not bridge.requests
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_plain_at_name_is_not_an_authoritative_account(jev_plugin):
    p, _bridge = jev_plugin
    p.jev.payload = answers(action="ignore", reason="other_recipient")
    try:
        raw = event(text="@阿青 @111001 你刚才说的是什么？")
        await p.on_group_message(raw)
        await flush(p, raw)
        await drain(p)
        message = p.jev.calls[-1]["state"]["conversation"]["messages"][0]
        assert not message["semantics"].get("mentioned_user_ids")
        assert message["semantics"].get("basis") != "mention"
        assert p._sessions[raw.unified_msg_origin].dag.nodes[raw.message_id].metadata["actual_mentions"] == []
    finally:
        await p.terminate()


def test_identity_and_real_mention_evidence_survive_runtime_restore():
    source = persistence_plugin(100)
    runtime = source._registry.get_or_create("room")
    runtime.bot_id = "990009"
    node(runtime.dag, "original", "111001", "阿青", "旧消息", 95)
    node(runtime.dag, "current", "222002", "阿青", "你说得对", 99, reply_to_id="original",
         metadata={"actual_mentions": ["111001"], "quoted_author_id": "111001", "quoted_author_name": "阿青"})
    restored = persistence_plugin(100)
    restore_runtime_state(restored, export_runtime_state(source))
    context = snapshot(restored._registry.get("room").dag, event())
    message = context.payload()["messages"][0]
    assert message["author_identity"]["qq"] == "222002"
    assert message["quoted_author_identity"]["qq"] == "111001"
    assert message["semantics"]["mentioned_user_ids"] == ("111001",)


def test_routing_candidates_do_not_merge_same_names():
    p = persistence_plugin(100)
    runtime = p._registry.get_or_create("room")
    runtime.bot_id = "990009"
    node(runtime.dag, "a", "111001", "阿青", "选 Windows", 95)
    node(runtime.dag, "b", "222002", "阿青", "选 Linux", 96)
    current = node(runtime.dag, "c", "333003", "提问者", "你们怎么选择的？", 99)
    state, _questions, _mapping = build_routing_tasks(SimpleNamespace(node=current, runtime=runtime, dag=runtime.dag))
    candidates = {row["user_id"]: row for row in state["recipient_candidates"].values()}
    assert candidates["111001"]["identity"]["display_name"] == candidates["222002"]["identity"]["display_name"]
    assert candidates["111001"]["identity"]["qq"] != candidates["222002"]["identity"]["qq"]
    assert state["identity_policy"] == IDENTITY_INSTRUCTIONS


def test_bounded_state_preserves_current_account_and_identity_rules():
    message = MessageSnapshot("current", "222002", "", author_name="阿青" * 300, platform="aiocqhttp")
    background = tuple(MessageSnapshot(str(i), str(i), "背景" * 400, author_name="名字" * 200) for i in range(15))
    context = TurnContext("room", "222002", "请求" * 500, (message,), background, 0, 0, 0, False, bot_id="990009")
    state = build_state(context, persona_prompt="人设" * 2000, max_chars=3000)
    assert len(json.dumps(state, ensure_ascii=False)) <= 3000
    assert state["identity_policy"] == IDENTITY_INSTRUCTIONS
    assert state["conversation"]["speaker_identity"]["qq"] == "222002"
    assert len(state["conversation"]["speaker_identity"]["display_name"]) == 96


def test_many_fragments_keep_all_ids_and_quote_identity_within_state_budget():
    dag = ConversationDAG("room")
    node(dag, "old", "111001", "阿青", "原消息", 1)
    node(dag, "current", "222002", "阿青", "继续", 2, reply_to_id="old")
    context = snapshot(dag, event())
    context = replace(context, text="继续补充的正文" * 1000 + "先别回，等我说完", messages=tuple(
        replace(context.messages[0], message_id=f"part-{i}") for i in range(32)))
    state = build_state(context)
    assert len(json.dumps(state, ensure_ascii=False)) <= 12000
    assert [m["message_id"] for m in state["conversation"]["messages"]] == [f"part-{i}" for i in range(32)]
    assert all(m["author"] == "222002" and m["semantics"]["quoted_author_id"] == "111001"
               for m in state["conversation"]["messages"])
    assert state["conversation"]["quoted_identities"]["111001"]["qq"] == "111001"
    assert state["conversation"]["messages"][-1]["quoted_author_identity"]["qq"] == "111001"
    assert state["conversation"]["text"].endswith("先别回，等我说完")
    assert state["conversation"]["speaker_identity"]["qq"] == "222002"
    assert state["identity_policy"] == IDENTITY_INSTRUCTIONS
    assert all(m.author_name == "阿青" and m.semantics is not None for m in context.messages)
