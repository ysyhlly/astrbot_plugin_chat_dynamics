"""Per-message addressee questions and the conversation map given to the reply agent."""

import pytest
import json
from dataclasses import replace

from astrbot_plugin_chat_dynamics.core.conversation_map import (
    MAX_ADDRESSEE_QUESTIONS, build_addressee_task, conversation_map, resolve_addressees,
)
from astrbot_plugin_chat_dynamics.core.jev_decision import (
    ADDRESSEE_PREFIX, MAX_TOTAL_REQUEST_CHARS, build_questions, build_state, describe_answers, request_size,
)
from astrbot_plugin_chat_dynamics.core.message_semantics import MessageSemantics
from astrbot_plugin_chat_dynamics.core.topic_jev import TopicCandidateSnapshot
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext, TurnDecision, reply_prompt

BOT = "900"


def sem(sender, *, recipients=(), certainty="unknown", mentions=(), topic="", topic_conf=0.0,
        ambiguous_topic=True, bot_addr=False, quoted=""):
    return MessageSemantics(sender, sender == BOT, tuple(recipients), "routing" if recipients else "unknown",
                            certainty, "", tuple(mentions), (), (), "unknown", quoted_author_id=quoted,
                            topic_id=topic, topic_confidence=topic_conf, topic_ambiguous=ambiguous_topic,
                            bot_is_addressee=bot_addr, addressee_ambiguous=not recipients)


def msg(mid, author, name, text, semantics, **kw):
    return MessageSnapshot(mid, author, text, semantics=semantics, author_name=name, platform="aiocqhttp",
                           source_text=text, timestamp=float(len(mid)), **kw)


def scenario():
    background = (
        msg("b1", "111", "Alice", "Bob 你那个显卡驱动装好了吗", sem("111", topic="gpu", topic_conf=.9,
                                                             ambiguous_topic=False)),
        msg("b2", "222", "Bob", "装好了，还是黑屏", sem("222", recipients=("111",), certainty="probable",
                                                topic="gpu", topic_conf=.9, ambiguous_topic=False)),
        msg("b3", "333", "Carol", "今晚谁去吃火锅", sem("333", topic="dinner", topic_conf=.9,
                                               ambiguous_topic=False)),
        msg("b4", "444", "Dan", "@Alice 收到", sem("444", mentions=("111",)), mentioned_users=("111",)),
    )
    current = msg("c1", "222", "Bob", "安安，黑屏一般是什么原因", sem("222", topic="gpu", topic_conf=.85,
                                                              ambiguous_topic=False))
    candidates = (TopicCandidateSnapshot("gpu", "显卡驱动黑屏", "", frozenset(), 1.0, None),
                  TopicCandidateSnapshot("dinner", "晚饭火锅", "", frozenset(), 1.0, None))
    return TurnContext("room", "222", current.text, (current,), background, 0, 0, 0.0, False,
                       bot_id=BOT, wake_kind="name", topic_candidates=candidates, snapshot_at=10.0)


def choice(value, confidence=0.9):
    return {"type": "choice", "choice": value, "confidence": confidence, "probabilities": {value: confidence}}


def task(turn, **kw):
    state = build_state(turn)
    questions = build_questions(turn)
    added, mapping = build_addressee_task(turn, state, questions, **kw)
    return state, questions, added, mapping


def test_questions_skip_platform_mentions_and_offer_only_turn_members():
    turn = scenario()
    _, _, added, mapping = task(turn)
    asked = [spec["message_id"] for spec in mapping.values()]
    # Current fragment first, then background newest first; the real @ in b4 is not asked.
    assert asked == ["c1", "b3", "b2", "b1"]
    assert all(qid.startswith(ADDRESSEE_PREFIX) for qid in added)
    first = added[next(iter(mapping))]
    assert first["type"] == "choice"
    assert {"bot", "group", "unclear"} <= set(first["criteria"])
    members = set(mapping[next(iter(mapping))]["options"].values())
    assert BOT not in members and "222" not in members  # never the bot or the sender itself
    assert members <= {"111", "333", "444"}


def test_addressee_questions_never_exceed_the_request_budget():
    turn = scenario()
    state, questions, added, _ = task(turn)
    assert len(added) <= MAX_ADDRESSEE_QUESTIONS
    assert request_size(state, {**questions, **added}) <= MAX_TOTAL_REQUEST_CHARS
    tight = request_size(state, questions) + 10
    _, _, none_added, none_mapping = task(turn, max_chars=tight)
    assert none_added == {} and none_mapping == {}


def test_only_offered_valid_answers_are_resolved():
    turn = scenario()
    _, _, added, mapping = task(turn)
    keys = list(mapping)
    member_key = next(k for k, uid in mapping[keys[2]]["options"].items() if uid == "111")
    answers = {keys[0]: choice("bot", .92), keys[1]: choice("group", .8), keys[2]: choice(member_key, .7),
               keys[3]: choice("member_99", .9)}
    resolved = resolve_addressees(answers, mapping)
    assert resolved["c1"] == {"kind": "bot", "confidence": .92}
    assert resolved["b3"] == {"kind": "group", "confidence": .8}
    assert resolved["b2"] == {"kind": "member", "user_id": "111", "confidence": .7}
    assert "b1" not in resolved  # an option that was never offered is dropped
    assert describe_answers(answers)[keys[0]] == {"type": "choice", "choice": "bot", "confidence": .92}


def test_map_combines_platform_jev_and_router_evidence_with_topics():
    turn = scenario()
    resolved = {"c1": {"kind": "bot", "confidence": .92}, "b3": {"kind": "group", "confidence": .8},
                "b1": {"kind": "member", "user_id": "222", "confidence": .4},
                "b4": {"kind": "member", "user_id": "333", "confidence": .99}}
    data = conversation_map(turn, resolved, floor=.6)
    rows = {row["message_id"]: row for row in data["messages"]}
    assert [row["message_id"] for row in data["messages"]] == ["b1", "b2", "b3", "b4", "c1"]
    assert rows["c1"]["current"] is True
    assert rows["c1"]["addressee"] == {"kind": "bot", "confidence": .92, "source": "jev"}
    assert rows["b3"]["addressee"]["kind"] == "group"
    # Below the floor, Jev does not replace the router's reading.
    assert rows["b1"]["addressee"] == {"kind": "unclear", "source": "none"}
    # A real @ outranks any model estimate.
    assert rows["b4"]["addressee"] == {"kind": "member", "user_id": "111", "display_name": "Alice",
                                       "source": "platform"}
    assert rows["b2"]["addressee"]["user_id"] == "111" and rows["b2"]["addressee"]["source"] == "router"
    assert rows["c1"]["speaker"] == {"user_id": "222", "display_name": "Bob"}
    topics = {t["ref"]: t for t in data["topics"]}
    assert topics[rows["c1"]["topic"]] == {"ref": rows["c1"]["topic"], "label": "显卡驱动黑屏",
                                           "message_ids": ["b1", "b2", "c1"], "current": True}
    assert rows["b3"]["topic"] != rows["c1"]["topic"] and "current" not in topics[rows["b3"]["topic"]]
    assert "topic" not in rows["b4"]


def test_reply_prompt_carries_the_map_only_when_present():
    turn = scenario()
    decision = TurnDecision.fallback(turn, "test")
    assert "conversation_map" not in json.loads(reply_prompt(turn, decision))
    data = conversation_map(turn, {})
    payload = json.loads(reply_prompt(turn, decision, conversation_map=data))
    assert payload["conversation_map"] == data


def test_bot_messages_are_not_asked_and_are_marked_as_bot():
    turn = scenario()
    bot_line = msg("b5", BOT, "安安", "可以试试重装驱动", sem(BOT, recipients=("222",), certainty="explicit"))
    turn = replace(turn, background=(*turn.background, bot_line))
    _, _, _, mapping = task(turn)
    assert "b5" not in {spec["message_id"] for spec in mapping.values()}
    rows = {row["message_id"]: row for row in conversation_map(turn, {})["messages"]}
    assert rows["b5"]["speaker"] == {"user_id": BOT, "is_bot": True}


from .test_jev_decision_layer import JevDouble, answers as layer_answers, jev_plugin  # noqa: F401, E402
from .test_persona_model import drain, flush  # noqa: E402
from .test_plugin_lifecycle import MockEvent  # noqa: E402


@pytest.mark.asyncio
async def test_jev_addressee_answers_reach_the_reply_agent(jev_plugin):  # noqa: F811
    p, bridge = jev_plugin

    class AddresseeJev(JevDouble):
        async def evaluate(self, *, state, questions, timeout=None):
            payload = await super().evaluate(state=state, questions=questions, timeout=timeout)
            extra = {qid: choice("bot", .9) for qid in questions if qid.startswith(ADDRESSEE_PREFIX)}
            return {**payload, **extra}

    p.jev = AddresseeJev(layer_answers())
    try:
        event = MockEvent("有人知道这个报错怎么解决吗", message_id="m1")
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        questions = p.jev.calls[0]["questions"]
        asked = [qid for qid in questions if qid.startswith(ADDRESSEE_PREFIX)]
        prompt = bridge.requests[0][0]
        rows = {row["message_id"]: row for row in prompt["conversation_map"]["messages"]}
        assert rows["m1"]["current"] is True
        assert rows["m1"]["speaker"]["user_id"] == "user_1"
        assert asked, "a message without a platform @ is asked about"
        assert rows["m1"]["addressee"] == {"kind": "bot", "confidence": .9, "source": "jev"}
        runtime = p._sessions[event.unified_msg_origin]
        assert set(runtime.jev_addressees) <= {"m1"}
    finally:
        await p.terminate()
