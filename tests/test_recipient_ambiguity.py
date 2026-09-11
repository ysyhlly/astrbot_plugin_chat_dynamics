"""Topic uncertainty must not change recipient decisions in either direction."""
import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.message_semantics import describe_message
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter


@pytest.mark.parametrize("recipient", ["bot", "human"])
@pytest.mark.parametrize("legacy", [False, True])
def test_topic_uncertainty_does_not_downgrade_recipient(recipient, legacy):
    dag = ConversationDAG()
    node = dag.add_message("m", "sender", "接着说", timestamp=1)
    routing = dict(addressee_ids=[recipient], addressee_confidence=.95,
                   bot_is_addressee=recipient == "bot",
                   bot_addressee_confidence=.95 if recipient == "bot" else 0,
                   topic_ambiguous=True, ambiguous=True)
    if not legacy:
        routing["addressee_ambiguous"] = False
    node.metadata["routing"] = routing
    result = AddressivityRouter(bot_id="bot").compute_addressivity(node, dag)
    assert result.is_bot_targeted == (recipient == "bot")
    assert result.level.value == ("strong" if recipient == "bot" else "weak")
    semantics = describe_message(node, dag, "bot")
    assert semantics.certainty == "probable"
    assert semantics.topic_ambiguous
    assert not semantics.addressee_ambiguous
    # Persona consumes the same attribution through immutable turn snapshots.
    from astrbot_plugin_chat_dynamics.core.persona_engine import turn_is_addressed
    from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=dag)
    turn = TurnContext("room", "sender", node.text,
        (MessageSnapshot("m", "sender", node.text, semantics=semantics),),
        (), 0, 0, 1, False)
    assert turn_is_addressed(runtime, turn, 1) == (recipient == "bot")
    payload = turn.payload()["messages"][0]["semantics"]
    assert payload["certainty"] == "probable"
    assert payload["topic_ambiguous"] and not payload["addressee_ambiguous"]


def test_real_router_serializes_independent_uncertainty():
    dag = ConversationDAG()
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=dag)
    node = dag.add_message("m", "sender", "嗯", timestamp=1, mentioned_users=["bot"])
    result = ThreadRouter().route(runtime, node)
    assert result.topic_ambiguous
    assert not result.addressee_ambiguous
    assert node.metadata["routing"]["addressee_ambiguous"] is False


def test_uncertain_recipient_remains_uncertain():
    dag = ConversationDAG()
    node = dag.add_message("m", "sender", "接着说", timestamp=1)
    node.metadata["routing"] = dict(addressee_ids=["bot"],
        addressee_confidence=.6, addressee_ambiguous=True,
        topic_ambiguous=False, bot_is_addressee=True, bot_addressee_confidence=.6)
    assert describe_message(node, dag, "bot").certainty == "possible"
