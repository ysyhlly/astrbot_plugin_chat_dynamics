from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.participation_policy import (
    ParticipationPolicy, ParticipationSnapshot, RecipientSnapshot)
from astrbot_plugin_chat_dynamics.core.turn_evidence import TurnEvidence
from astrbot_plugin_chat_dynamics.core.routing_trace import (
    build_routing_trace, compact_trace_inputs, finalize_decision_trace, trace_with_updates)
from astrbot_plugin_chat_dynamics.core.outcome_recorder import mark_delivered


def test_snapshot_detaches_nested_inputs_and_policy_repeats():
    ids = ["bot"]
    route = {"candidates": [[0.8, "old"]]}
    evidence = TurnEvidence.capture(session_id="s", message_id="m", epoch=3,
        config_id="c", policy_id="p", visible_before=20,
        participation=ParticipationSnapshot("bot", RecipientSnapshot(ids=ids, canonical=True, ambiguous=False)),
        policy=ParticipationPolicy(), routing=route)
    before = evidence.evaluate()
    ids.clear()
    route["candidates"][0][1] = "new"
    evidence.routing()["candidates"].clear()
    assert evidence.evaluate() == before
    assert evidence.participation.recipient.ids == ("bot",)
    assert evidence.routing()["candidates"][0][1] == "old"
    assert not evidence.is_current(session_id="s", message_id="m", epoch=4, config_id="c", policy_id="p")


def test_frozen_trace_survives_reroute_outcome_and_nested_mutation():
    routing = {"topic_id": "old", "topic_candidates": [[0.8, "old"]]}
    trace = finalize_decision_trace(build_routing_trace(routing=routing), should_reply=False, branch="hard_gate")
    persisted = compact_trace_inputs(trace)
    routing["topic_id"] = "new"
    trace["routing"]["topic_candidates"][0]["final_score"] = 0.1
    node = SimpleNamespace(metadata={"decision_trace": persisted})
    block = mark_delivered(node, now=2)
    block["final_outcome"] = "corrupted"
    assert "outcome" not in persisted
    assert node.metadata["outcome"]["final_outcome"] == "delivered"
    exported = trace_with_updates(persisted, outcome=node.metadata["outcome"])
    assert exported["routing"]["selected_topic"] == "old"
    assert exported["routing"]["topic_candidates"][0]["final_score"] == 0.8
    assert exported["participation"]["should_reply"] is False
    assert finalize_decision_trace(exported, should_reply=True)["participation"]["should_reply"] is False
    assert exported["decision_branch"] == "hard_gate"


def test_first_send_latency_is_terminal_and_does_not_change_decision(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import outcome_recorder
    monkeypatch.setattr(outcome_recorder.time, "perf_counter", lambda: 15.0)
    trace = {"participation": {"should_reply": True}}
    node = SimpleNamespace(metadata={"turn_latency": {"started": 10.0}, "decision_trace": trace})
    mark_delivered(node, now=2)
    assert node.metadata["turn_latency"]["first_send_seconds"] == 5.0
    monkeypatch.setattr(outcome_recorder.time, "perf_counter", lambda: 20.0)
    mark_delivered(node, now=3)
    assert node.metadata["turn_latency"]["first_send_seconds"] == 5.0
    assert trace == {"participation": {"should_reply": True}}
