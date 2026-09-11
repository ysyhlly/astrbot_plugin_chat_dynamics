import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG, ConversationNode
from astrbot_plugin_chat_dynamics.core.message_semantics import describe_message
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState, SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ParentRetriever, RoutingInference, ThreadRouter
from astrbot_plugin_chat_dynamics.core.topic_identity import node_topic_id
from astrbot_plugin_chat_dynamics.core.topic_resolution import TopicResolver


@pytest.mark.parametrize("metadata,expected", [
    ({}, ""),
    ({"topic_id": "legacy"}, "legacy"),
    ({"topic_id": "stale", "routing": {"topic_id": "current"}}, "current"),
    ({"topic_id": "stale", "routing": {"topic_id": ""}}, ""),
    ({"topic_id": "stale", "routing": {}}, ""),
    ({"topic_id": "stale", "routing": None}, ""),
    ({"topic_id": "stale", "routing": RoutingInference()}, ""),
])
def test_routing_empty_is_authoritative_and_thread_is_never_topic(metadata, expected):
    node = ConversationNode("n", "A", "message", 1, metadata=metadata, thread_id="explicit-thread")
    assert node_topic_id(node) == expected
    assert describe_message(node, ConversationDAG()).topic_id == expected


@pytest.mark.parametrize("metadata", [{}, {"topic_id": "stale", "routing": {"topic_id": "", "topic_status": "unformed"}}])
def test_quote_does_not_promote_thread_or_stale_mirror_to_topic(metadata):
    dag = ConversationDAG()
    parent = dag.add_message("parent", "A", "reference", timestamp=1, metadata=metadata)
    node = dag.add_message("child", "B", "这个呢", timestamp=2, reply_to_id="parent")
    topic, *_ = TopicResolver().resolve(node, dag, RoutingState(), {}, explicit_parent=parent)
    assert topic == "child"
    assert node.thread_id == parent.thread_id


@pytest.mark.parametrize("metadata,accepted", [
    ({}, False),
    ({"topic_id": "target", "routing": {"topic_id": ""}}, False),
    ({"topic_id": "target"}, True),
])
def test_parent_retrieval_requires_topic_identity(metadata, accepted):
    dag = ConversationDAG()
    parent = dag.add_message("parent", "A", "driver version?", timestamp=1, metadata=metadata)
    parent.thread_id = "target"
    node = dag.add_message("child", "B", "driver works correctly", timestamp=2)
    found, *_ = ParentRetriever().retrieve(node, dag, {"parent": 1.0}, "target")
    assert (found == "parent") is accepted


def test_addressivity_does_not_use_stale_mirror_for_active_topic_bonus():
    dag = ConversationDAG()
    trigger = dag.add_message("trigger", "A", "initial request", timestamp=1)
    bot = dag.add_message("bot-message", "bot", "acknowledged", timestamp=2, reply_to_id=trigger.msg_id,
                          metadata={"topic_id": "stale", "routing": {"topic_id": ""}})
    node = dag.add_message("current", "A", "new subject", timestamp=3,
                           metadata={"routing": {"topic_id": "stale"}})
    score = AddressivityRouter().compute_addressivity(node, dag, last_bot_node=bot)
    assert not any("Active interlocutor dialogue continuation bonus" in reason for reason in score.reasons)


def unformed_dialogue():
    rt = SessionRuntime("room", "group", "room", bot_id="bot", dag=ConversationDAG())
    router = ThreadRouter()
    trigger = rt.dag.add_message("trigger", "A", "bot帮我看看", timestamp=1, mentioned_users=["bot"])
    router.route(rt, trigger)
    bot = rt.dag.add_message("bot-message", "bot", "请提供相关信息", timestamp=2, reply_to_id=trigger.msg_id)
    router.observe_bot_message(rt, bot)
    return rt, router, bot


@pytest.mark.parametrize("author,text,addressed", [("A", "那怎么办", True), ("B", "那怎么办", False), ("A", "那家餐厅今天关门了", False)])
def test_unformed_continuation_keeps_parent_evidence_without_inventing_topic(author, text, addressed):
    rt, router, bot = unformed_dialogue()
    node = rt.dag.add_message("follow", author, text, timestamp=3)
    result = router.route(rt, node)
    assert result.bot_is_addressee is addressed
    assert result.topic_id == ""
    assert not rt.routing_state.topics
    if addressed:
        assert result.parent_message_id == bot.msg_id


def test_explicit_reply_to_unformed_bot_stays_addressed():
    rt, router, bot = unformed_dialogue()
    node = rt.dag.add_message("reply", "A", "这个呢", timestamp=3, reply_to_id=bot.msg_id)
    result = router.route(rt, node)
    assert result.bot_is_addressee
    assert not result.topic_id
    assert AddressivityRouter().compute_addressivity(node, rt.dag, last_bot_node=bot).is_bot_targeted


def test_two_empty_topics_do_not_retarget_unrelated_human_quote_to_bot():
    rt, router, bot = unformed_dialogue()
    unrelated = rt.dag.add_message("unrelated", "C", "different subject", timestamp=1.5)
    router.route(rt, unrelated)
    node = rt.dag.add_message("quote", "A", "这个呢", timestamp=3, reply_to_id=unrelated.msg_id)
    result = router.route(rt, node)
    assert not result.bot_is_addressee
    assert result.addressee_ids == ["C"]


def test_unformed_quoted_subject_continuation_uses_original_request_reference():
    rt = SessionRuntime("room", "group", "room", bot_id="bot", dag=ConversationDAG())
    router = ThreadRouter()
    reference = rt.dag.add_message("reference", "C", "reference proposal", timestamp=1)
    router.route(rt, reference)
    trigger = rt.dag.add_message("trigger", "A", "请解释", timestamp=2,
                                 reply_to_id=reference.msg_id, mentioned_users=["bot"])
    router.route(rt, trigger)
    bot = rt.dag.add_message("bot-message", "bot", "请提供相关信息", timestamp=3, reply_to_id=trigger.msg_id)
    router.observe_bot_message(rt, bot)
    follow = rt.dag.add_message("follow", "A", "这个呢", timestamp=4, reply_to_id=reference.msg_id)
    result = router.route(rt, follow)
    assert result.bot_is_addressee
    assert result.parent_message_id == reference.msg_id
    assert not result.topic_id
    assert not rt.routing_state.topics
