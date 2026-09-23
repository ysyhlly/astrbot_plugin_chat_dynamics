"""Runtime E is measured from actual conversation state, never inferred by text."""

from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.arbiter import InterventionArbiter
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.learning_environment import capture_environment
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot


def test_environment_uses_real_message_and_cooling_state():
    dag = ConversationDAG()
    dag.add_message("old", "user-a", "earlier", timestamp=500)
    last_bot = dag.add_message("bot-1", "bot", "answer", timestamp=950)
    dag.add_message("user-1", "user-a", "question", timestamp=978,
                    reply_to_id="bot-1", mentioned_users=["bot"])
    dag.add_message("user-2", "user-b", "follow up", timestamp=980)
    runtime = SimpleNamespace(bot_id="bot", session_key="room", dag=dag,
                              last_bot_node=last_bot)
    turn = SimpleNamespace(messages=(MessageSnapshot("user-1", "user-a", "question",
                                                     "bot-1", mentioned_users=("bot",)),
                                     MessageSnapshot("user-2", "user-b", "follow up")))
    arbiter = InterventionArbiter()
    arbiter.trigger_cooling("room", duration_seconds=30, current_time=980)
    env = capture_environment(runtime, turn, arbiter, now=980)
    assert env == {
        "schema_version": 1, "mentioned_self": True, "reply_to_self": True,
        "seconds_since_last_bot_message": 30.0,
        "consecutive_bot_messages_before_decision": 1,
        "bot_messages_last_5m": 1, "active_users_last_5m": 2,
        "room_messages_last_5m": 3, "cooldown_remaining_seconds": 30.0,
    }
    assert "user-a" not in str(env) and "bot-1" not in str(env)


def test_environment_marks_missing_sources_as_unknown():
    runtime = SimpleNamespace(bot_id="", session_key="room", dag=None,
                              last_bot_node=None)
    turn = SimpleNamespace(messages=(MessageSnapshot("m", "u", "hi", "missing"),))
    env = capture_environment(runtime, turn, None, now=10)
    assert env["mentioned_self"] is None
    assert env["reply_to_self"] is None
    assert env["seconds_since_last_bot_message"] is None
    assert env["active_users_last_5m"] is None
    assert env["room_messages_last_5m"] is None
    assert env["cooldown_remaining_seconds"] is None


def test_full_dag_window_does_not_report_partial_counts_as_exact():
    dag = ConversationDAG(max_nodes=2)
    dag.add_message("a", "u1", "one", timestamp=100)
    dag.add_message("b", "u2", "two", timestamp=101)
    runtime = SimpleNamespace(bot_id="bot", session_key="room", dag=dag,
                              last_bot_node=None)
    turn = SimpleNamespace(messages=(MessageSnapshot("b", "u2", "two"),))
    env = capture_environment(runtime, turn, None, now=102)
    assert env["active_users_last_5m"] is None
    assert env["room_messages_last_5m"] is None


def test_evicted_quote_uses_platform_author_evidence():
    runtime = SimpleNamespace(bot_id="bot", session_key="room",
                              dag=ConversationDAG(), last_bot_node=None)
    message = SimpleNamespace(message_id="m", reply_to="", mentioned_users=(),
                              semantics=SimpleNamespace(quoted_message_id="gone",
                                                        quoted_author_id="bot"))
    env = capture_environment(runtime, SimpleNamespace(messages=(message,)), None, now=100)
    assert env["reply_to_self"] is True
