"""Learning snapshots retain source evidence while normal prompts stay bounded."""
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.decision_routing import build_routing_tasks
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState






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
