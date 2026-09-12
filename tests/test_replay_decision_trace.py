from types import SimpleNamespace
import pytest

from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG


@pytest.mark.asyncio
async def test_replay_500_messages_and_live_limit_preserve_stored_nodes():
    from .test_plugin_lifecycle import _plugin
    from astrbot_plugin_chat_dynamics.core.dashboard import scene_replay_snapshot

    plugin = _plugin()
    try:
        now = plugin.time_service.time()
        runtime = plugin._get_or_create_runtime("room-a", group_id="a", umo="room-a")
        other = plugin._get_or_create_runtime("room-b", group_id="b", umo="room-b")
        for i in range(500):
            runtime.dag.add_message(str(i), "user", "hidden text", timestamp=now - 500 + i,
                metadata={"routing": {"topic_id": "topic" if i % 2 else "UNKNOWN"}})
        other.dag.add_message("other", "other-user", "other room", timestamp=now,
                              metadata={"routing": {"topic_id": "other-topic"}})
        data = scene_replay_snapshot(plugin, session_key="room-a")
        assert data["message_limit"] == data["retained_message_count"] == 500
        assert data["unassigned_message_count"] == 250
        assert data["topic_blocks"][0]["message_count"] == 250
        assert data["topic_blocks"][0]["messages"][0]["msg_id"] == "1"
        assert "hidden text" not in str(data) and "other-topic" not in str(data)

        panel = await plugin.save_config_values({"replay_message_limit": 80})
        assert panel["effective"]["replay_message_limit"] == 80
        smaller = scene_replay_snapshot(plugin, session_key="room-a")
        assert smaller["retained_message_count"] == 80
        assert smaller["unassigned_message_count"] == 40
        assert smaller["topic_blocks"][0]["message_count"] == 40
        assert len(runtime.dag.nodes) == 500
        await plugin.save_config_values({"replay_message_limit": 500})
        assert scene_replay_snapshot(plugin, session_key="room-a")["retained_message_count"] == 500
    finally:
        await plugin.terminate()


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
