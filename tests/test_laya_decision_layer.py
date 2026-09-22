"""The Laya decision layer runs inside the persona turn, and degrades locally.

`core/integrations/laya.py` speaks the same typed-decision contract as the Jev
transport and `core/jev_decision.py` maps the answers verbatim, so what is pinned
here is what differs: which client gets consulted, which reason prefix names the
decision in a trace, and which metric counts it.
"""

from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.jev_decision import (
    ACTIONS,
    REASONS,
    STATES,
    decision_from_answers,
)
from astrbot_plugin_chat_dynamics.core.turn_decision import TurnContext
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot
from .test_jev_decision_layer import JevDouble, answers
from .test_persona_model import BridgeDouble, drain, flush
from .test_plugin_lifecycle import MockEvent, _plugin


class LayaDouble(JevDouble):
    """The self-hosted transport, with the answers a test wants to pin."""


@pytest.fixture
def laya_plugin(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    p = _plugin({
        "decision_mode": "persona_model",
        "decision_backend": "laya",
        "base_thinking_delay": 0,
        "debounce_base_cooldown": 10,
        "daily_rhythm_enabled": False,
    })
    bridge = BridgeDouble()
    p.persona_engine.bridge = bridge
    p.decision_calls = []

    async def decide(**kwargs):
        p.decision_calls.append(kwargs)
        return SimpleNamespace(completion_text="{}")

    p.context.llm_generate = decide
    p.jev = JevDouble(None)
    p.laya = LayaDouble(answers())
    return p, bridge


def turn() -> TurnContext:
    return TurnContext(
        session_key="g/1", author="user_1", text="帮我看下这个报错",
        messages=(MessageSnapshot("m1", "user_1", "帮我看下这个报错"),),
        background=(), epoch=1, revision=1, started_at=0.0, explicit=True,
    )


def test_the_reason_prefix_names_the_layer_that_answered():
    """Both layers share one mapping, so the prefix is the only trace marker."""
    accepted = decision_from_answers(turn(), answers(), prefix="laya_")
    assert accepted.reason_code == "laya_addressed_question"
    assert accepted.action == "reply"

    declined = decision_from_answers(turn(), answers(join={"type": "noul", "noul": 0.2}), prefix="laya_")
    assert declined.reason_code == "laya_join_declined"
    assert declined.action == "ignore"

    weak = decision_from_answers(turn(), answers(), min_confidence=0.95, prefix="laya_")
    assert weak.reason_code == "laya_low_confidence"

    # The Jev prefix still stands when nothing is passed, which is what keeps the
    # existing Jev traces and their tests unchanged.
    assert decision_from_answers(turn(), answers()).reason_code == "jev_addressed_question"


def test_laya_configuration_is_parsed_and_clamped():
    config, warnings = parse_runtime_config({
        "decision_mode": "persona_model",
        "decision_backend": "laya",
        "laya_base_url": "http://192.168.1.4:8900",
        "laya_timeout": 2.5,
        "laya_min_confidence": 0.8,
    })
    assert config.decision_backend == "laya"
    assert config.laya_base_url == "http://192.168.1.4:8900"
    assert (config.laya_timeout, config.laya_min_confidence) == (2.5, 0.8)
    assert not [warning for warning in warnings if "laya" in warning]

    clamped, warnings = parse_runtime_config({
        "laya_timeout": 99,
        "laya_min_confidence": 0.01,
    })
    assert (clamped.laya_timeout, clamped.laya_min_confidence) == (1.5, 0.6)
    assert any("laya_timeout" in warning for warning in warnings)
    assert any("laya_min_confidence" in warning for warning in warnings)


def test_laya_stays_consulted_outside_the_persona_turn():
    """The legacy path asks it for the WTS sub-scores, so it is not idle there.

    A Jev backend answers the turn decision alone and really is persona-only; Laya
    answers whichever small decisions the active path raises, so warning that it
    would sit unused in legacy mode would be describing a plugin that no longer is.
    """
    laya, warnings = parse_runtime_config({"decision_mode": "legacy", "decision_backend": "laya"})
    assert laya.decision_backend == "laya"
    assert not [warning for warning in warnings if "decision_backend=laya" in warning]

    jev, warnings = parse_runtime_config({"decision_mode": "legacy", "decision_backend": "jev"})
    assert any("decision_backend=jev" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_laya_decides_the_turn_and_the_model_decision_is_not_asked(laya_plugin):
    p, bridge = laya_plugin
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)

        assert not p.decision_calls, "the model decision path must not run behind a Laya backend"
        assert p.jev.calls == [], "a Laya backend must not reach for the Jev transport"
        # The turn decision is one call of several: the message path also warms the
        # per-message completeness read. What matters is which one answered the turn.
        turn_calls = [call for call in p.laya.calls if "action" in call["questions"]]
        assert len(turn_calls) == 1
        call = turn_calls[0]
        assert set(call["questions"]) >= {"join", "action", "state", "length", "reason"}
        assert set(call["questions"]["action"]["criteria"]) == set(ACTIONS)
        assert set(call["questions"]["state"]["criteria"]) == set(STATES)
        assert set(call["questions"]["reason"]["criteria"]) == set(REASONS)
        assert call["timeout"] == p._runtime_config.laya_timeout

        plan = bridge.requests[0][0]["response_plan"]
        assert plan["action"] == "reply" and plan["length"] == "brief"
        assert plan["reason_code"] == "laya_addressed_question"
        assert p._metrics["laya_decision"] == 1 and p._metrics["laya_unavailable"] == 0
        assert p._metrics["jev_decision"] == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_a_silent_laya_answer_keeps_the_bot_out_of_the_thread(laya_plugin):
    p, bridge = laya_plugin
    p.laya.payload = answers(action="ignore", state="observing", reason="low_value_chatter")
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bridge.requests == []
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_an_unavailable_laya_falls_back_to_the_local_plan(laya_plugin):
    """None means "no decision", never a negative one: the local plan decides."""
    p, bridge = laya_plugin
    p.laya.payload = None
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert p._metrics["laya_unavailable"] == 1 and p._metrics["laya_decision"] == 0
        # The local conservative plan still answers an explicitly addressed turn.
        assert bridge.requests and bridge.requests[0][0]["response_plan"]["action"] == "reply"
        assert bridge.requests[0][0]["response_plan"]["reason_code"] == "laya_unavailable"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_low_confidence_is_refused_rather_than_approximated(laya_plugin):
    p, bridge = laya_plugin
    p.laya.payload = answers(action="reply")
    p.laya.payload["action"]["confidence"] = 0.2
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bridge.requests[0][0]["response_plan"]["reason_code"] == "laya_low_confidence"
    finally:
        await p.terminate()
