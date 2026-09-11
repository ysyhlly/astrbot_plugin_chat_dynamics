import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import thread_router
from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import AddresseeResolver, ParentRetriever, ThreadRouter, TopicResolver


def setup():
    rt = SessionRuntime("room", "group", "room", bot_id="bot", dag=ConversationDAG())
    router = ThreadRouter(require_intense_dialogue=False)
    first = rt.dag.add_message("first", "A", "graphics driver installation", timestamp=1)
    router.route(rt, first)
    node = rt.dag.add_message("new", "B", "tomato growing in garden", timestamp=2)
    router.route(rt, node)
    return rt, router, first, node


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["rerank_pending", "title_topic"])
async def test_enrichment_snapshots_and_commits_under_lock_but_releases_for_io(method):
    rt, router, first, node = setup()
    started, release = asyncio.Event(), asyncio.Event()

    async def model(**kwargs):
        assert not rt.state_lock.locked()
        started.set()
        await release.wait()
        return "Garden" if method == "title_topic" else SimpleNamespace(choice="A", topic_id="first")

    target = node
    reranker = SimpleNamespace(title=model, rerank=model)
    async with rt.state_lock:
        task = asyncio.create_task(getattr(router, method)(rt, target, reranker))
        await asyncio.sleep(0)
        assert not started.is_set()
    await asyncio.wait_for(started.wait(), 1)
    async with rt.state_lock:
        release.set()
        await asyncio.sleep(0)
        assert not task.done()
        assert "topic_title" not in target.metadata
        assert target.metadata["routing"]["topic_id"] == "new"
    await asyncio.wait_for(task, 1)
    if method == "title_topic":
        assert target.metadata["topic_title"] == "Garden"
    else:
        assert target.metadata["routing"]["topic_id"] == "first"


@pytest.mark.asyncio
async def test_title_failure_retries_after_backoff_and_deduplicates_inflight(monkeypatch):
    rt, router, node, _ = setup()
    topic = rt.routing_state.topics["first"]
    clock = [100.0]
    monkeypatch.setattr(thread_router, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def title(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ""
        started.set()
        await release.wait()
        return "Drivers"

    reranker = SimpleNamespace(title=title)
    await router.title_topic(rt, node, reranker)
    assert topic.title_retry_at == 130.0
    await router.title_topic(rt, node, reranker)
    assert len(calls) == 1
    clock[0] = 131.0
    task = asyncio.create_task(router.title_topic(rt, node, reranker))
    await started.wait()
    await router.title_topic(rt, node, reranker)
    assert len(calls) == 2
    release.set()
    await task
    assert topic.generated_title == "Drivers"
    assert not topic.title_in_flight
    assert topic.title_retry_at == 0.0


@pytest.mark.asyncio
async def test_cancelled_title_clears_inflight_and_cannot_write_after_reset():
    rt, router, node, _ = setup()
    topic = rt.routing_state.topics["first"]
    started = asyncio.Event()

    async def title(**kwargs):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(router.title_topic(rt, node, SimpleNamespace(title=title)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not topic.title_in_flight
    assert not topic.generated_title

    async def stale_title(**kwargs):
        node.metadata["routing"] = dict(node.metadata["routing"])
        return "Stale"

    await router.title_topic(rt, node, SimpleNamespace(title=stale_title))
    assert "topic_title" not in node.metadata


@pytest.mark.parametrize("text,expected", [
    ("bot回答我", True), ("bot继续", True), ("hello bot please help", True),
    ("这个bot怎么老不回", False), ("both please help", False),
    ("bot今天很慢", False),
])
def test_vocative_evidence_is_shared(text, expected):
    assert AddresseeResolver.is_vocative_call(text, ["bot"]) is expected
    assert AddressivityRouter._name_mentioned_in_text("bot", text) is expected


def test_direct_name_call_can_target_bot_with_quoted_human_subject():
    rt, router, first, _ = setup()
    node = rt.dag.add_message("quote", "C", "bot请解释", timestamp=3, reply_to_id=first.msg_id)
    result = router.route(rt, node, bot_names=["bot"])
    assert result.bot_is_addressee
    assert result.parent_message_id == first.msg_id
    assert AddressivityRouter().compute_addressivity(node, rt.dag).is_bot_targeted


def test_parent_affinity_uses_platform_interactions_not_inferred_feedback():
    dag = ConversationDAG()
    candidate = dag.add_message("candidate", "A", "driver installation?", timestamp=1)
    candidate.metadata["routing"] = {"topic_id": "topic"}
    node = dag.add_message("node", "B", "driver installation works", timestamp=4)
    retriever = ParentRetriever()

    def score():
        return retriever.retrieve(node, dag, {"candidate": .9}, "topic")[1]

    baseline = score()
    interaction = dag.add_message("interaction", "B", "ok", timestamp=2)
    interaction.parent_ids.add("candidate")
    interaction.edge_kinds["candidate"] = "inferred_reply"
    inferred = score()
    interaction.reply_to_id = "candidate"
    explicit = score()
    assert explicit > inferred
    assert inferred <= baseline


@pytest.mark.parametrize("similarity,topic,committed", [(.99, "b", True), (.6, "b", False), (.99, "c", False)])
def test_only_high_confidence_inferred_followup_backfills_pending(similarity, topic, committed):
    class Resolver(TopicResolver):
        def resolve(self, node, dag, state, matches, explicit_parent=None):
            if node.msg_id == "pending":
                return "a", .55, True, ["topic_ambiguous"], "b", [(.55, "a"), (.53, "b")]
            return (topic if node.msg_id == "follow" else node.msg_id), .9, False, [], None, []

    rt = SessionRuntime("room", "group", "room", dag=ConversationDAG())
    router = ThreadRouter(topic_resolver=Resolver(), require_intense_dialogue=False)
    for index, mid in enumerate(("a", "b", "pending"), 1):
        text = "graphics driver version?" if mid == "pending" else "substantive topic " + mid
        node = rt.dag.add_message(mid, mid, text, timestamp=index)
        router.route(rt, node)
    rt.dag.semantic_match_fn = lambda left, right: SimpleNamespace(
        score=similarity if right.endswith("?") else .01, embedding_cosine=0.0)
    follow = rt.dag.add_message("follow", "other", "driver version works correctly", timestamp=4)
    router.route(rt, follow)
    pending = rt.dag.nodes["pending"]
    assert (pending.metadata["routing"]["topic_status"] == "committed") is committed
    if committed:
        assert follow.metadata["routing"]["parent_message_id"] == "pending"
        assert pending.metadata["routing"]["topic_id"] == "b"
        assert "pending" not in rt.routing_state.pending_assignments
