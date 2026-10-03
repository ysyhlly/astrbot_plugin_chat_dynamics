"""The Jev decision layer runs inside the persona turn, and degrades locally."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.jev_decision import ACTIONS, REASONS, STATES
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_persona_model import BridgeDouble, drain, flush
from .test_plugin_lifecycle import MockEvent, _plugin


def answers(action="reply", state="focused", length="brief", reason="addressed_question", **extra):
    payload = {
        "completion": {"type": "choice", "choice": "complete", "confidence": 0.9},
        "join": {"type": "noul", "noul": 0.85},
        "recipient": {"type": "choice", "choice": "bot", "confidence": 0.85},
        "action": {"type": "choice", "choice": action, "confidence": 0.9, "probabilities": {}},
        "state": {"type": "choice", "choice": state, "confidence": 0.8, "probabilities": {}},
        "length": {"type": "choice", "choice": length, "confidence": 0.75, "probabilities": {}},
        "reason": {"type": "choice", "choice": reason, "confidence": 0.85, "probabilities": {}},
    }
    payload.update(extra)
    return payload


class JevDouble:
    """The decision layer's transport, with the answers a test wants to pin."""

    def __init__(self, payload=None, delay=0.0):
        self.payload = payload
        self.delay = delay
        self.calls = []
        self.configuration = {}

    def configure(self, **kwargs):
        """Stand in for the real client's reconfiguration path."""
        self.configuration = dict(kwargs)

    async def evaluate(self, *, state, questions, timeout=None):
        self.calls.append({"state": state, "questions": questions, "timeout": timeout})
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload

    def snapshot(self):
        return {"configured": True, "available": True, "status": "available", "detail": "ready",
                "error_code": "", "model": "jev-latest", "endpoint_host": "example.invalid",
                "calls": len(self.calls), "failures": 0, "served_model": "", "request_id": ""}


@pytest.fixture
def jev_plugin(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    p = _plugin({
        "decision_mode": "persona_model",
        "decision_backend": "jev",
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
    p.jev = JevDouble(answers())
    return p, bridge


@pytest.mark.asyncio
async def test_jev_decides_the_turn_and_the_model_decision_is_not_asked(jev_plugin):
    p, bridge = jev_plugin
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)

        assert not p.decision_calls, "the model decision path must not run behind a Jev backend"
        assert len(p.jev.calls) == 1
        call = p.jev.calls[0]
        assert set(call["questions"]) >= {"join", "action", "state", "length", "reason"}
        assert set(call["questions"]["action"]["criteria"]) == set(ACTIONS)
        assert set(call["questions"]["state"]["criteria"]) == set(STATES)
        assert set(call["questions"]["reason"]["criteria"]) == set(REASONS)
        assert call["timeout"] == p._runtime_config.jev_timeout
        assert call["state"]["persona"] == bridge.persona.prompt
        conversation = call["state"]["conversation"]
        assert [message["message_id"] for message in conversation["messages"]] == ["m1"]
        assert conversation["messages"][0]["author"] == "user_1"
        # Current text appears once, at the top of the conversation, exactly as the
        # model path receives it; fragments carry provenance rather than a copy.
        assert conversation["text"] == "帮我看下这个报错"
        assert "text" not in conversation["messages"][0]

        plan = bridge.requests[0][0]["response_plan"]
        assert plan["action"] == "reply" and plan["length"] == "brief"
        assert plan["reason_code"] == "jev_wake_addressed"
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.model_diagnostic["backend"] == "jev"
        assert runtime.jev_decision["action"] == {"type": "choice", "choice": "reply", "confidence": 0.9}
        assert p._metrics["jev_decision"] == 1 and p._metrics["jev_unavailable"] == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_jev_can_reject_a_soft_wake_aimed_at_someone_else(jev_plugin):
    p, bridge = jev_plugin
    p.jev.payload = answers(action="ignore", state="observing", reason="other_recipient",
                            recipient={"type": "choice", "choice": "other", "confidence": 0.9})
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bridge.requests == []
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.interaction_state == "observing"
        assert runtime.model_diagnostic["reason_code"] == "jev_wake_other"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_an_unavailable_decision_layer_does_not_admit_a_soft_wake(jev_plugin):
    p, bridge = jev_plugin
    p.jev.payload = None
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not p.decision_calls
        assert not bridge.requests and not event.replies_sent
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.model_diagnostic["reason_code"] == "jev_unavailable"
        assert p._metrics["jev_unavailable"] == 1 and p._metrics["jev_decision"] == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_soft_wake_depends_on_recipient_not_action_confidence(jev_plugin):
    p, bridge = jev_plugin
    weak = answers()
    weak.update(action={"type": "choice", "choice": "clarify", "confidence": 0.2},
                state={"type": "choice", "choice": "playful", "confidence": 0.2},
                length={"type": "choice", "choice": "detailed", "confidence": 0.2},
                reason={"type": "choice", "choice": "boundary_or_sensitive", "confidence": 0.2})
    p.jev.payload = weak
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        plan = bridge.requests[0][0]["response_plan"]
        assert (plan["action"], plan["state"], plan["length"], plan["reason_code"]) == (
            "reply", "focused", "brief", "jev_wake_addressed")
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_ambient_decision_defaults_uncertain_style_and_records_action_confidence(jev_plugin):
    p, bridge = jev_plugin
    p.jev.payload = answers(state="playful", length="detailed")
    for key in ("state", "length", "reason"):
        p.jev.payload[key]["confidence"] = 0.2
    try:
        event = MockEvent("有人知道这个报错怎么处理吗", message_id="m1")
        runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.group_id,
                                         umo=event.unified_msg_origin, bot_id=event.self_id)
        turn = TurnContext(runtime.session_key, event.sender_id, event.message_str,
                           (MessageSnapshot("m1", event.sender_id, ""),), (), 0, 0, 0.0, False)
        item = ModelTurn(turn, (), {}, False)
        decision = await p.persona_engine.decide(item, bridge.persona, "observing", runtime=runtime)
        assert (decision.action, decision.state, decision.length, decision.reason_code) == (
            "reply", "focused", "brief", "jev_action_accepted")
        assert runtime.jev_decision["confidence"] == pytest.approx(0.9)
        assert runtime.jev_decision["length"]["confidence"] == pytest.approx(0.2)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_ambient_silence_is_decided_by_jev_then_confirmed_by_the_gate(jev_plugin):
    p, bridge = jev_plugin
    p.jev.payload = answers(action="ignore", state="observing", reason="other_recipient")
    try:
        event = MockEvent("他们俩聊得挺开心", message_id="m1")
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(p.jev.calls) == 1 and bridge.requests == []
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_the_turn_deadline_still_bounds_a_slow_decision_layer(jev_plugin):
    p, bridge = jev_plugin
    p.jev.delay = 0.4
    p._runtime_config = replace(p._runtime_config, decision_timeout=0.02)
    try:
        event = MockEvent("帮我看下这个报错", message_id="m1", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not bridge.requests and not event.replies_sent
        assert p._sessions[event.unified_msg_origin].model_diagnostic["reason_code"] == "decision_timeout"
    finally:
        await p.terminate()












@pytest.mark.asyncio
async def test_a_decision_layer_never_answers_a_stale_turn(jev_plugin):
    """The evidence and the plan both belong to the turn that asked for them."""
    p, bridge = jev_plugin
    try:
        turn = TurnContext("room", "user", "help", (MessageSnapshot("m", "user", "help"),), (),
                           0, 0, 0.0, True)
        item = ModelTurn(turn, (), {}, False)
        decision = await p.persona_engine.decide(item, bridge.persona, "observing",
                                                 runtime=p._sessions.get("room"))
        assert decision.action == "reply"
        assert decision.target_message_ids == ("m",)
    finally:
        await p.terminate()
