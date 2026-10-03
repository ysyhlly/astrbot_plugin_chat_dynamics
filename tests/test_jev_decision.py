"""The Jev decision layer maps typed answers onto the plugin's own decision contract."""

import json
from dataclasses import asdict

import pytest

from astrbot_plugin_chat_dynamics.core.jev_decision import (
    ACTIONS, LENGTHS, REASONS, REASON_PREFIX, STATES, StateBudgetExceeded, build_questions, build_state,
    decision_confidence, decision_from_answers, describe_answers, target_options,
)
from astrbot_plugin_chat_dynamics.core.turn_decision import (
    MessageSnapshot, TurnContext, TurnDecision,
)


def turn(*ids, text="帮我看看这个报错", explicit=True, author="user") -> TurnContext:
    messages = tuple(MessageSnapshot(message_id, author, "") for message_id in ids)
    return TurnContext("room", author, text, messages, (), 0, 0, 0.0, explicit)


def choice(value, confidence=0.9):
    return {"type": "choice", "choice": value, "confidence": confidence, "probabilities": {value: confidence}}


def answers(**overrides):
    payload = {
        "join": {"type": "noul", "noul": 0.82},
        "action": choice("reply", 0.88),
        "state": choice("focused", 0.75),
        "length": choice("brief", 0.7),
        "reason": choice("addressed_question", 0.8),
    }
    payload.update(overrides)
    return payload


def test_questions_offer_only_the_plugin_vocabulary():
    questions = build_questions(turn("m1", "m2"))
    assert set(questions) == {"completion", "join", "action", "state", "length", "reason", "target"}
    assert questions["join"]["type"] == "noul"
    assert tuple(questions["action"]["criteria"]) == ACTIONS
    assert tuple(questions["state"]["criteria"]) == STATES
    assert tuple(questions["length"]["criteria"]) == LENGTHS
    assert tuple(questions["reason"]["criteria"]) == REASONS
    for question in questions.values():
        assert question["instructions"]


def test_a_single_message_turn_asks_no_target_question():
    questions = build_questions(turn("m1"))
    assert "target" not in questions
    assert target_options(turn("m1")) == {"m1": "the newest message, sent by user"}


def test_target_options_stay_bounded_and_ordered():
    turn_context = turn(*[f"m{index}" for index in range(12)])
    options = target_options(turn_context, limit=3)
    assert list(options) == ["m9", "m10", "m11"]
    assert "sent by user" in options["m9"]
    assert options["m11"] == "the newest message, sent by user"


def test_state_is_bounded_before_it_is_sent():
    background = tuple(
        MessageSnapshot(f"b{index}", "other", "背景" * 200) for index in range(15)
    )
    context = TurnContext("room", "user", "正文" * 500, (MessageSnapshot("m1", "user", ""),),
                          background, 0, 0, 0.0, False)
    state = build_state(context, previous_state="casual", observations={"scene_tags": ["work"]},
                        presence="lively", persona_prompt="克制" * 2000, max_chars=3000)
    assert len(json.dumps(state, ensure_ascii=False)) <= 3000
    assert len(state["persona"]) <= 1200
    assert state["previous_state"] == "casual"
    assert state["participation_policy"]["presence_knob"] == "lively"
    # The turn itself is never mutated by shrinking a copy of it.
    assert len(context.messages) == 1 and len(context.background) == 15 and len(context.text) == 1000


def test_unbounded_state_keeps_what_matters():
    context = turn("m1", text="只有一句话")
    state = build_state(context, observations={"x": "y"}, persona_prompt="人设")
    assert state["persona"] == "人设" and state["observations"] == {"x": "y"}
    assert state["conversation"]["text"] == "只有一句话"


@pytest.mark.parametrize("count", [1, 24, 32])
def test_many_topics_and_fragments_keep_current_text_and_fit_the_hard_budget(count):
    context = turn(*[f"m{i}" for i in range(count)], text="当前问题必须保留")
    topics = {f"topic_{i}": {"label": "l" * 48, "excerpt": "e" * 180 if i < 8 else ""} for i in range(80)}
    before = json.dumps(topics)
    state = build_state(context, active_topics=topics)
    assert len(json.dumps(state, ensure_ascii=False)) <= 12000
    assert state["conversation"]["text"] == context.text
    assert [m["message_id"] for m in state["conversation"]["messages"]] == [m.message_id for m in context.messages]
    assert json.dumps(topics) == before


def test_impossible_state_budget_rejects_instead_of_dropping_identity_or_returning_oversize():
    with pytest.raises(StateBudgetExceeded, match="state_budget_exceeded"):
        build_state(turn("m1", text="当前请求"), max_chars=100)


def test_answers_become_the_plugin_decision_contract():
    context = turn("m1", "m2")
    decision = decision_from_answers(context, answers(target=choice("m1", 0.66)))
    assert decision.action == "reply"
    assert decision.state == "focused"
    assert decision.length == "brief"
    assert decision.reason_code == "jev_addressed_question"
    assert decision.target_message_ids == ("m1", "m2")
    assert 0 < len(decision.response_goal) <= 600
    # The mapped decision satisfies the same contract the model path is parsed into.
    assert TurnDecision.parse(json.dumps(asdict(decision), ensure_ascii=False), context) == decision


def test_target_outside_the_turn_falls_back_to_the_turn_itself():
    context = turn("m1", "m2")
    decision = decision_from_answers(context, answers(target=choice("elsewhere")))
    assert decision.target_message_ids == ("m1", "m2")


def test_a_background_message_cannot_head_the_reply():
    """Only turn messages are target options, so a background ID is not a pick."""
    context = TurnContext("room", "user", "帮我看看这个报错",
                          (MessageSnapshot("m1", "user", ""),),
                          (MessageSnapshot("b1", "other", "背景"),), 0, 0, 0.0, True)
    decision = decision_from_answers(context, answers(target=choice("b1")))
    assert decision.target_message_ids == ("m1",)


def test_ignore_keeps_the_turn_messages_as_context():
    context = turn("m1", explicit=False)
    decision = decision_from_answers(
        context, answers(action=choice("ignore"), reason=choice("other_recipient")))
    assert decision.action == "ignore"
    assert decision.reason_code == "jev_other_recipient"
    assert decision.target_message_ids == ("m1",)


def test_join_noul_below_the_floor_declines():
    context = turn("m1", explicit=False)
    decision = decision_from_answers(context, answers(join={"type": "noul", "noul": 0.2}))
    assert decision.action == "ignore"
    assert decision.reason_code == "jev_join_declined"


def test_join_above_the_floor_leaves_the_action_alone():
    context = turn("m1")
    decision = decision_from_answers(context, answers(join={"type": "noul", "noul": 0.75}))
    assert decision.action == "reply"


def test_action_confidence_below_the_floor_is_not_a_decision():
    context = turn("m1")
    payload = answers(action=choice("reply", 0.4))
    decision = decision_from_answers(context, payload, min_confidence=0.6)
    assert decision == TurnDecision.fallback(context, "jev_low_confidence")
    assert decision.action == "reply"  # explicit turns still get the local plan


@pytest.mark.parametrize("weak,value,expected", [
    ("state", "supportive", ("focused", "brief", "jev_addressed_question")),
    ("length", "detailed", ("focused", "brief", "jev_addressed_question")),
    ("reason", "addressed_request", ("focused", "brief", "jev_action_accepted")),
])
def test_uncertain_presentation_uses_defaults_without_vetoing_participation(weak, value, expected):
    context = turn("m1", explicit=False)
    decision = decision_from_answers(context, answers(**{weak: choice(value, 0.4)}))
    assert decision.action == "reply"
    assert (decision.state, decision.length, decision.reason_code) == expected
    if weak == "reason":
        assert decision.response_goal == "针对目标消息给出实质回复，不展开无关内容。"


def test_all_uncertain_presentation_answers_cannot_bypass_the_join_floor():
    context = turn("m1", explicit=False)
    payload = answers(state=choice("playful", 0.2), length=choice("detailed", 0.2),
                      reason=choice("open_group_topic", 0.2))
    admitted = decision_from_answers(context, payload)
    assert (admitted.action, admitted.state, admitted.length, admitted.reason_code) == (
        "reply", "focused", "brief", "jev_action_accepted")
    payload["join"] = {"type": "noul", "noul": 0.2}
    refused = decision_from_answers(context, payload)
    assert (refused.action, refused.state, refused.reason_code) == ("ignore", "observing", "jev_join_declined")


def test_confidence_at_the_floor_keeps_the_selected_presentation():
    context = turn("m1", explicit=False)
    decision = decision_from_answers(context, answers(action=choice("reply", 0.6),
        state=choice("supportive", 0.6), length=choice("normal", 0.6), reason=choice("ongoing_thread", 0.6)))
    assert (decision.action, decision.state, decision.length, decision.reason_code) == (
        "reply", "supportive", "normal", "jev_ongoing_thread")


def test_low_confidence_ambient_stays_silent():
    context = turn("m1", explicit=False)
    decision = decision_from_answers(
        context, answers(action=choice("reply", 0.3)), min_confidence=0.6)
    assert decision.action == "ignore" and decision.state == "observing"


def test_the_floor_is_configurable():
    context = turn("m1")
    accepted = decision_from_answers(context, answers(), min_confidence=0.5)
    refused = decision_from_answers(context, answers(), min_confidence=0.95)
    assert accepted.reason_code == "jev_addressed_question"
    # An explicit turn still gets the local plan, so the reason code — not the action —
    # is what shows the answer was refused.
    assert refused.reason_code == "jev_low_confidence"


@pytest.mark.parametrize("payload", [
    {},
    {"action": choice("reply")},
    answers(action=choice("shout")),
    answers(state=choice("hysterical")),
    answers(length=choice("epic")),
    answers(reason=choice("because")),
    answers(action={"type": "noul", "noul": 0.9}),
    answers(length={"type": "choice", "choice": "brief"}),
    answers(**{"join": {"type": "noul", "noul": 1.5}}),
])
def test_unmappable_answers_are_refused_rather_than_approximated(payload):
    context = turn("m1")
    assert decision_from_answers(context, payload) == TurnDecision.fallback(
        context, "jev_invalid_answer")


def test_every_reason_code_matches_the_trace_contract():
    import re
    for reason in REASONS:
        code = REASON_PREFIX + reason
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,47}", code), code


def test_evidence_reports_action_confidence_and_keeps_presentation_uncertainty():
    payload = answers()
    described = describe_answers(payload)
    assert described["action"] == {"type": "choice", "choice": "reply", "confidence": 0.88}
    assert described["join"] == {"type": "noul", "noul": 0.82}
    assert described["confidence"] == pytest.approx(0.88)
    assert described["length"]["confidence"] == pytest.approx(0.7)
    assert decision_confidence(payload) == pytest.approx(0.88)
    json.dumps(described)
    assert decision_confidence({"action": choice("reply")}) == pytest.approx(0.9)
    assert decision_confidence({"action": {"type": "choice", "choice": "reply"}}) == 0.0
