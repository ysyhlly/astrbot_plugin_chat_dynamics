from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG


def test_replay_trace_whitelist_redaction_and_copy():
    dag = ConversationDAG()
    node = dag.add_message("m", "alice", "private message", metadata={"routing": {"topic_id": "t"},
        "decision_trace": {"routing_schema_version": 2,
            "topic": {"topic_id": "t", "ambiguous": True},
            "recipient": {"ids": ["alice"], "bot_targeted": True, "ambiguous": False, "text": "secret"},
            "state": {"active_interlocutor": "alice", "intervening_users": ["bob"],
                      "last_bot_message_id": "bot-message"},
            "participation": {"level": "STRONG", "should_reply": None}, "payload": "secret"}})
    plugin = SimpleNamespace(dags={"room": dag}, console_show_message_content=False)
    hidden = replay_topic_blocks(plugin, [], "room")[0]["messages"][0]
    trace = hidden["decision_trace"]
    assert trace["recipient"]["ids"] == []
    assert trace["state"]["active_interlocutor"] is None
    assert trace["state"]["intervening_users"] == 1
    # A bot message id identifies the dialogue anchor, so it is redacted too.
    assert trace["state"]["last_bot_message_id"] is None
    assert trace["topic"]["ambiguous"] is True
    assert trace["recipient"]["ambiguous"] is False
    assert trace["participation"]["should_reply"] is None
    assert "private message" not in str(hidden) and "secret" not in str(hidden)
    plugin.console_show_message_content = True
    visible = replay_topic_blocks(plugin, [], "room")[0]["messages"][0]["decision_trace"]
    assert visible["recipient"]["ids"] == ["alice"]
    visible["recipient"]["ids"].append("mutation")
    assert node.metadata["decision_trace"]["recipient"]["ids"] == ["alice"]
