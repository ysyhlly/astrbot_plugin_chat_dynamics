from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter, TopicResolver


class Resolver(TopicResolver):
    def resolve(self, node, dag, state, matches, explicit_parent=None):
        if node.msg_id == "pending":
            return "a", .55, True, ["topic_ambiguous"], "b", [(.55, "a"), (.53, "b")]
        return ("b" if node.msg_id == "follow" else node.msg_id), .9, False, [], None, []


def setup():
    runtime = SessionRuntime("one", "g", "one", dag=ConversationDAG())
    router = ThreadRouter(topic_resolver=Resolver())
    for mid in ("a", "b", "pending"):
        node = runtime.dag.add_message(mid, mid, "substantive topic " + mid, timestamp=len(runtime.dag.nodes)+1)
        router.route(runtime, node)
    return runtime, router


def test_pending_never_pollutes_and_direct_followup_backfills():
    rt, router = setup()
    assert rt.dag.nodes["pending"].metadata["routing"]["topic_id"] == ""
    assert all("pending" not in t.message_ids for t in rt.routing_state.topics.values())
    node = rt.dag.add_message("follow", "other", "explicit independent content", timestamp=4, reply_to_id="pending")
    router.route(rt, node)
    assert "pending" in rt.routing_state.topics["b"].message_ids
    assert not rt.routing_state.pending_assignments
    assert rt.dag.nodes["pending"].metadata["routing"]["topic_id"] == "b"


def test_three_unrelated_messages_expire_to_unknown_and_reset_clears():
    rt, router = setup()
    for i in range(3):
        node = rt.dag.add_message(str(i), "other", "unrelated subject", timestamp=4+i)
        router.route(rt, node)
    assert not rt.routing_state.pending_assignments
    assert rt.dag.nodes["pending"].metadata["routing"]["topic_status"] == "unknown"
    assert all("pending" not in t.message_ids for t in rt.routing_state.topics.values())
    rt.routing_state.clear()
    assert not rt.routing_state.archive.entries


def test_rerouting_same_node_does_not_consume_followup_budget():
    rt, router = setup()
    for _ in range(5):
        router.route(rt, rt.dag.nodes["pending"])
    assert not rt.routing_state.pending_assignments["pending"].observed_ids
    rt.routing_state.prune(rt.dag, 35)
    assert not rt.routing_state.pending_assignments


@pytest.mark.asyncio
async def test_reranker_cannot_commit_after_reset():
    rt, router = setup()
    class Reranker:
        async def rerank(self, **kwargs):
            rt.revision += 1
            rt.routing_state.clear()
            return SimpleNamespace(choice="B", topic_id="b")
    await router.rerank_pending(rt, rt.dag.nodes["pending"], Reranker())
    assert not rt.routing_state.topics


@pytest.mark.asyncio
async def test_reranker_commits_only_topic_and_survives_embedding_reroute():
    rt, router = setup()
    class Reranker:
        async def rerank(self, **kwargs):
            return SimpleNamespace(choice="B", topic_id="b")
    node = rt.dag.nodes["pending"]
    await router.rerank_pending(rt, node, Reranker())
    assert "pending" in rt.routing_state.topics["b"].message_ids
    assert router.route(rt, node).topic_id == "b"
    assert not node.metadata["routing"]["bot_is_addressee"]


@pytest.mark.asyncio
async def test_new_topic_is_aggregated_with_real_candidate_context():
    rt, router = setup()
    node = rt.dag.add_message("new", "A", "a differently worded follow-up question", timestamp=4)
    router.route(rt, node)
    assert node.metadata["routing"]["topic_id"] == "new"
    class Reranker:
        async def rerank(self, **kwargs):
            assert all(c.topic_id != "new" for c in kwargs["candidates"])
            candidate = next(c for c in kwargs["candidates"] if c.topic_id == "a")
            assert "substantive topic a" in candidate.exemplars[0]
            return SimpleNamespace(choice="A", topic_id="a")
    await router.rerank_pending(rt, node, Reranker())
    assert node.metadata["routing"]["topic_id"] == "a"
    assert "new" not in rt.routing_state.topics
    assert "new" in rt.routing_state.topics["a"].message_ids
    assert not node.metadata["routing"]["parent_message_id"]


@pytest.mark.asyncio
async def test_new_topic_unknown_fallback_and_only_one_attempt():
    rt, router = setup()
    node = rt.dag.add_message("new", "A", "unrelated discussion", timestamp=4)
    router.route(rt, node)
    class Reranker:
        calls = 0
        async def rerank(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(choice="UNKNOWN", topic_id="")
    reranker = Reranker()
    await router.rerank_pending(rt, node, reranker)
    await router.rerank_pending(rt, node, reranker)
    assert reranker.calls == 1
    assert node.metadata["routing"]["topic_id"] == "new"


@pytest.mark.asyncio
async def test_title_once_and_reset_guard():
    rt, router = setup()
    class Titler:
        calls = 0
        async def title(self, **kwargs):
            self.calls += 1
            return "显卡散热优化"
    titler = Titler()
    node = rt.dag.nodes["a"]
    await router.title_topic(rt, node, titler)
    router.topic_resolver.rebuild_profile(rt.routing_state.topics["a"], rt.dag)
    await router.title_topic(rt, node, titler)
    assert rt.routing_state.topics["a"].label == "显卡散热优化"
    assert node.metadata["topic_title"] == "显卡散热优化"
    assert titler.calls == 1
    class ResetTitler:
        async def title(self, **kwargs):
            rt.revision += 1
            rt.routing_state.clear()
            return "过期标题"
    await router.title_topic(rt, rt.dag.nodes["b"], ResetTitler())
    assert "topic_title" not in rt.dag.nodes["b"].metadata
