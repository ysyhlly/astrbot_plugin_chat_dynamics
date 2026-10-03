"""Small requests retain attribution and constraints before any text clipping."""

from dataclasses import replace
import json

import pytest

from astrbot_plugin_chat_dynamics.core.decision_persona import decision_persona_summary
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import (
    build_questions, build_state, bound_request_state, decision_from_answers, request_size,
)
from astrbot_plugin_chat_dynamics.core.member_identity import IDENTITY_INSTRUCTIONS
from astrbot_plugin_chat_dynamics.core.message_semantics import describe_message
from astrbot_plugin_chat_dynamics.core.presence_policy import participation_policy
from astrbot_plugin_chat_dynamics.core.prompt_policy import DEFAULT_DECISION_PROMPT
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime, TopicState
from astrbot_plugin_chat_dynamics.core.topic_jev import build_topic_task, capture_topic_candidates
from astrbot_plugin_chat_dynamics.core.topic_resolution import TopicResolver
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent


def test_normal_input_deduplicates_identities_and_empty_fields_without_clipping_or_losing_confidence():
    dag = ConversationDAG("compact")
    parent = dag.add_message("quoted", "111001", "不能联网，保留完整代码。", timestamp=1)
    current = dag.add_message("current", "222002", "按引用要求写方案", timestamp=2, reply_to_id=parent.msg_id)
    semantics = replace(describe_message(current, dag, "990009"), addressee_confidence=.41,
                        bot_addressee_confidence=0.0, topic_confidence=0.0, addressee_ambiguous=True)
    message = MessageSnapshot("current", "222002", current.text, reply_to=parent.msg_id,
                              author_name="同名", platform="qq", semantics=semantics,
                              quoted_author_name="同名")
    messages = tuple(replace(message, message_id=f"m{i}") for i in range(3))
    background = tuple(MessageSnapshot(f"b{i}", "333003", "用户条件不截断" * 30,
                                      author_name="同名", platform="qq") for i in range(3))
    turn = TurnContext("compact", "222002", "\n".join(m.text for m in messages), messages, background,
                       0, 0, 0, False, bot_id="990009")
    original = turn.learning_payload()
    observations = {"pending_input": False, "queue_delay_seconds": 0, "empty": [], "nested": {"unused": None}}
    raw = {"conversation": turn.payload(), "identity_policy": IDENTITY_INSTRUCTIONS,
           "participation_policy": participation_policy("sensible"), "decision_prompt": DEFAULT_DECISION_PROMPT,
           "previous_state": "observing", "observations": observations, "persona": "人设"}
    assert len(json.dumps(raw, ensure_ascii=False)) < 12000
    state = build_state(turn, observations=observations, persona_prompt="人设")
    conversation = state["conversation"]
    assert request_size(state, build_questions(turn)) < request_size(raw, build_questions(turn))
    assert conversation["text"] == turn.text and not conversation["truncated"]
    assert [m["text"] for m in conversation["background"]] == [m.text for m in background]
    assert "author_identity" not in conversation["messages"][0]
    assert conversation["messages"][-1]["author_identity"]["qq"] == "222002"
    assert conversation["member_identities"]["333003"]["qq"] == "333003"
    assert conversation["quoted_identities"]["111001"]["qq"] == "111001"
    for m in conversation["messages"]:
        assert m["semantics"]["quoted_message_id"] == parent.msg_id
        assert m["semantics"]["quoted_author_id"] == "111001"
        assert m["semantics"]["parent_confidence"] == 1.0
        assert m["semantics"]["addressee_confidence"] == .41
        assert m["semantics"]["bot_addressee_confidence"] == 0.0
        assert m["semantics"]["topic_confidence"] == 0.0
        assert m["semantics"]["addressee_ambiguous"] is True
        assert "attachments" not in m and "text_excerpt" not in m
    assert state["observations"] == {"pending_input": False, "queue_delay_seconds": 0}
    assert turn.learning_payload() == original and observations["empty"] == []


def test_same_account_historical_names_and_distinct_accounts_are_not_merged():
    messages = (MessageSnapshot("old-fragment", "222", "补充", author_name="旧名", platform="qq"),
                MessageSnapshot("new-fragment", "222", "问题", author_name="新名", platform="qq"))
    background = (MessageSnapshot("a", "111", "条件", author_name="新名", platform="qq"),
                  MessageSnapshot("b", "111", "补充", author_name="旧名", platform="qq"))
    turn = TurnContext("names", "222", "补充\n问题", messages, background, 0, 0, 0, False)
    conversation = build_state(turn)["conversation"]
    assert conversation["messages"][0]["author_identity"]["display_name"] == "旧名"
    assert conversation["speaker_identity"]["display_name"] == "新名"
    assert [m["author_identity"]["qq"] for m in conversation["background"]] == ["111", "111"]
    assert [m["author_identity"]["display_name"] for m in conversation["background"]] == ["新名", "旧名"]


@pytest.mark.parametrize("wake", ["quote", "name", "supplement"])
def test_soft_wakes_omit_join_and_still_require_a_confident_bot_recipient(wake):
    turn = TurnContext("soft", "u", "继续", (MessageSnapshot("m", "u", ""),), (), 0, 0, 0, True,
                       wake_kind=wake)
    questions = build_questions(turn)
    assert "join" not in questions
    assert {"recipient", "completion", "action", "state", "length", "reason"} <= questions.keys()
    payload = answers(recipient={"type": "choice", "choice": "bot", "confidence": .35})
    payload.pop("join")
    assert decision_from_answers(turn, payload).action == "reply"
    payload["recipient"]["confidence"] = .34
    assert decision_from_answers(turn, payload).action == "ignore"
    payload["recipient"].update(choice="other", confidence=.99)
    assert decision_from_answers(turn, payload).action == "ignore"
    assert "join" in build_questions(replace(turn, wake_kind="none", explicit=False))


def topic_runtime():
    rt = SessionRuntime("r", "g", "r", dag=ConversationDAG("r"))
    for i in range(70):
        rt.routing_state.topics[f"noise-{i}"] = TopicState(f"noise-{i}", generated_title="显卡风扇调节",
            summary_excerpts=["显卡散热问题"], created_at=800+i, updated_at=800+i)
    quote = rt.dag.add_message("quote", "alice", "原来的离线部署条件", timestamp=1)
    TopicResolver.remember(rt.routing_state, quote, "quoted-topic", dag=rt.dag)
    confirmed = rt.dag.add_message("prior", "bob", "需要保存旧格式", timestamp=2)
    TopicResolver.remember(rt.routing_state, confirmed, "confirmed-topic", dag=rt.dag)
    current = rt.dag.add_message("current", "u", "显卡散热问题", timestamp=1000, reply_to_id=quote.msg_id)
    current.metadata["routing"] = {"topic_id": "confirmed-topic", "topic_status": "committed",
                                   "topic_confidence": .8, "topic_ambiguous": False}
    turn = TurnContext("r", "u", current.text,
        (MessageSnapshot(current.msg_id, "u", current.text, reply_to=quote.msg_id),), (), 0, 0, 1000, False)
    return rt, turn


def test_quote_and_confirmed_continuation_topics_are_required_despite_no_lexical_overlap():
    rt, turn = topic_runtime()
    turn = replace(turn, topic_candidates=capture_topic_candidates(rt, turn))
    rt.dag.nodes["current"].metadata["routing"]["topic_id"] = "noise-0"
    descriptions, questions, mapping = build_topic_task(rt, turn, 1000)
    assert len(descriptions) == 8
    assert {t.topic_id for key, t in mapping["topics"].items() if descriptions[key].get("required")} == {
        "quoted-topic", "confirmed-topic"}
    assert all(d["excerpt"] for d in descriptions.values())
    state = build_state(turn, active_topics=descriptions, max_chars=4000)
    state = bound_request_state(state, build_questions(turn), max_chars=10000)
    assert {k for k, d in descriptions.items() if d.get("required")} <= state["active_topics"].keys()
    assert len(questions["topic"]["criteria"]) == 10


def test_ambiguous_current_topic_does_not_displace_a_related_shortlist_candidate():
    rt, turn = topic_runtime()
    rt.dag.nodes["current"].metadata["routing"]["topic_ambiguous"] = True
    descriptions, _, mapping = build_topic_task(rt, turn, 1000)
    offered = {t.topic_id for t in mapping["topics"].values()}
    assert "quoted-topic" in offered and "confirmed-topic" not in offered
    assert sum(bool(d.get("required")) for d in descriptions.values()) == 1


def test_more_than_eight_required_topics_are_retained_and_expired_ones_are_rejected():
    rt, turn = topic_runtime()
    for i in range(9):
        old = rt.dag.add_message(f"old-{i}", "alice", f"条件{i}", timestamp=3+i)
        TopicResolver.remember(rt.routing_state, old, f"required-{i}", dag=rt.dag)
    messages = tuple(MessageSnapshot(f"fragment-{i}", "u", "继续", reply_to=f"old-{i}") for i in range(9))
    for m in messages:
        rt.dag.add_message(m.message_id, "u", m.text, timestamp=1000, reply_to_id=m.reply_to)
    turn = replace(turn, messages=messages, text="\n".join(m.text for m in messages))
    rt.routing_state.topics["required-0"].human_updated_at = -3000
    descriptions, _, mapping = build_topic_task(rt, turn, 1000)
    assert len(descriptions) == 8
    assert {t.topic_id for t in mapping["topics"].values()} == {f"required-{i}" for i in range(1, 9)}
    rt.routing_state.topics["required-0"].human_updated_at = 3
    descriptions, _, mapping = build_topic_task(rt, turn, 1000)
    assert len(descriptions) == 9 and all(d.get("required") for d in descriptions.values())


def long_persona():
    return ("你是小舟，身份是普通群成员。\n" + "她喜欢收集旅行故事。\n" * 160
            + "参与倾向：只在能帮助推进话题时接话。\n" + "她收藏各种明信片。\n" * 160
            + "边界：\n- 不主动介入私人争执。\n- 不要透露任何群友隐私。")


def test_decision_persona_preserves_identity_participation_and_late_boundaries():
    prompt = long_persona()
    assert prompt.index("边界") > 1200
    summary = decision_persona_summary(prompt)
    assert len(summary) <= 1200
    assert "你是小舟" in summary and "只在能帮助推进话题时接话" in summary
    assert "不主动介入私人争执" in summary and "不要透露任何群友隐私" in summary
    assert decision_persona_summary("简短角色卡") == "简短角色卡"


@pytest.mark.asyncio
async def test_reply_agent_still_receives_the_entire_persona(jev_plugin):
    p, bridge = jev_plugin
    bridge.persona = replace(bridge.persona, prompt=long_persona())
    event = MockEvent("小助手请帮我解释一下", message_id="summary")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(p.jev.calls) == len(bridge.requests) == 1
        assert p.jev.calls[0]["state"]["persona"] == decision_persona_summary(bridge.persona.prompt)
        assert bridge.requests[0][1].prompt == bridge.persona.prompt
    finally:
        await p.terminate()
