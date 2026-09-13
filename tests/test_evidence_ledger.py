"""Evidence diagnostics stay numeric, immutable and independent of decisions."""
import json
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.evidence import sanitize_ledger, routing_ledger
from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace, redact_trace_identifiers
from astrbot_plugin_chat_dynamics.core.dashboard import _replay_decision_trace
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState
from astrbot_plugin_chat_dynamics.core.topic_resolution import TopicResolver


def test_ledger_allowlist_and_nonfinite():
    entries = [dict(domain="topic", code="centroid", source="topic_resolver", raw_value=.7,
                    contribution=.21, text="private text", user_id="private user"),
               dict(domain="topic", code="private text", source="routing", raw_value=1),
               dict(domain="parent", code="semantic", source="routing", raw_value=float("nan")),
               dict(domain="parent", code="semantic", source="routing", raw_value=.5, contribution=float("inf"))]
    snapshot = sanitize_ledger({"entries": entries})
    assert len(snapshot["entries"]) == 2
    assert snapshot["entries"][1]["contribution"] is None
    assert "private" not in json.dumps(snapshot, allow_nan=False)
    entries[0]["raw_value"] = 99
    assert snapshot["entries"][0]["raw_value"] == .7
    assert snapshot["calibrated"] is False


def test_parent_trace_and_redaction_copy():
    routing = dict(parent_message_id="private-parent", parent_confidence=.8,
                   parent_margin=.1, parent_ambiguous=False,
                   parent_candidates=[(.8, "private-parent"), (float("inf"), "bad")],
                   topic_id="private-topic", addressee_ids=["private-user"])
    trace = build_routing_trace(routing=routing)
    assert trace["parent"]["candidates"] == [[.8, "private-parent"]]
    redacted = redact_trace_identifiers(trace)
    assert "private" not in json.dumps(redacted)
    assert trace["parent"]["message_id"] == "private-parent"
    assert trace["routing_schema_version"] == 2


def test_rerank_ledger_supersedes_old_factor_and_replay_topic():
    old = dict(domain="topic", code="centroid", source="topic_resolver", raw_value=.5, contribution=.15)
    routing = dict(topic_id="new", topic_confidence=.78, evidence=["topic_llm_rerank"], ledger={"entries": [old]})
    node = SimpleNamespace(metadata={"routing": routing, "decision_trace": {"topic": {"topic_id": "old", "confidence": .5}}})
    trace = _replay_decision_trace(node, True)
    assert trace["topic"]["topic_id"] == "new"
    assert not any(e["code"] == "centroid" for e in trace["ledger"]["entries"])
    assert any(e["code"] == "topic_llm_rerank" for e in trace["ledger"]["entries"])
    assert routing_ledger({**routing, "ledger": routing_ledger(routing)}) == routing_ledger(routing)


def test_topic_breakdown_preserves_exact_score_and_is_bounded():
    dag = ConversationDAG()
    state = RoutingState()
    resolver = TopicResolver()
    first = dag.add_message("a", "A", "database connection timeout configuration", timestamp=1)
    resolver.remember(state, first, "a", dag)
    node = dag.add_message("b", "B", "database connection timeout", timestamp=2)
    details = []
    score = resolver.score_topic(node, dag, state.topics["a"], {"a": .8}, breakdown=details)
    assert score == resolver.score_topic(node, dag, state.topics["a"], {"a": .8})
    assert len(details) == 7
    assert score == round(sum(e["contribution"] for e in details), 4)
    resolver.resolve(node, dag, state, {"a": .8})
    assert len(node.metadata["_topic_score_evidence"][2]) == 7


def test_parent_candidate_evidence_does_not_claim_selected_parent_or_recipient():
    routing = dict(parent_message_id="", parent_confidence=0.0, parent_ambiguous=True,
                   parent_candidates=[(.79, "candidate")], addressee_confidence=.2,
                   ledger={"entries": [dict(domain="parent", code="semantic",
                                            source="parent_retriever", raw_value=.9, contribution=.342)]})
    trace = build_routing_trace(routing=routing)
    assert trace["parent"]["message_id"] == ""
    assert trace["parent"]["ambiguous"] is True
    selected = {e["domain"]: e["raw_value"] for e in trace["ledger"]["entries"] if e["code"] == "selected_score"}
    assert selected == {"topic": 0.0, "parent": 0.0, "recipient": .2}
    assert trace["ledger"]["entries"][0]["domain"] == "parent"
    assert trace["ledger"]["entries"][0]["source"] == "parent_retriever"


def test_unknown_parent_fields_are_not_exported():
    trace = build_routing_trace(routing={"parent_text": "secret", "payload": {"raw": "secret"},
                                       "parent_candidates": [{"text": "secret"}, (.8, {"text": "secret"})],
                                       "ledger": {"entries": [{"domain": "topic", "code": "centroid",
                                          "source": "raw secret", "raw_value": 1}]}})
    assert "secret" not in json.dumps(trace)
    assert trace["parent"]["candidates"] == []
