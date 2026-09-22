from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.decision_routing import build_routing_tasks, apply_routing_answers
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState, TopicState
from astrbot_plugin_chat_dynamics.core.routing_contract import topic_evidence_is_consistent


def turn():
    dag = ConversationDAG("session")
    old = dag.add_message("old", "alice", "Discuss the database schema", timestamp=10.)
    node = dag.add_message("new", "bob", "How should the schema work?", timestamp=20.)
    node.metadata["routing"] = {"topic_candidates": [[.5, "old"]], "topic_ambiguous": True,
                                "topic_status": "pending", "addressee_ambiguous": True,
                                "evidence": ["topic_ambiguous"]}
    state = RoutingState(topics={"old": TopicState("old", message_ids=[old.msg_id])})
    runtime = SimpleNamespace(dag=dag, bot_id="bot", routing_state=state)
    return SimpleNamespace(dag=dag, runtime=runtime, node=node)


def test_closed_candidates_keep_multiple_recipients_without_edges():
    t = turn()
    state, questions, mapping = build_routing_tasks(t)
    assert state["topic_candidates"]["topic_0"]["messages"]
    assert set(questions["topic"]["criteria"]) == {"KEEP", "topic_0"}
    answers = {key: {"type": "noul", "noul": 1.} for key in mapping["recipients"]}
    answers["topic"] = {"type": "choice", "choice": "topic_0", "confidence": 1.}
    assert apply_routing_answers(t, answers, mapping)
    routing = t.node.metadata["routing"]
    assert routing["addressee_ids"] == ["bot", "alice"]
    assert routing["topic_id"] == "old"
    assert "new" in t.runtime.routing_state.topics["old"].message_ids
    assert topic_evidence_is_consistent(routing)
    assert not t.node.parent_ids


def test_explicit_recipients_and_topic_are_not_offered():
    t = turn()
    t.node.mentioned_users = ["alice"]
    assert build_routing_tasks(t)[1] == {}


def test_changed_routing_rejects_snapshot():
    t = turn()
    _, _, mapping = build_routing_tasks(t)
    t.node.metadata["routing"]["topic_status"] = "committed"
    assert not apply_routing_answers(t, {"topic": {"type": "choice", "choice": "topic_0", "confidence": 1.}}, mapping)


def test_partial_and_uncertain_answers_do_not_erase_routing():
    t = turn()
    _, _, mapping = build_routing_tasks(t)
    assert not apply_routing_answers(t, {"recipient.0": {"type": "noul", "noul": 1.}}, mapping)
    answers = {key: {"type": "noul", "noul": .5} for key in mapping["recipients"]}
    answers["topic"] = {"type": "choice", "choice": "invented", "confidence": 1.}
    assert not apply_routing_answers(t, answers, mapping)


def test_reset_dag_rejects_even_equal_routing():
    t = turn()
    _, _, mapping = build_routing_tasks(t)
    t.runtime.dag = ConversationDAG("session")
    assert not apply_routing_answers(t, {"topic": {"type": "choice", "choice": "topic_0", "confidence": 1.}}, mapping)
