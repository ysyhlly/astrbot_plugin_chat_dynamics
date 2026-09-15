"""Stage attribution survives persona silence, gate denial and delivery."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.persona_trace import stage_trace
from astrbot_plugin_chat_dynamics.core.turn_decision import TurnDecision, TurnContext, PersonaSnapshot
from .test_persona_model import model_plugin as _model_plugin, flush, drain
from .test_plugin_lifecycle import MockEvent

model_plugin = _model_plugin


def test_stage_trace_preserves_proposal_and_does_not_store_persona_text():
    turn = TurnContext("group", "user", "private text", (), (), 1, 1, 0, True)
    proposed = TurnDecision("reply", "focused", (), "private goal", "normal", "request")
    final = replace(proposed, action="ignore", reason_code="asleep_ambient")
    gate = SimpleNamespace(should_speak=False, reason_code="asleep_ambient",
                           rhythm=SimpleNamespace(state="asleep_self", action=""))
    trace = stage_trace({"participation": {"level": "strong", "score": 1}},
                        persona=PersonaSnapshot("fp", "conv", "quiet", "private persona"),
                        interaction_state="observing", presence="sensible", turn=turn,
                        proposed=proposed, final=final, gate=gate)
    assert trace["decision_stages"]["persona"]["action"] == "reply"
    assert trace["decision_stages"]["gate"]["allowed"] is False
    assert trace["review_context"]["interaction_state"] == "observing"
    assert "private" not in json.dumps(trace)


@pytest.mark.asyncio
async def test_persona_success_records_delivery_and_constraints(model_plugin):
    p, bridge = model_plugin
    evaluate = p.decision_gate.evaluate
    def brief_wake(**kwargs):
        gate = evaluate(**kwargs)
        return replace(gate, rhythm=replace(gate.rhythm, state="brief_wake", action="wake_reply"),
                       length_hint="brief")
    p.decision_gate.evaluate = brief_wake
    event = MockEvent("帮我看这个问题", message_id="stage-success", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("stage-success")
        assert node.metadata["outcome"]["final_outcome"] == "delivered"
        trace = node.metadata["decision_trace"]
        assert trace["decision_stages"]["persona"]["action"] == "reply"
        assert trace["review_context"]["decision_mode"] == "persona_model"
        assert bridge.requests[0][0]["delivery_constraints"]["length_hint"] == "brief"
        assert bridge.requests[0][0]["delivery_constraints"]["rhythm_state"] == "brief_wake"
        assert bridge.requests[0][0]["delivery_constraints"]["rhythm_action"] == "wake_reply"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_persona_gate_failure_is_not_persona_silence(model_plugin):
    p, bridge = model_plugin
    def fail(**kwargs):
        raise RuntimeError("gate unavailable")
    p.decision_gate.evaluate = fail
    event = MockEvent("帮我看看", message_id="stage-gate", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("stage-gate")
        assert node.metadata["outcome"]["stage"] == "gate"
        stages = node.metadata["decision_trace"]["decision_stages"]
        assert stages["persona"]["action"] == "reply"
        assert stages["gate"]["allowed"] is False
        assert not bridge.requests
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected, stage", [
    ("silence", "suppressed", "persona"),
    ("empty", "generation_failed", "generation"),
])
async def test_persona_silence_and_empty_generation_are_different(model_plugin, mode, expected, stage):
    p, bridge = model_plugin
    if mode == "silence":
        async def ignore(item, persona, state):
            return TurnDecision("ignore", "observing", (), "", "brief", "persona_boundary")
        p.persona_engine.decide = ignore
    else:
        bridge.response = ""
    event = MockEvent("帮我看看", message_id="stage-negative", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("stage-negative")
        assert node.metadata["outcome"]["final_outcome"] == expected
        assert node.metadata["outcome"]["stage"] == stage
        assert not event.replies_sent
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_succeeds", [False, True])
async def test_send_failure_keeps_any_success_terminal(model_plugin, first_succeeds):
    p, bridge = model_plugin
    bridge.response = "甲" * 1000 + "\n\n" + "乙" * 1000
    p.time_service.sleep = lambda seconds: asyncio.sleep(0)
    attempts = []
    async def deliver(runtime, event, fragment, **kwargs):
        attempts.append(fragment)
        return SimpleNamespace(success=first_succeeds and len(attempts) == 1, message_id="delivered")
    p._send_owned = deliver
    event = MockEvent("帮我解释", message_id="send-outcome", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("send-outcome")
        assert node.metadata["outcome"]["final_outcome"] == ("delivered" if first_succeeds else "delivery_failed")
        assert len(attempts) == (2 if first_succeeds else 1)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_invalid_model_fallback_is_technical_not_persona_silence(model_plugin):
    p, bridge = model_plugin
    async def invalid(**kwargs):
        return SimpleNamespace(completion_text="not json")
    p.context.llm_generate = invalid
    event = MockEvent("今天晚上吃什么", message_id="fallback-outcome", is_at_or_wake_command=False)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("fallback-outcome")
        assert node.metadata["outcome"]["final_outcome"] == "generation_failed"
        assert node.metadata["outcome"]["suppression_reason"] == "decision_invalid_or_failed"
        assert not bridge.requests
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_persona_change_after_generation_closes_in_flight(model_plugin):
    p, bridge = model_plugin
    async def change():
        bridge.persona = replace(bridge.persona, fingerprint="changed", prompt="changed persona")
    bridge.before_reply = change
    event = MockEvent("帮我看看", message_id="changed-outcome", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("changed-outcome")
        assert node.metadata["outcome"]["final_outcome"] == "suppressed"
        assert node.metadata["outcome"]["suppression_reason"] == "persona_changed"
        assert not event.replies_sent
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", [True, False])
async def test_late_failure_does_not_write_replacement_node(model_plugin, invalidate):
    p, bridge = model_plugin
    event = MockEvent("帮我看看", message_id="reused", is_at_or_wake_command=True)
    async def replace_node_then_fail():
        runtime = p._sessions[event.unified_msg_origin]
        if invalidate:
            runtime.epoch += 1
        runtime.dag.nodes.pop("reused")
        runtime.dag.add_message(msg_id="reused", user_id="new-user", text="new turn", timestamp=0,
                                metadata={"replacement": True})
        raise RuntimeError("old attempt failed")
    bridge.before_reply = replace_node_then_fail
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("reused")
        assert "outcome" not in node.metadata
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_shadow_does_not_rewrite_real_decision_trace(model_plugin):
    p, bridge = model_plugin
    p.config["shadow_mode"] = True
    p._sync_runtime_from_config()
    event = MockEvent("帮我看看", message_id="shadow-stage", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node("shadow-stage")
        assert "decision_stages" not in node.metadata["decision_trace"]
        assert "review_context" not in node.metadata["decision_trace"]
        assert not bridge.requests
    finally:
        await p.terminate()
