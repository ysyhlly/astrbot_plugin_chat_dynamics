"""Learning snapshots retain source evidence while normal prompts stay bounded."""
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.persona_engine import snapshot_turn
from astrbot_plugin_chat_dynamics.core.jev_decision import build_learning_state
from astrbot_plugin_chat_dynamics.core.decision_tasks import turn_questions
from astrbot_plugin_chat_dynamics.core.decision_routing import build_routing_tasks
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext


def test_learning_retains_long_current_and_target_source_without_future_messages():
    dag = ConversationDAG("session")
    old = dag.add_message("old", "alice", "past " * 1500, timestamp=10.)
    node = dag.add_message("new", "alice", "current " * 1500, timestamp=20.,
                           reply_to_id="old", mentioned_users=["bot"])
    dag.add_message("future", "alice", "future evidence must be excluded", timestamp=30.)
    runtime = SimpleNamespace(dag=dag, session_key="session", bot_id="bot", epoch=0, user_revisions={})
    parsed = SimpleNamespace(message_id="new", sender_id="alice", reply_to_id="old", media_component_types=[])
    result = SimpleNamespace(user_id="alice", consolidated_text=node.text, raw_events=[parsed],
                             start_time=20., metadata={}, last_event=parsed)
    turn = snapshot_turn(runtime, result, ["new"], [parsed], True, {}, False).context
    _, candidates = turn_questions(turn)
    learning = build_learning_state(turn, candidates=candidates, persona_prompt="card " * 3000)
    current = learning["conversation"]["messages"][0]
    assert current["text"] == node.text
    assert current["reply_to"] == "old"
    assert current["timestamp"] == 20.
    assert current["mentioned_users"] == ("bot",)
    assert current["semantics"]["mentioned_user_ids"] == ("bot",)
    assert learning["target_candidates"]["target.1"]["text"] == old.text
    assert learning["conversation"]["text"] == node.text
    assert learning["conversation"]["truncated"] is False
    assert "future" not in turn.allowed_ids
    ordinary = turn.payload()
    assert "text" not in ordinary["messages"][0]
    assert "timestamp" not in ordinary["messages"][0]
    assert len(ordinary["text"]) == 8000
    assert len(ordinary["background"][0]["text"]) == 1200


def test_absent_fragment_not_reconstructed_from_combined_text():
    turn = TurnContext("session", "alice", "combined", (MessageSnapshot("new", "alice", ""),),
                       (), 0, 0, 20., False)
    state = build_learning_state(turn, candidates=["new"])
    assert state["target_candidates"]["target.0"]["text"] == ""
    assert state["target_candidates"]["target.0"]["text_missing"] is True


def test_recipient_candidates_have_closed_identity_and_past_full_evidence():
    dag = ConversationDAG("session")
    old = dag.add_message("old", "alice", "past " * 1500, timestamp=10., mentioned_users=["bob"])
    node = dag.add_message("new", "bob", "please explain", timestamp=20.)
    dag.add_message("future", "eve", "secret", timestamp=30.)
    runtime = SimpleNamespace(bot_id="bot", routing_state=RoutingState())
    state, questions, _ = build_routing_tasks(SimpleNamespace(node=node, dag=dag, runtime=runtime))
    assert "recipient.1" in questions
    candidate = state["recipient_candidates"]["recipient.1"]
    assert candidate["user_id"] == "alice"
    assert candidate["messages"][0]["text"] == old.text
    assert candidate["messages"][0]["timestamp"] == 10.
    assert candidate["messages"][0]["mentioned_users"] == ["bob"]
    assert state["recipient_candidates"]["recipient.0"]["is_bot"] is True
    assert all(m["message_id"] != "future" for m in state["recent_messages"])
