"""Formation backfill preserves fragment evidence and coherent ambiguity."""
import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter, TopicResolver


def setup(router=None):
    return SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG()), router or ThreadRouter()


def add(rt, mid, user, timestamp, turn, text, **kwargs):
    return rt.dag.add_message(mid, user, text, timestamp=timestamp,
        metadata={"turn_id": turn, "topic_source_text": text}, **kwargs)


def seed(rt, router, times):
    for i, timestamp in enumerate(times):
        node = add(rt, f"h{i}", "B" if i % 2 == 0 else "C", timestamp,
                   f"history-{i}", f"显卡风扇温度调节方案第{i}个配置")
        router.route(rt, node)


@pytest.mark.parametrize("explicit", [True, False])
def test_burst_backfills_final_metadata_without_overwriting_fragment_addressivity(explicit):
    rt, router = setup()
    seed(rt, router, [98, 99, 105])
    first = add(rt, "f0", "A", 100, "batch-A", "显卡风扇温度调节方案需要检查",
                reply_to_id="h0" if explicit else None,
                mentioned_users=["bot"] if explicit else [])
    last = add(rt, "f1", "A", 110, "batch-A", "显卡风扇温度调节方案需要改进")
    rt.dag.link_related(last.msg_id, first.msg_id)
    old_return = router.route(rt, first)
    assert old_return.topic_status == "unformed"
    router.route(rt, last)
    routing = first.metadata["routing"]
    assert routing["topic_status"] == "committed"
    assert routing["topic_id"] == last.metadata["routing"]["topic_id"]
    assert not routing["topic_ambiguous"]
    assert routing["ambiguous"] == (routing["addressee_confidence"] < .72)
    assert routing["ambiguous"] is not explicit
    assert routing["explicit_reply"] is explicit
    assert routing["explicit_mention"] is explicit
    if explicit:
        assert routing["parent_message_id"] == "h0"
        assert first.edge_kinds["h0"] == "reply"
    assert "topic_burst_confirmed" in routing["evidence"]
    assert not rt.routing_state.pending_assignments
    refreshed = router.route(rt, first)
    assert refreshed.topic_status == "committed"
    assert refreshed.ambiguous == routing["ambiguous"]


def test_four_fragments_do_not_turn_three_real_turns_into_four():
    rt, router = setup()
    seed(rt, router, [98, 99])
    fragments = [add(rt, f"f{i}", "A", 100+i, "one-turn",
        f"显卡风扇温度调节方案检查第{i}个位置") for i in range(4)]
    for node in fragments:
        router.route(rt, node)
    assert not rt.routing_state.topics
    assert all(n.metadata["routing"]["topic_status"] == "unformed" for n in fragments)


class PendingResolver(TopicResolver):
    def resolve(self, node, dag, state, matches, explicit_parent=None):
        if node.msg_id == "pending":
            return "a", .55, True, ["topic_ambiguous"], "b", [(.55, "a"), (.53, "b")]
        return ("b" if node.msg_id == "follow" else node.msg_id), .9, False, [], None, []


@pytest.mark.parametrize("explicit", [True, False])
def test_pending_followup_recomputes_ambiguity_from_preserved_addressee(explicit):
    rt, router = setup(ThreadRouter(topic_resolver=PendingResolver(), require_intense_dialogue=False))
    for i, mid in enumerate(("a", "b", "pending")):
        text = ("显卡风扇温度检查", "今晚烧烤菜单安排", "明天会议需要准备报告")[i]
        node = add(rt, mid, mid, i+1, mid, text,
                   mentioned_users=["bot"] if mid == "pending" and explicit else [])
        router.route(rt, node)
    prior = rt.dag.nodes["pending"]
    assert prior.metadata["routing"]["topic_status"] == "pending"
    assert prior.metadata["routing"]["ambiguous"]
    before_conf = prior.metadata["routing"]["addressee_confidence"]
    follow = add(rt, "follow", "other", 4, "follow", "explicit independent content", reply_to_id="pending")
    router.route(rt, follow)
    routing = prior.metadata["routing"]
    assert routing["topic_status"] == "committed"
    assert routing["topic_id"] == "b"
    assert "pending_followup" in routing["evidence"]
    assert not routing["topic_ambiguous"]
    assert routing["addressee_confidence"] == before_conf
    assert routing["ambiguous"] == (before_conf < .72)
    assert routing["ambiguous"] is not explicit
    assert routing["explicit_mention"] is explicit
