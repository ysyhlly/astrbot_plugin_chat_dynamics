import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG, ConversationNode
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.semantics import semantic_match
from astrbot_plugin_chat_dynamics.core.thread_router import (
    ThreadRouter,
    TopicResolver,
    ParentRetriever,
    AddresseeResolver,
    TopicState,
    RoutingState,
    build_contextual_query,
)


def setup():
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG())
    router = ThreadRouter()
    return runtime, router


def add(runtime, router, mid, user, text, ts, **kwargs):
    node = runtime.dag.add_message(mid, user, text, timestamp=ts, **kwargs)
    return node, router.route(runtime, node, bot_names=("bot",))


def test_explicit_reply_and_mention_have_separate_meaning():
    rt, router = setup()
    add(rt, router, "b", "B", "problem", 1)
    node, result = add(rt, router, "a", "A", "what about this", 2,
                       reply_to_id="b", mentioned_users=["bot"])
    assert result.parent_message_id == "b"
    assert result.addressee_ids == ["bot"]
    assert node.edge_kinds == {"b": "reply"}
    router.route(rt, node)
    assert node.edge_kinds == {"b": "reply"}


def test_active_interlocutor_followup_and_bystander():
    rt, router = setup()
    add(rt, router, "a", "A", "problem", 1)
    bot, _ = add(rt, router, "bot1", "bot", "try this", 2, reply_to_id="a")
    rt.last_bot_node = bot
    _, bystander = add(rt, router, "b", "B", "真的假的", 3)
    _, answer = add(rt, router, "a2", "A", "那怎么办", 4)
    assert not bystander.bot_is_addressee
    assert answer.bot_is_addressee
    assert answer.bot_addressee_confidence >= .72
    _, expired = add(rt, router, "a3", "A", "这个呢", 400)
    assert not expired.bot_is_addressee


def test_subject_is_not_addressee_and_direct_name_call():
    rt, router = setup()
    _, subject = add(rt, router, "a", "A", "这个bot怎么老不回", 1)
    _, direct = add(rt, router, "b", "B", "bot你怎么老不回", 2)
    assert subject.subject_is_bot and not subject.bot_is_addressee
    assert direct.bot_is_addressee


def test_interleaved_topics_short_answer_retrieves_question():
    rt, router = setup()
    from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
    adapter = EmbeddingAdapter(enabled=True)
    for text in ("5090温度有点高怎么办", "5090风扇曲线怎么设的？"):
        adapter.remember(text, [1, 0, 0])
    for text in ("今晚打游戏吗", "游戏可以啊几点？"):
        adapter.remember(text, [0, 1, 0])
    adapter.remember("我默认的", [0, 0, 1])
    rt.dag.semantic_match_fn = adapter.match
    gpu, _ = add(rt, router, "a", "A", "5090温度有点高怎么办", 1)
    game, _ = add(rt, router, "c", "C", "今晚打游戏吗", 2)
    question, _ = add(rt, router, "d", "D", "5090风扇曲线怎么设的？", 3)
    add(rt, router, "e", "E", "游戏可以啊几点？", 4)
    answer, result = add(rt, router, "a2", "A", "我默认的", 5)
    assert build_contextual_query(answer, rt.dag) == answer.text
    gpu_topic = rt.routing_state.topics[gpu.metadata["routing"]["topic_id"]]
    contextual = build_contextual_query(answer, rt.dag, gpu_topic)
    assert gpu.text in contextual and question.text in contextual
    assert game.text not in contextual
    assert question.metadata["routing"]["topic_id"] == gpu_topic.topic_id
    # Without an explicit interlocutor the short answer cannot identify which
    # parallel conversation it continues; retain it without polluting either.
    assert result.topic_id == "" and result.topic_status == "pending"
    assert not result.parent_message_id and not result.addressee_ids
    assert answer.msg_id not in gpu_topic.message_ids


def test_ambiguity_does_not_link_and_reroute_removes_old_inference():
    rt, router = setup()
    first, _ = add(rt, router, "a", "A", "hello", 1)
    node, result = add(rt, router, "b", "B", "哈哈", 2)
    assert not result.parent_message_id
    rt.dag._link_parent(node.msg_id, first.msg_id, "inferred_reply")
    router.route(rt, node)
    assert not node.parent_ids


def test_state_isolation_reset_and_bounded_pruning():
    rt, router = setup()
    other, _ = setup()
    for i in range(100):
        add(rt, router, str(i), "A", "x", i + 1)
    assert sum(len(t.message_ids) for t in rt.routing_state.topics.values()) <= 80
    assert not other.routing_state.topics
    rt.reset_conversation_state()
    assert not rt.routing_state.topics


def test_delayed_reroute_does_not_discard_newer_topics():
    rt, router = setup()
    old, _ = add(rt, router, "old", "A", "old topic", 1)
    newer, _ = add(rt, router, "new", "B", "new subject", 2)
    router.route(rt, old)
    ids = {mid for topic in rt.routing_state.topics.values() for mid in topic.message_ids}
    assert newer.msg_id in ids
    assert old.msg_id in ids


def test_repeated_low_information_never_infers_parent():
    rt, router = setup()
    add(rt, router, "a", "A", "哈哈哈哈", 1)
    node, result = add(rt, router, "b", "B", "哈哈哈哈", 2)
    assert not node.parent_ids
    assert not result.parent_message_id


def test_quote_can_be_subject_of_active_bot_question_with_topic_evidence():
    rt, router = setup()
    add(rt, router, "b", "B", "参考方案", 1)
    add(rt, router, "a", "A", "帮我分析", 2, reply_to_id="b", mentioned_users=["bot"])
    bot, _ = add(rt, router, "bot1", "bot", "先看参考方案", 3, reply_to_id="a")
    rt.last_bot_node = bot
    node, result = add(rt, router, "a2", "A", "这个呢", 4, reply_to_id="b")
    assert result.parent_message_id == "b"
    assert result.addressee_ids == ["bot"]
    assert node.edge_kinds == {"b": "reply"}


def test_two_technical_topics_do_not_merge_on_generic_scene_alone():
    rt, router = setup()
    _, python = add(rt, router, "a", "A", "Python asyncio gather异常堆栈", 1)
    _, network = add(rt, router, "b", "B", "路由器DNS解析部署失败", 2)
    assert python.topic_id != network.topic_id


def test_explicit_human_mention_wins_over_active_bot_followup():
    rt, router = setup()
    add(rt, router, "a", "A", "帮我看看", 1)
    bot, _ = add(rt, router, "bot1", "bot", "试一下", 2, reply_to_id="a")
    rt.last_bot_node = bot
    _, result = add(rt, router, "a2", "A", "那怎么办", 3, mentioned_users=["B"])
    assert result.addressee_ids == ["B"]
    assert not result.bot_is_addressee


def test_topic_resolver_profile_scoring_and_thresholds():
    from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
    resolver = TopicResolver()
    adapter = EmbeddingAdapter(enabled=True)
    dag = ConversationDAG(semantic_match_fn=adapter.match)
    dag.add_message("m1", "Alice", "Python 异步编程教程", timestamp=10.0)
    node = ConversationNode("m2", "Alice", "并发协程的学习资料", timestamp=20.0)
    adapter.remember("Python 异步编程教程", [1, 0])
    adapter.remember(node.text, [1, 0])
    topic = TopicState("topic_py", message_ids=["m1"])
    state = RoutingState(topics={"topic_py": topic})
    topic_id, conf, ambiguous, evidence, _, _ = resolver.resolve(node, dag, state, {"m1": .95})
    assert topic_id == "topic_py" and conf >= resolver.join_threshold
    assert not ambiguous and "topic_profile" in evidence
    assert topic.centroid_space.startswith("neural:")
    assert topic.exemplar_messages and topic.recent_message_ids == ["m1"]

    # Equal topic evidence is ambiguous even when each candidate is strong.
    state.topics["other"] = TopicState("other", message_ids=["m1"])
    assert resolver.resolve(node, dag, state, {"m1": .95})[2]

    unrelated = ConversationNode("m4", "Charlie", "今天天气真好", timestamp=20.0)
    adapter.remember(unrelated.text, [0, 1])
    topic_id, _, ambiguous, _, _, _ = resolver.resolve(unrelated, dag, state, {"m1": .05})
    assert topic_id == "m4" and not ambiguous


def test_parent_retriever_6_factor_scoring_and_dual_similarity():
    """Verify ParentRetriever 6-factor composite equation and candidate scoping."""
    retriever = ParentRetriever(accept_threshold=0.72, margin_threshold=0.08)
    dag = ConversationDAG()
    q_node = dag.add_message("q1", "Alice", "5090显卡功耗和发热测试？", timestamp=10.0)
    q_node.metadata["routing"] = {"topic_id": "topic_gpu"}

    a_node = ConversationNode("a1", "Bob", "测试结果发热很低，功耗450W", timestamp=12.0)
    matches = {"q1": 0.92}

    # Verify score_candidate formula components
    score = retriever.score_candidate(
        node=a_node,
        candidate=q_node,
        sim=0.92,
        is_primary_topic=True,
        turn_distance=1,
    )
    # 0.92*0.38 + 1.0*0.20 + 1.0*0.17 + (1-2/180)*0.10 + (1-0.15)*0.10 + 0.5*0.05
    assert score >= 0.85

    # Retrieve candidate
    parent_id, p_conf, p_margin, p_ev, possible_mid, candidates = retriever.retrieve(
        node=a_node,
        dag=dag,
        matches=matches,
        primary_topic="topic_gpu",
    )
    assert parent_id == "q1"
    assert p_conf >= 0.72
    assert p_margin >= 0.08
    assert "inferred_reply" in p_ev
    assert possible_mid == "q1"


def test_addressee_resolver_vocative_cues_and_demonstratives():
    """Verify AddresseeResolver distinguishes direct vocatives from demonstrative subject mentions."""
    resolver = AddresseeResolver()
    bot_names = ("bot", "小助手")

    # Direct vocative calls
    assert resolver.is_vocative_call("bot，你觉得这个怎么样", bot_names) is True
    assert resolver.is_vocative_call("小助手: 帮我算一下", bot_names) is True
    assert resolver.is_vocative_call("bot看看这个", bot_names) is True
    assert resolver.is_vocative_call("bot回答我", bot_names) is True
    assert resolver.is_vocative_call("bot为什么报错", bot_names) is True
    assert resolver.is_vocative_call("bot在吗", bot_names) is True

    # Demonstrative subject references (filtered out from vocatives)
    assert resolver.is_vocative_call("这个bot怎么老不回", bot_names) is False
    assert resolver.is_vocative_call("那bot今天好像有点卡", bot_names) is False
    assert resolver.is_vocative_call("现在的bot真好用", bot_names) is False

    # Subject reference classification
    assert resolver.is_subject_reference("这个bot怎么老不回", bot_names) is True
    assert resolver.is_subject_reference("明天开会讨论需求", bot_names) is False


@pytest.mark.asyncio
async def test_route_async_fast_path_and_timeout_fallback():
    """Verify route_async returns immediately for unambiguous turns and falls back on timeout."""
    router = ThreadRouter()
    dag = ConversationDAG()
    node = dag.add_message("m1", "Alice", "你好", timestamp=10.0, mentioned_users=["bot"])
    runtime = SimpleNamespace(dag=dag, routing_state=RoutingState(), bot_id="bot", last_bot_node=None)

    # 1. Fast-path: explicit mention is unambiguous -> returns immediately
    class UncalledAdapter:
        enabled = True

        async def embed(self, text):
            raise AssertionError("Should not be called for unambiguous turn")

    fast_result = await router.route_async(
        runtime=runtime,
        node=node,
        bot_names=("bot",),
        embedding_adapter=UncalledAdapter(),
    )
    assert fast_result.bot_is_addressee is True
    assert fast_result.ambiguous is False

    # 2. Ambiguous-path with timeout fallback
    ambig_node = dag.add_message("m2", "Bob", "这个呢", timestamp=20.0)

    class SlowHangingAdapter:
        enabled = True

        async def embed(self, text):
            await asyncio.sleep(10.0)
            return [1.0, 0.0]

    timeout_result = await router.route_async(
        runtime=runtime,
        node=ambig_node,
        bot_names=("bot",),
        timeout=0.05,
        embedding_adapter=SlowHangingAdapter(),
    )
    # Gracefully falls back to synchronous hashed inference without error
    assert timeout_result is not None
    assert timeout_result.topic_id == ""
    assert timeout_result.topic_status == "pending"


def test_mock_runtime_safe_attribute_access():
    """Verify route handles minimal mock runtime objects without raising AttributeError."""
    router = ThreadRouter()
    dag = ConversationDAG()
    node = dag.add_message("m1", "Alice", "test", timestamp=1.0)
    # Minimal namespace with only dag and routing_state
    runtime = SimpleNamespace(dag=dag, routing_state=RoutingState())
    inference = router.route(runtime, node)
    assert inference.topic_id == "m1"
    assert inference.parent_message_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["revision", "remove"])
async def test_async_route_does_not_restore_invalidated_state(invalidate):
    runtime, router = setup()
    node = runtime.dag.add_message("old", "alice", "hello", timestamp=10)

    class Adapter:
        enabled = True

        async def embed(self, text):
            runtime.routing_state.clear()
            if invalidate == "revision":
                runtime.revision += 1
            else:
                runtime.dag.nodes.pop(node.msg_id)
            return [1.0, 0.0]

    await router.route_async(runtime, node, embedding_adapter=Adapter())
    assert not runtime.routing_state.topics
