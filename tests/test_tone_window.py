"""Regressions for the real ingestion path, constrained evidence and provider selection."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core import member_boundary, tone_context
from astrbot_plugin_chat_dynamics.core.conversation_intent import build_intent_task, reply_interaction
from astrbot_plugin_chat_dynamics.core.jev_decision import (
    bound_request_with_optional,
    build_questions,
    build_state,
    request_size,
)
from astrbot_plugin_chat_dynamics.core.systemone import window
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import MockEvent, drain, flush, jev_plugin  # noqa: F401


@pytest.mark.asyncio
async def test_long_group_input_survives_actual_ingress_only_for_wide_decision(jev_plugin):  # noqa: F811
    plugin, _bridge = jev_plugin
    plugin.jev.context_window_tokens = 65536
    text = "小助手，请帮我读完：" + "甲乙丙丁 " * 10000 + " END-尾标"
    event = MockEvent(text, message_id="long-input")
    try:
        await plugin.on_group_message(event)
        await flush(plugin, event)
        await drain(plugin)
        assert len(plugin.jev.calls) == 1
        state = plugin.jev.calls[0]["state"]["conversation"]
        assert state["text"] == text
        assert state["messages"][-1]["text_spans"] == [[0, len(text)]]
        assert "expression_mode" in plugin.jev.calls[0]["questions"]
    finally:
        await plugin.terminate()


def _fixture():
    q = MessageSnapshot("q", "one", "你就是个废物。", mentioned_users=("bot",), timestamp=1)
    m = MessageSnapshot("m", "two", "这话我不认。", reply_to=q.message_id, timestamp=4)
    peer = MessageSnapshot("near", "three", "怎么又开始骂人了", timestamp=3)
    return TurnContext(
        "room",
        "two",
        m.text,
        (m,),
        (q,),
        0,
        0,
        0,
        True,
        bot_id="bot",
        snapshot_at=4,
        nearby_context=(peer,),
    )


def test_attributed_quote_reserved_under_pressure_and_rechecked(monkeypatch):
    turn = _fixture()
    state = build_state(turn)
    queries = build_questions(turn, narrative=True)
    rows = tone_context.tone_context(turn, None, None)
    assert rows[0]["message_id"] == "q" and rows[0]["author"] == "one" and rows[0]["mentions_bot"]
    packed, kept = bound_request_with_optional(
        state, queries, build_intent_task({}, {}, compact=True), tone_context=rows
    )
    assert {"expression_mode", "speaker_stance"} <= set(kept)
    assert packed["tone_exchange"]["preceding"] == rows
    assert request_size(packed, {**queries, **kept}) <= 20000
    frame = {
        "session_key": "room",
        "speaker_id": "two",
        "trigger_message_id": "m",
        "answers": {},
        "tone_sources": tone_context.source_signatures(rows),
    }
    assert tone_context.for_reply(turn, frame, None, None)[1] == rows
    assert tone_context.for_reply(replace(turn, session_key="other"), frame, None, None) == (None, [])
    monkeypatch.setattr(
        member_boundary,
        "filter_frames",
        lambda *a, **k: ([], member_boundary.BoundaryState(required=True)),
    )
    assert tone_context.for_reply(turn, frame, None, None) == (None, [])


def test_uncertain_accusation_not_projected_as_member_trait():
    turn = _fixture()
    frame = {
        "session_key": "room",
        "speaker_id": "two",
        "trigger_message_id": "m",
        "answers": {"speaker_stance": {"choice": "personal_attack", "confidence": 0.76, "type": "choice"}},
    }
    interaction = reply_interaction(turn, frame)
    assert interaction["speaker_stance"]["choice"] == "unclear"
    assert "speaker_stance" not in interaction.get("tentative_understanding", {})


def test_small_model_and_blocked_sources_do_not_recover_long_input():
    turn = _fixture()
    event = NS()
    small = NS(_runtime_config=NS(decision_backend="jev"), jev=NS(context_window_tokens=None))
    parsed = NS(text="Z" * 9000, message_id="m", sender_id="two")
    window.remember_input(small, event, parsed)
    assert not hasattr(event, "_chat_dynamics_wide_input")
    event._chat_dynamics_wide_input = ("m", "two", parsed.text)
    required = NS(_runtime_config=NS(autonomous_blocking_required=True))
    assert window.decision_turn(turn, (event,), required) is turn


def test_wide_history_keeps_only_selected_sources_with_currently_permitted_text(monkeypatch):
    from astrbot_plugin_chat_dynamics.core.context_retrieval import clip_text

    turn = _fixture()
    original = "很长的一段已选中的前文。" * 950
    message = replace(
        turn.background[0],
        text=clip_text(original, 180),
        source_text=original,
        semantics=NS(quoted_author_id=""),
    )
    turn = replace(turn, background=(message,))
    assert window.decision_turn(turn, (), None).background[0].text == original
    monkeypatch.setattr(
        member_boundary,
        "filter_frames",
        lambda h, e, frames, **kw: (
            [dict(frames[0], text="可见的一小段")],
            member_boundary.BoundaryState(available=True),
        ),
    )
    after = window.decision_turn(turn, (), None)
    assert [(m.message_id, m.text, m.source_text) for m in after.background] == [("q", "可见的一小段", None)]
    monkeypatch.setattr(
        member_boundary,
        "filter_frames",
        lambda *a, **kw: ([], member_boundary.BoundaryState(available=True)),
    )
    assert not window.decision_turn(turn, (), None).background
