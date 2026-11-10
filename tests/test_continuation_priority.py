"""Keep real owned exchanges engaged without authorizing unrelated messages."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core.dialogue_context import capture_dialogue
from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.daily_rhythm import DailyRhythmVerdict
from astrbot_plugin_chat_dynamics.core.jev_decision import build_questions, decision_from_answers, describe_answers
from .test_dialogue_context import room, delivered, route, turn
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent


def choice(value, confidence=0.9):
    return {"type": "choice", "choice": value, "confidence": confidence}


def conversation(text="其实我是说另一个细节", *, age=240, state="casual", closed=False, topic=True, question=False):
    runtime = room()
    _, bot = delivered(
        runtime,
        state=state,
        action="close" if closed else "reply",
        text="你最在意的是哪一点？" if question else "我们可以接着聊这个细节",
    )
    current = runtime.dag.add_message("current", "alice", text, timestamp=bot.timestamp + age)
    if topic:
        route(current)
    visible = frozenset(runtime.dag.nodes)
    snapshot = capture_dialogue(runtime, current, visible)
    context = replace(turn(current, runtime), dialogue=snapshot, visible_node_ids=visible)
    return runtime, current, context


@pytest.mark.parametrize("wake", ["none", "supplement", "name"])
@pytest.mark.parametrize("action", ["ignore", "reply", "close"])
def test_confirmed_followup_survives_generic_ignore_and_low_join(wake, action):
    _, _, context = conversation()
    context = replace(context, wake_kind=wake)
    payload = answers(
        action=action, reason="low_value_chatter", join={"type": "noul", "noul": 0.01}, continuity=choice("continue")
    )
    result = decision_from_answers(context, payload)
    assert result.action == "reply" and result.reason_code == "jev_dialogue_continue"
    questions = build_questions(context)
    assert "continuity" in questions and "recipient" in questions
    assert describe_answers(payload)["continuity"]["choice"] == "continue"


def test_long_delayed_answer_without_topic_words_keeps_pending_question():
    _, _, context = conversation(
        "因为前几次都没说清楚，我想这次从头把原因解释一下，这样你才能理解我为什么会这么想。", question=True, topic=False
    )
    assert context.dialogue.continuation_kind == "pending_answer"
    assert context.dialogue.pending_question == "你最在意的是哪一点？"
    assert context.dialogue.next_step == "check_current_answer"
    result = decision_from_answers(
        context, answers(action="ignore", reason="low_value_chatter", continuity=choice("answer"))
    )
    assert result.reason_code == "jev_dialogue_answer"


def test_soft_close_reopens_only_with_new_substance_and_semantic_confirmation():
    _, _, context = conversation(closed=True)
    assert context.dialogue.closed and context.dialogue.next_step == "evaluate_reopening"
    assert (
        decision_from_answers(context, answers(action="ignore", continuity=choice("reopen"))).reason_code
        == "jev_dialogue_reopen"
    )
    assert decision_from_answers(context, answers(action="ignore", continuity=choice("continue"))).action == "ignore"
    _, _, reaction = conversation("哈哈", closed=True)
    assert not reaction.dialogue.continuation_kind


@pytest.mark.parametrize(
    "change",
    [
        {"continuity": choice("end")},
        {"continuity": choice("unrelated")},
        {"continuity": choice("unclear")},
        {"continuity": choice("continue", 0.4)},
        {"recipient": choice("other")},
        {"recipient": choice("unclear")},
        {"reason": choice("boundary_or_sensitive")},
        {"reason": choice("other_recipient")},
        {"completion": choice("wait")},
        {"action": choice("invalid")},
        {"reason": choice("invalid")},
    ],
)
def test_vetoes_and_uncertainty_do_not_get_forced_reply(change):
    _, _, context = conversation()
    payload = answers(action="ignore", continuity=choice("continue"))
    payload.update(change)
    assert decision_from_answers(context, payload).action == "ignore"


@pytest.mark.parametrize(
    "case",
    ["mention_other", "quote_other", "stop", "expiry", "different_member", "reset", "new_topic", "explicit_ending"],
)
def test_local_candidate_keeps_identity_and_lifecycle_boundaries(case):
    runtime, current, context = conversation()
    if case == "mention_other":
        current.metadata["actual_mentions"] = ["bob"]
    elif case == "quote_other":
        other = runtime.dag.add_message("other", "bob", "你说呢？", timestamp=current.timestamp - 1)
        current.reply_to_id = other.msg_id
    elif case == "stop":
        runtime.topic_stop_revisions["alice"] = 1
    elif case == "expiry":
        current.timestamp = 2000
    elif case == "different_member":
        current.user_id = "bob"
    elif case == "reset":
        runtime.reset_conversation_state()
    elif case == "new_topic":
        current.text = "换个话题，今天的天气怎么样"
    elif case == "explicit_ending":
        current.text = "先不聊了"
    snapshot = capture_dialogue(runtime, current, frozenset(runtime.dag.nodes))
    assert snapshot is None or not snapshot.continuation_kind


def test_answered_question_and_current_correction_reach_followup_context():
    runtime, current, _ = conversation("不是速度问题，我更在意它是否稳定", question=True)
    bot = runtime.last_bot_node
    answer = route(
        runtime.dag.add_message("answer", "alice", "最在意稳定性", timestamp=bot.timestamp + 5, reply_to_id=bot.msg_id)
    )
    snapshot = capture_dialogue(runtime, current, frozenset(runtime.dag.nodes))
    assert not snapshot.question_pending
    assert snapshot.next_step == "respond_to_user_answer"
    assert (answer.msg_id, answer.text) in snapshot.user_updates


@pytest.mark.asyncio
@pytest.mark.parametrize("cooling", [False, True])
async def test_three_turns_resume_without_at_after_interleaved_chatter(jev_plugin, monkeypatch, cooling):
    p, bridge = jev_plugin
    clock_now, elapsed = p.time_service.time, [0]
    monkeypatch.setattr(p.time_service, "time", lambda: clock_now() + elapsed[0])
    route_original, observe_original = p._route_message, p._observe_routed_bot

    def route_current(runtime, node):
        result = route_original(runtime, node)
        if node.user_id == "alice":
            route(node, "topic", addressee_ids=[], bot_addressee_confidence=0)
        else:
            route(node, "other-topic", addressee_ids=["eve"], addressee_confidence=1)
        return result

    def observe(runtime, bot):
        observe_original(runtime, bot)
        route(bot, "topic")

    monkeypatch.setattr(p, "_route_message", route_current)
    monkeypatch.setattr(p, "_observe_routed_bot", observe)
    try:
        bridge.response = "你最在意的是哪一点？"
        first = MockEvent("我想聊聊这个问题", sender_id="alice", message_id="first", components=[At("bot_42")])
        await p.on_group_message(first)
        await flush(p, first)
        await drain(p)
        assert first.replies_sent
        runtime = p._sessions[first.unified_msg_origin]
        p.jev.payload = answers(action="ignore", recipient=choice("other"), reason="other_recipient")
        unrelated = MockEvent("我们讨论另一件事", sender_id="bob", message_id="unrelated")
        await p.on_group_message(unrelated)
        await flush(p, unrelated)
        await drain(p)
        assert not unrelated.replies_sent
        # Advance the shared clock while preserving source event chronology.
        elapsed[0] = 240
        if cooling:
            monkeypatch.setattr(p.arbiter, "is_in_deep_cooling", lambda *args, **kwargs: True)
        for index, text in enumerate(
            ["我最在意的是对方有没有认真听", "其实也不全是，我还想解释一下原因", "对，就是想把这件事聊明白"]
        ):
            p.jev.payload = answers(
                action="ignore",
                state="casual",
                reason="low_value_chatter",
                join={"type": "noul", "noul": 0.01},
                continuity=choice("answer" if index == 0 else "continue"),
            )
            bridge.response = "我听到了，你在意的是交流中有没有被理解。"
            current = MockEvent(text, sender_id="alice", message_id=f"followup-{index}")
            await p.on_group_message(current)
            await flush(p, current)
            await drain(p)
            assert current.replies_sent, runtime.model_diagnostic
            assert bridge.requests[-1][0]["response_plan"]["reason_code"].startswith("jev_dialogue_")
            assert "continuity" in p.jev.calls[-1]["questions"]
        assert len(bridge.requests) == 4
        assert len(runtime.ambient_openings) == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_delayed_answer_with_real_router_reaches_reply_without_at(jev_plugin, monkeypatch):
    p, bridge = jev_plugin
    clock_now, elapsed = p.time_service.time, [0]
    monkeypatch.setattr(p.time_service, "time", lambda: clock_now() + elapsed[0])
    try:
        bridge.response = "你最在意的是哪一点？"
        first = MockEvent("想和你聊聊", sender_id="alice", message_id="first", components=[At("bot_42")])
        await p.on_group_message(first)
        await flush(p, first)
        await drain(p)
        assert first.replies_sent
        elapsed[0] = 240
        p.jev.payload = answers(
            action="ignore",
            reason="low_value_chatter",
            continuity=choice("answer"),
            join={"type": "noul", "noul": 0.01},
        )
        second = MockEvent(
            "最在意的是能不能认真听我解释。因为之前沟通的时候有些误会，所以这次想把话说清楚。",
            sender_id="alice",
            message_id="answer",
        )
        bridge.response = "那我们把之前的误会说清楚。"
        await p.on_group_message(second)
        await flush(p, second)
        await drain(p)
        runtime = p._sessions[second.unified_msg_origin]
        assert second.replies_sent, runtime.model_diagnostic
        assert bridge.requests[-1][0]["response_plan"]["reason_code"] == "jev_dialogue_answer"
        assert p.jev.calls[-1]["state"]["conversation"]["dialogue"]["question_pending"]
    finally:
        await p.terminate()


def test_dialogue_priority_does_not_turn_into_media_or_sleep_authorization():
    gate = DynamicsDecisionGate()
    cfg, _ = parse_runtime_config({"daily_rhythm_enabled": False})
    gate.manners.evaluate = Mock(wraps=gate.manners.evaluate)
    gate.media.evaluate = Mock(wraps=gate.media.evaluate)
    gate.rhythm.evaluate = Mock(return_value=DailyRhythmVerdict(False, "asleep_ambient", "sleep"))
    result = gate.evaluate(
        session_id="room",
        user_id="alice",
        text="接着说刚才的事情",
        explicit=False,
        continuation=True,
        committed_reply=True,
        cfg=cfg,
    )
    assert gate.manners.evaluate.call_args.kwargs["explicit"] is True
    assert gate.media.evaluate.call_args.kwargs["explicit"] is False
    assert gate.rhythm.evaluate.call_args.kwargs["explicit"] is False
    assert not result.should_speak and result.reason_code == "asleep_ambient"


def test_peer_evidence_still_prevents_forcing_an_ambiguous_followup():
    _, _, context = conversation()
    context = replace(context, peer_continuation=SimpleNamespace(strong=True))
    result = decision_from_answers(context, answers(action="ignore", continuity=choice("continue")))
    assert result.action == "ignore"


def test_bare_reaction_does_not_force_an_extra_reply():
    _, _, context = conversation("哈哈")
    result = decision_from_answers(context, answers(action="ignore", continuity=choice("continue")))
    assert result.action == "ignore"
