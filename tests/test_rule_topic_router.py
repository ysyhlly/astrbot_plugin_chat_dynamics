import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.rule_topic_router import RuleTopicRouter
from astrbot_plugin_chat_dynamics.core.discussion_reranker import DiscussionReranker, DiscussionRanking
from astrbot_plugin_chat_dynamics.core.topic_reranker import RerankCandidate
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config


def setup():
    return SessionRuntime("room", "g", "room", bot_id="bot", dag=ConversationDAG()), RuleTopicRouter()


def add(rt, router, mid, user, text="这是一个具体的讨论问题", t=0, mentions=(), quote=None):
    node = rt.dag.add_message(mid, user, text, timestamp=t, mentioned_users=list(mentions), reply_to_id=quote)
    router.route(rt, node)
    return node


def route(node):
    return node.metadata["routing"]


def scorer(scores):
    async def rank(text, candidates):
        return DiscussionRanking(
            tuple(sorted([(scores.get(c.topic_id, 0.05), c.topic_id) for c in candidates], reverse=True)), "ranked"
        )

    return SimpleNamespace(rank=rank, threshold=0.65, margin=0.1)


def test_default_configuration_and_rollback():
    cfg, warnings = parse_runtime_config({})
    assert cfg.rule_topic_routing_enabled and cfg.discussion_reranker_provider == ""
    cfg, _ = parse_runtime_config({"rule_topic_routing_enabled": False})
    assert not cfg.rule_topic_routing_enabled


def test_no_parent_search_or_nearest_mention_edge():
    rt, router = setup()
    router.parent_retriever.retrieve = lambda **kw: pytest.fail("parent search must never run")
    add(rt, router, "a", "A")
    b = add(rt, router, "b", "B", t=1, mentions=["A"])
    assert route(b)["addressee_ids"] == ["A"]
    assert route(b)["parent_message_id"] == "" and not b.parent_ids
    c = add(rt, router, "c", "A", "好的", t=2)
    assert route(c)["addressee_ids"] == ["B"]
    assert not c.parent_ids


def test_switch_timeout_and_outsider():
    rt, router = setup()
    add(rt, router, "a", "A", mentions=["B"])
    assert route(add(rt, router, "a2", "A", t=10))["addressee_ids"] == ["B"]
    add(rt, router, "c", "C", t=11)
    assert route(add(rt, router, "a3", "A", t=12))["addressee_ids"] == []
    add(rt, router, "a4", "A", t=13, mentions=["D"])
    assert route(add(rt, router, "a5", "A", t=60))["addressee_ids"] == []


def test_quote_author_outside_short_window_and_missing_author():
    rt, router = setup()
    add(rt, router, "old", "A", t=0)
    current = add(rt, router, "now", "B", t=200, quote="old")
    assert route(current)["addressee_ids"] == ["A"]
    assert route(current)["parent_message_id"] == "old"
    unknown = add(rt, router, "unknown", "B", t=201, quote="missing")
    assert route(unknown)["addressee_ids"] == []


def test_multiple_recipients_do_not_seed_continuation():
    rt, router = setup()
    node = add(rt, router, "a", "A", mentions=["B", "C"])
    assert route(node)["addressee_ids"] == ["B", "C"]
    assert route(add(rt, router, "b", "A", t=1))["addressee_ids"] == []


def test_future_same_timestamp_not_used():
    rt, router = setup()
    first = add(rt, router, "1", "A", "嗯", t=0)
    add(rt, router, "2", "B", t=0, mentions=["A"])
    router.route(rt, first)
    assert route(first)["addressee_ids"] == []
    assert router._candidates(rt, first) == []


def test_stop_revision_drops_old_anchor():
    rt, router = setup()
    add(rt, router, "a", "A", mentions=["B"])
    rt.topic_stop_revisions["A"] = 1
    assert route(add(rt, router, "b", "A", t=1))["addressee_ids"] == []


def test_delivered_bot_revision_belongs_to_interlocutor():
    rt, router = setup()
    rt.topic_stop_revisions["A"] = 1
    add(rt, router, "a", "A", mentions=["bot"])
    reply = add(rt, router, "bot-msg", "bot", t=1, quote="a")
    reply.metadata.update(trigger_user_id="A", dialogue_stop_revision=1)
    followup = add(rt, router, "followup", "A", "继续", t=2)
    assert route(followup)["addressee_ids"] == ["bot"]


def test_native_quote_is_preserved_and_idempotent():
    rt, router = setup()
    add(rt, router, "a", "A")
    b = add(rt, router, "b", "B", t=1, quote="a")
    before = dict(route(b))
    router.route(rt, b)
    assert route(b) == before
    assert b.edge_kinds["a"] == "reply"
    assert route(b)["topic_id"] == "a"


@pytest.mark.asyncio
async def test_abc_cde_share_c_without_merging_topics():
    rt, router = setup()
    add(rt, router, "pc", "A", "电脑显卡散热方案", t=0)
    add(rt, router, "pc-b", "B", "显卡风扇转速", t=1, quote="pc")
    add(rt, router, "pc-c", "C", "机箱进风温度", t=2, quote="pc")
    dinner = add(rt, router, "dinner", "C", "周末聚餐选哪家餐厅", t=3, mentions=["D"])
    await router.rerank_pending(rt, dinner, scorer({}))
    add(rt, router, "dinner-d", "D", "聚餐吃火锅", t=4, quote="dinner")
    add(rt, router, "dinner-e", "E", "周六一起吃饭", t=5, quote="dinner")
    assert set(rt.routing_state.topics) == {"pc", "dinner"}
    assert rt.routing_state.topics["pc"].participants == {"A", "B", "C"}
    assert rt.routing_state.topics["dinner"].participants == {"C", "D", "E"}
    back = add(rt, router, "back", "C", "显卡风扇我再看一下", t=6)
    await router.rerank_pending(rt, back, scorer({"pc": 0.9}))
    assert route(back)["topic_id"] == "pc"
    assert route(back)["parent_message_id"] == ""
    assert len(rt.routing_state.topics) == 2


@pytest.mark.asyncio
async def test_different_people_can_join_same_discussion():
    rt, router = setup()
    add(rt, router, "pc", "A", "显卡散热风扇", t=0)
    node = add(rt, router, "b", "E", "机箱散热可以加风扇", t=1)
    await router.rerank_pending(rt, node, scorer({"pc": 0.9}))
    assert route(node)["topic_id"] == "pc"
    assert route(node)["addressee_ids"] == []
    assert node.metadata["discussion_support_topic_id"] == "pc"
    assert not node.metadata.get("discussion_support_ids"), "a topic score is not a sentence-level connection"


@pytest.mark.asyncio
async def test_ambiguous_topics_do_not_union():
    rt, router = setup()
    add(rt, router, "a", "A", "显卡风扇方案", t=0)
    other = add(rt, router, "b", "C", "周末聚餐方案", t=1)
    await router.rerank_pending(rt, other, scorer({}))
    add(rt, router, "a-support", "B", "好的", t=2, quote="a")
    add(rt, router, "b-support", "D", "我来", t=3, quote="b")
    current = add(rt, router, "c", "C", "两个方案都行", t=4)
    await router.rerank_pending(rt, current, scorer({"a": 0.85, "b": 0.83}))
    assert route(current)["topic_id"] == ""
    assert set(route(current)["discussion_candidate_ids"]) == {"a", "b"}
    assert len(rt.routing_state.topics) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["epoch", "stop", "replace", "members"])
async def test_late_results_cannot_commit(change):
    rt, router = setup()
    add(rt, router, "a", "A", t=0)
    add(rt, router, "a-support", "C", "同意", t=0.5, quote="a")
    node = add(rt, router, "b", "B", t=1)
    started, release = asyncio.Event(), asyncio.Event()

    async def rank(*args):
        assert not rt.state_lock.locked()
        started.set()
        await release.wait()
        return DiscussionRanking(((0.9, "a"),), "ranked")

    task = asyncio.create_task(router.rerank_pending(rt, node, SimpleNamespace(rank=rank, threshold=0.65, margin=0.1)))
    await started.wait()
    if change == "epoch":
        rt.epoch += 1
    elif change == "stop":
        rt.topic_stop_revisions["A"] = 1
    elif change == "replace":
        node.metadata["routing"] = dict(route(node))
    else:
        rt.routing_state.topics["a"].message_ids.clear()
    release.set()
    await task
    assert route(node)["topic_id"] == ""


def test_representative_ignores_filler_and_counts_distinct_people():
    rt, router = setup()
    add(rt, router, "a", "A", "讨论显卡风扇方案", t=0)
    add(rt, router, "b", "B", "机箱风扇要怎么安装", t=1, quote="a")
    for i in range(8):
        add(rt, router, f"spam{i}", "C", "哈哈", t=i + 2, quote="b")
    add(rt, router, "d", "D", "好的", t=11, quote="a")
    rows = list(rt.dag.nodes.values())
    assert router._representative(rows, 11).msg_id == "a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,reason",
    [
        ([SimpleNamespace(index=0, relevance_score=0.9)], "ranked"),
        ([SimpleNamespace(index=0, relevance_score=float("nan"))], "invalid_scores"),
        ([SimpleNamespace(index=4, relevance_score=0.9)], "invalid_scores"),
        ([SimpleNamespace(index=0, relevance_score=0.9)] * 2, "invalid_scores"),
        ([], "incomplete_scores"),
    ],
)
async def test_native_provider_score_contract(rows, reason):
    provider = SimpleNamespace(rerank=AsyncMock(return_value=rows))
    context = SimpleNamespace(get_provider_by_id=lambda pid: provider)
    ranker = DiscussionReranker(context, "native")
    result = await ranker.rank("散热风扇", [RerankCandidate("a", "显卡风扇")])
    assert result.reason == reason


@pytest.mark.asyncio
async def test_unconfigured_provider_and_cancellation():
    ctx = SimpleNamespace(get_provider_by_id=lambda pid: pytest.fail("must not pick another model"))
    result = await DiscussionReranker(ctx, "").rank("message", [RerankCandidate("a")])
    assert result.reason == "not_configured"
    provider = SimpleNamespace(rerank=AsyncMock(side_effect=asyncio.CancelledError))
    ctx = SimpleNamespace(get_provider_by_id=lambda pid: provider)
    with pytest.raises(asyncio.CancelledError):
        await DiscussionReranker(ctx, "native").rank("message", [RerankCandidate("a")])
