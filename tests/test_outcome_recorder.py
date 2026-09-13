"""Schema 3: the candidate set, the selection, and where the turn ended up.

Schema 2 could describe a routing decision but not its ending, so a send that
the gate suppressed and a reply the router never admitted were the same record.
These tests pin the two facts schema 3 adds, and the redaction rule that has to
keep up with them: the candidate set repeats every topic identifier, so a
"redacted" trace that only cleared the topic section would still carry them.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core import outcome_recorder as recorder
from astrbot_plugin_chat_dynamics.core.routing_trace import (
    build_routing_trace, redact_trace_identifiers,
)


def _node(metadata=None):
    return SimpleNamespace(metadata=dict(metadata or {}))


# ---- the candidate set --------------------------------------------------

def test_legacy_pairs_and_structured_rows_both_become_schema_3_rows():
    pairs = build_routing_trace(routing={
        "topic_id": "b", "topic_candidates": [[0.72, "b"], [0.68, "a"]]})
    rows = build_routing_trace(routing={
        "topic_id": "b",
        "topic_candidates": [{"topic_id": "a", "final_score": 0.68, "rank": 2},
                             {"topic_id": "b", "final_score": 0.72, "rank": 1}]})

    assert pairs["trace_schema_version"] == 3
    assert [row["topic_id"] for row in pairs["routing"]["topic_candidates"]] == ["b", "a"]
    assert [row["rank"] for row in pairs["routing"]["topic_candidates"]] == [1, 2]
    assert pairs["routing"] == rows["routing"]
    assert pairs["routing"]["selected_topic"] == "b"


def test_per_candidate_evidence_is_kept_for_every_candidate():
    """Only the winner breakdown used to survive, which cannot explain a loss."""
    trace = build_routing_trace(routing={
        "topic_id": "b",
        "topic_candidates": [[0.72, "b"], [0.68, "a"]],
        "topic_candidate_evidence": {"a": {"semantic": 0.74, "lexical": 0.43},
                                     "b": {"semantic": 0.81, "lexical": 0.40}},
    })
    rows = {row["topic_id"]: row for row in trace["routing"]["topic_candidates"]}

    assert rows["a"]["evidence"] == {"semantic": 0.74, "lexical": 0.43}
    assert rows["b"]["evidence"]["semantic"] == 0.81


def test_a_candidate_with_no_recorded_score_keeps_the_field_out():
    trace = build_routing_trace(routing={"topic_candidates": [{"topic_id": "a"}]})
    row = trace["routing"]["topic_candidates"][0]

    assert row["topic_id"] == "a"
    assert "final_score" not in row, "缺失的分数不能被读成一个决定性的 0"


def test_an_empty_candidate_list_is_recorded_as_empty_not_omitted():
    trace = build_routing_trace(routing={"topic_id": "", "topic_candidates": []})

    assert trace["routing"]["topic_candidates"] == []
    assert "routing" in trace


# ---- the outcome block --------------------------------------------------

def test_no_outcome_recorded_means_no_outcome_key():
    """An absent ending and a negative one are different claims."""
    trace = build_routing_trace(routing={})

    assert recorder.OUTCOME_KEY not in trace


def test_the_recorder_writes_the_outcome_onto_the_node_and_the_trace():
    node = _node({"decision_trace": {"trace_schema_version": 3}})
    recorder.mark_suppressed(node, "asleep_ambient")

    assert node.metadata["outcome"]["final_outcome"] == "suppressed"
    assert node.metadata["outcome"]["stage"] == "gate"
    assert node.metadata["decision_trace"]["outcome"]["suppression_reason"] == "asleep_ambient"


def test_the_outcome_is_re_attached_when_a_trace_is_rebuilt():
    node = _node()
    recorder.mark_delivered(node)
    rebuilt = build_routing_trace(routing={}, outcome=node.metadata.get("outcome"))

    assert rebuilt["outcome"]["final_outcome"] == "delivered"
    assert rebuilt["outcome"]["delivered"] is True


def test_delivery_is_terminal_but_a_failure_still_replaces_a_suppression():
    node = _node()
    recorder.mark_delivered(node)
    recorder.mark_generation_failed(node, "late")
    assert recorder.read_outcome(node)["final_outcome"] == "delivered"

    other = _node()
    recorder.mark_suppressed(other, "asleep_ambient")
    recorder.mark_generation_failed(other, "llm_timeout")
    assert recorder.read_outcome(other)["final_outcome"] == "generation_failed"


def test_the_default_outcome_is_a_recorded_non_attempt():
    node = _node()
    recorder.mark_not_attempted(node)
    block = recorder.read_outcome(node)

    assert block["final_outcome"] == "not_attempted"
    assert block["delivered"] is False
    assert block["stage"] == "admission"


def test_the_in_flight_marker_is_replaced_once_the_ending_is_known():
    node = _node()
    recorder.mark_in_flight(node)
    assert recorder.read_outcome(node)["final_outcome"] == "in_flight"
    recorder.mark_delivery_failed(node, "send_failed")
    assert recorder.read_outcome(node)["final_outcome"] == "delivery_failed"


def test_read_outcome_accepts_a_node_a_metadata_mapping_or_a_trace():
    node = _node({"decision_trace": {"trace_schema_version": 3}})
    recorder.mark_delivered(node)

    assert recorder.read_outcome(node)["final_outcome"] == "delivered"
    assert recorder.read_outcome(node.metadata)["final_outcome"] == "delivered"
    assert recorder.read_outcome(node.metadata["decision_trace"])["final_outcome"] == "delivered"
    assert recorder.read_outcome({}) == {}


def test_a_node_with_no_metadata_mapping_is_not_silently_written_to():
    """No mapping to write to is reported by the return value, not by a crash."""
    class Bare:
        pass

    block = recorder.mark_delivered(Bare())

    assert block["final_outcome"] == "delivered"


# ---- redaction ----------------------------------------------------------

def test_identifiers_inside_the_candidate_set_are_redacted_too():
    trace = build_routing_trace(routing={
        "topic_id": "secret-topic",
        "topic_candidates": [[0.9, "secret-topic"], [0.4, "other-secret"]],
        "topic_candidate_evidence": {"secret-topic": {"semantic": 0.9}},
    })
    redacted = redact_trace_identifiers(trace)

    assert "secret" not in json.dumps(redacted)
    assert trace["routing"]["selected_topic"] == "secret-topic", "原始轨迹不被就地修改"
