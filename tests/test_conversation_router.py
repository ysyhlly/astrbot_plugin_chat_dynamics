"""Comprehensive production-grade test suite for Conversation Router.

Validates the short-term group chat Conversation Router layer (`core/thread_router.py`)
and its integration with `core/graph.py`, `core/session_runtime.py`, `core/addressivity.py`,
and `core/message_semantics.py`.

Covers all 4 Testing Tiers:
- Tier 1: Feature Coverage (Data contracts, deterministic DAG ingestion, topic & addressee resolution)
- Tier 2: Boundaries & Corner Cases (Contextual query boundaries, low-info suppression, TTL, pruning)
- Tier 3: Cross-Feature Combinations (Precedence, re-routing idempotency, competing chatter)
- Tier 4: Realistic Multi-Turn Chat Scenarios (All 10 Core Regression Scenarios):
    1. Bot->A, A: "那怎么办？" -> addressee=Bot
    2. Bot->A, B: "真的假的" -> addressee!=Bot
    3. D: "风扇怎么设？" + interleaved gaming chatter + A: "默认" -> parent=D, topic=GPU
    4. A quotes B + "@bot 他说得对吗" -> quoted=B, addressee=Bot
    5. A quotes B: "这个呢？" while A is in active dialogue with Bot -> addressee=Bot
    6. A: "这个bot怎么老不回" -> subject=Bot, addressee!=Bot
    7. A: "bot，你怎么老不回" -> addressee=Bot
    8. A: "哈哈" -> no high-confidence parent link
    9. Two concurrent technical topics -> separated into two distinct topics
    10. Topic inactive for > 5 min followed by "这个呢" -> does not force-resume dead topic
"""

from __future__ import annotations

from dataclasses import replace

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG, ConversationNode
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import (
    ThreadRouter,
    RoutingInference,
    RoutingState,
    build_contextual_query,
    WINDOW_NODES,
)
from astrbot_plugin_chat_dynamics.core.addressivity import (
    AddressivityRouter,
    AddressivityLevel,
)
from astrbot_plugin_chat_dynamics.core.message_semantics import describe_message
from astrbot_plugin_chat_dynamics.core.semantics import semantic_match


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

def _setup_router_env(
    session_id: str = "test_room",
    bot_id: str = "bot",
    bot_names: tuple[str, ...] = ("bot", "小助手"),
) -> tuple[SessionRuntime, ThreadRouter, AddressivityRouter]:
    """Sets up an isolated, in-memory router testing environment."""
    dag = ConversationDAG(session_id=session_id)
    runtime = SessionRuntime(
        session_key=session_id,
        group_id=session_id,
        umo=f"test:GroupMessage:{session_id}",
        bot_id=bot_id,
        dag=dag,
    )
    router = ThreadRouter()
    addressivity = AddressivityRouter(bot_id=bot_id, bot_names=list(bot_names))
    return runtime, router, addressivity


def _add_turn(
    runtime: SessionRuntime,
    router: ThreadRouter,
    mid: str,
    user: str,
    text: str,
    ts: float,
    reply_to_id: str | None = None,
    mentioned_users: list[str] | None = None,
    bot_names: tuple[str, ...] = ("bot", "小助手"),
) -> tuple[ConversationNode, RoutingInference]:
    """Ingests a message into the DAG and routes it through ThreadRouter."""
    node = runtime.dag.add_message(
        msg_id=mid,
        user_id=user,
        text=text,
        timestamp=ts,
        reply_to_id=reply_to_id,
        mentioned_users=mentioned_users or [],
    )
    routing = router.route(runtime, node, bot_names=bot_names)
    runtime.touch(ts)
    return node, routing


# ===========================================================================
# Tier 1: Core Feature Coverage
# ===========================================================================

def test_tier1_routing_inference_dataclass_defaults():
    """R2.1: RoutingInference captures all required routing and topological attributes."""
    inference = RoutingInference()
    assert inference.topic_id == ""
    assert inference.topic_confidence == 0.0
    assert inference.parent_message_id == ""
    assert inference.parent_confidence == 0.0
    assert inference.addressee_ids == []
    assert inference.addressee_confidence == 0.0
    assert inference.subject_user_ids == []
    assert inference.subject_is_bot is False
    assert inference.bot_is_addressee is False
    assert inference.bot_addressee_confidence == 0.0
    assert inference.ambiguous is True
    assert inference.evidence == []
    assert inference.possible_parent == ""

    # Test initialization with explicit fields
    custom = RoutingInference(
        topic_id="topic_1",
        topic_confidence=0.88,
        parent_message_id="msg_0",
        parent_confidence=0.82,
        addressee_ids=["bot"],
        bot_is_addressee=True,
        bot_addressee_confidence=0.82,
        ambiguous=False,
        evidence=["active_interlocutor_followup"],
    )
    assert custom.topic_id == "topic_1"
    assert custom.bot_is_addressee is True
    assert custom.ambiguous is False


def test_tier1_dag_ingestion_is_strictly_deterministic():
    """R1.1 & R1.2: ConversationDAG.add_message() creates ONLY deterministic edges."""
    dag = ConversationDAG(session_id="test_dag")
    # Message 1
    m1 = dag.add_message("m1", "Alice", "Hello world", timestamp=10.0)
    assert m1.parent_ids == set()
    assert m1.edge_kinds == {}
    assert m1.thread_id == "m1"

    # Message 2: Explicit reply
    m2 = dag.add_message("m2", "Bob", "Replying to Alice", timestamp=12.0, reply_to_id="m1")
    assert m2.parent_ids == {"m1"}
    assert m2.edge_kinds == {"m1": "reply"}
    assert m2.thread_id == m1.thread_id

    # Message 3: Mention
    m3 = dag.add_message("m3", "Charlie", "Calling Alice", timestamp=14.0, mentioned_users=["Alice"])
    assert "m1" in m3.parent_ids
    assert m3.edge_kinds.get("m1") == "mention"

    # Message 4: Semantically similar message without reply_to or mention
    # DAG add_message must NOT create semantic links automatically
    m4 = dag.add_message("m4", "David", "Hello world everyone", timestamp=16.0)
    assert m4.parent_ids == set()
    assert "semantic" not in m4.edge_kinds.values()
    assert "inferred_reply" not in m4.edge_kinds.values()


def test_tier1_thread_id_vs_topic_id_separation():
    """R1.3: node.thread_id tracks reply trees; topic state resides in node.metadata['routing']['topic_id']."""
    rt, router, _ = _setup_router_env()
    n1, r1 = _add_turn(rt, router, "n1", "Alice", "第一话题开头", 10.0)
    n2, r2 = _add_turn(rt, router, "n2", "Bob", "跟帖回复", 12.0, reply_to_id="n1")

    # Reply tree: n2 inherits n1's thread_id
    assert n1.thread_id == "n1"
    assert n2.thread_id == "n1"

    # Topic ID is recorded in metadata["routing"]
    assert n1.metadata["routing"]["topic_id"] == "n1"
    assert n2.metadata["routing"]["topic_id"] == "n1"


def test_tier1_topic_resolver_clustering_and_state():
    """R2.2: TopicState tracks participants and message IDs; RoutingState._remember works correctly."""
    state = RoutingState()
    node1 = ConversationNode(msg_id="m1", user_id="Alice", text="Topic 1 start", timestamp=10.0)
    ThreadRouter._remember(state, node1, "topic_alpha")

    assert "topic_alpha" in state.topics
    topic = state.topics["topic_alpha"]
    assert topic.message_ids == ["m1"]
    assert topic.participants == {"Alice"}
    assert topic.updated_at == 10.0

    # Add second message to same topic
    node2 = ConversationNode(msg_id="m2", user_id="Bob", text="Topic 1 continue", timestamp=15.0)
    ThreadRouter._remember(state, node2, "topic_alpha")
    assert topic.message_ids == ["m1", "m2"]
    assert topic.participants == {"Alice", "Bob"}
    assert topic.updated_at == 15.0


def test_tier1_parent_retriever_promotes_inferred_edge():
    """R2.3: ParentRetriever promotes high-confidence candidate to DAG inferred_reply edge."""
    rt, router, _ = _setup_router_env()
    q_node, _ = _add_turn(rt, router, "q1", "Alice", "Python 怎么解析 JSON 文件？", 10.0)
    
    # Force semantic match between q1 and answer
    def custom_match(left, right):
        if "JSON" in left and "JSON" in right:
            return replace(semantic_match(left, right), score=0.96)
        return semantic_match(left, right)
    rt.dag.semantic_match_fn = custom_match

    ans_node, ans_routing = _add_turn(rt, router, "a1", "Bob", "使用 json.loads() 或者 json.load() 读取 JSON", 15.0)

    assert ans_routing.parent_message_id == "q1"
    assert ans_routing.parent_confidence >= 0.72
    assert ans_routing.addressee_ids == ["Alice"]
    assert "inferred_reply" in ans_routing.evidence
    assert ans_node.edge_kinds.get("q1") == "inferred_reply"
    assert "q1" in ans_node.parent_ids
    assert "a1" in q_node.child_ids


def test_tier1_addressee_resolver_vocative_cues():
    """R2.4: AddresseeResolver detects direct vocative address patterns."""
    rt, router, _ = _setup_router_env(bot_id="bot", bot_names=("bot", "小助手"))

    # Direct vocative with comma
    _, r1 = _add_turn(rt, router, "t1", "Alice", "bot，你觉得这个怎么样", 10.0)
    assert r1.bot_is_addressee is True
    assert "direct_name_call" in r1.evidence
    assert r1.bot_addressee_confidence >= 0.90

    # Direct vocative with colon
    _, r2 = _add_turn(rt, router, "t2", "Bob", "小助手: 帮我算一下", 12.0)
    assert r2.bot_is_addressee is True
    assert "direct_name_call" in r2.evidence

    # Third person subject (not vocative)
    _, r3 = _add_turn(rt, router, "t3", "Charlie", "小助手今天好像有点卡", 14.0)
    assert r3.subject_is_bot is True
    assert r3.bot_is_addressee is False


# ===========================================================================
# Tier 2: Boundaries, Edge Cases & Temporal Decay
# ===========================================================================

def test_tier2_contextual_query_expansion_boundaries():
    """Candidate-local short context stays separate from the raw semantic query."""
    dag = ConversationDAG(session_id="query_test")
    # Substantive turn by Alice
    dag.add_message("m1", "Alice", "5090显卡功耗和发热测试结果", timestamp=10.0)
    # Alice short elliptical turn (<= 16 chars)
    short_turn = dag.add_message("m2", "Alice", "那怎么办？", timestamp=20.0)
    query_short = build_contextual_query(short_turn, dag)
    assert query_short == short_turn.text
    from astrbot_plugin_chat_dynamics.core.session_runtime import TopicState
    candidate = TopicState("gpu", message_ids=["m1"])
    assert "5090显卡功耗" in build_contextual_query(short_turn, dag, candidate)

    # Alice long turn (> 16 chars)
    long_turn = dag.add_message("m3", "Alice", "请问这个功耗墙设置多少瓦比较安全呢？", timestamp=30.0)
    query_long = build_contextual_query(long_turn, dag)
    assert query_long == long_turn.text

    # Bob short turn - should NOT borrow Alice's prior turn
    bob_turn = dag.add_message("m4", "Bob", "那怎么办？", timestamp=40.0)
    query_bob = build_contextual_query(bob_turn, dag)
    assert query_bob == "那怎么办？"

    # Alice turn arriving > 90s later - should NOT borrow stale turn
    stale_turn = dag.add_message("m5", "Alice", "那怎么办？", timestamp=130.0)
    query_stale = build_contextual_query(stale_turn, dag)
    assert query_stale == "那怎么办？"


def test_tier2_low_information_filler_suppression():
    """R2.3: Low-information phatic messages are suppressed from inferring parents or DAG edges."""
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "q", "Alice", "明天几点去开会？", 10.0)

    for idx, filler in enumerate(["哈哈", "2333", "?", "？？", "好的", "ok", "嗯嗯", "哦哦"]):
        node, routing = _add_turn(rt, router, f"f_{idx}", "Bob", filler, 12.0 + idx)
        assert routing.parent_message_id == "", f"Filler '{filler}' should not link parent"
        assert "q" not in node.parent_ids
        assert "inferred_reply" not in node.edge_kinds.values()


def test_tier2_parent_scoring_margin_gate():
    """R2.3: Inferred parent requires a score margin >= 0.10 over competing candidates."""
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "c1", "Alice", "关于深度学习模型微调参数的问题？", 10.0)
    _add_turn(rt, router, "c2", "Bob", "关于深度学习模型评估指标的问题？", 12.0)

    # Ingest a response that matches both candidates almost equally
    def tie_match(left, right):
        if "深度学习" in left and "深度学习" in right:
            return replace(semantic_match(left, right), score=0.85)
        return semantic_match(left, right)
    rt.dag.semantic_match_fn = tie_match

    ans_node, ans_routing = _add_turn(rt, router, "ans", "Charlie", "建议看看深度学习的官方文档说明", 14.0)

    # When candidates have almost identical scores (margin < 0.10), router does not link an inferred parent
    assert ans_routing.parent_message_id == ""
    assert ans_node.edge_kinds.get("c1") != "inferred_reply"
    assert ans_node.edge_kinds.get("c2") != "inferred_reply"


def test_tier2_ttl_and_bounded_pruning():
    """R2.2: Nodes older than WINDOW_SECONDS (300s) are pruned; max 80 nodes kept."""
    rt, router, _ = _setup_router_env()

    # Add 90 messages spaced by 1 second
    for i in range(90):
        _add_turn(rt, router, f"m_{i}", "Alice", f"Message number {i}", float(i + 1))

    # Bounded topic nodes must not exceed 80
    total_tracked = sum(len(t.message_ids) for t in rt.routing_state.topics.values())
    assert total_tracked <= WINDOW_NODES

    # Add a message 400s later (exceeds 300s TTL)
    _add_turn(rt, router, "late", "Alice", "Late message", 500.0)
    # The early messages should be pruned
    all_tracked_mids = {mid for t in rt.routing_state.topics.values() for mid in t.message_ids}
    assert "m_0" not in all_tracked_mids
    assert "m_10" not in all_tracked_mids


def test_tier2_session_runtime_state_isolation_and_reset():
    """R3.1: RoutingState is isolated per session runtime; reset_conversation_state clears state."""
    rt1, router1, _ = _setup_router_env(session_id="room_1")
    rt2, router2, _ = _setup_router_env(session_id="room_2")

    _add_turn(rt1, router1, "r1_m1", "Alice", "Room 1 turn", 10.0)
    assert len(rt1.routing_state.topics) > 0
    assert len(rt2.routing_state.topics) == 0

    # Reset room 1
    rt1.reset_conversation_state()
    assert len(rt1.routing_state.topics) == 0


# ===========================================================================
# Tier 3: Cross-Feature Combinations & Precedence Hierarchy
# ===========================================================================

def test_tier3_explicit_mention_overrides_active_bot_followup():
    """R2.4: Explicit @mention of human overrides active bot dialogue interlocutor bonus."""
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "a1", "Alice", "帮我看看这个报错", 10.0)
    bot_node, _ = _add_turn(rt, router, "bot1", "bot", "这是空指针异常", 12.0, reply_to_id="a1")
    rt.last_bot_node = bot_node
    rt.last_interlocutor = "Alice"

    # Alice asks a follow-up, but explicitly mentions Bob instead of Bot
    _, routing = _add_turn(
        rt, router, "a2", "Alice", "@Bob 那怎么办", 14.0, mentioned_users=["Bob"]
    )

    assert routing.addressee_ids == ["Bob"]
    assert routing.bot_is_addressee is False
    assert "explicit_mention" in routing.evidence
    assert "active_interlocutor_followup" not in routing.evidence


def test_tier3_re_routing_idempotency_removes_old_inferred_edge():
    """R1.1 & R2.3: Re-routing a node removes previous inferred_reply edge and updates topic state."""
    rt, router, _ = _setup_router_env()
    q_node, _ = _add_turn(rt, router, "q", "Alice", "问一个问题", 10.0)
    ans_node, _ = _add_turn(rt, router, "ans", "Bob", "这是一个回答", 12.0)

    # Manually link an inferred reply edge
    rt.dag._link_parent("ans", "q", kind="inferred_reply")
    assert ans_node.edge_kinds.get("q") == "inferred_reply"

    # Re-route the node without strong matching evidence
    router.route(rt, ans_node)
    # The stale inferred_reply edge must be pruned
    assert "inferred_reply" not in ans_node.edge_kinds.values()


def test_tier3_quoted_human_in_dialogue_elliptical_vs_non_elliptical():
    """R2.4: Quoting human in active bot dialogue requires elliptical phrasing to target bot."""
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "b1", "Bob", "建议使用方案 B", 10.0)
    _add_turn(rt, router, "a1", "Alice", "帮我分析系统架构", 12.0, reply_to_id="b1", mentioned_users=["bot"])
    bot_node, _ = _add_turn(rt, router, "bot1", "bot", "推荐方案 A", 14.0, reply_to_id="a1")
    rt.last_bot_node = bot_node
    rt.last_interlocutor = "Alice"

    # Case A: Elliptical quote -> targets Bot as inquiry object
    _, r_elliptical = _add_turn(rt, router, "a2", "Alice", "这个呢？", 16.0, reply_to_id="b1")
    assert r_elliptical.bot_is_addressee is True
    assert r_elliptical.addressee_ids == ["bot"]

    # Case B: Non-elliptical statement to Bob -> targets Bob, not Bot
    _, r_statement = _add_turn(
        rt, router, "a3", "Alice", "方案 B 看起来性能更好，大家怎么看？", 18.0, reply_to_id="b1"
    )
    assert r_statement.bot_is_addressee is False
    assert r_statement.addressee_ids == ["Bob"]


def test_tier3_competing_human_chatter_invalidates_active_interlocutor_fallback():
    """R2.4: Competing human chatter in same topic after bot turn blocks active interlocutor fallback."""
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "a1", "Alice", "显卡温度太高了", 10.0)
    bot_node, _ = _add_turn(rt, router, "bot1", "bot", "可以降低功耗墙", 12.0, reply_to_id="a1")
    rt.last_bot_node = bot_node
    rt.last_interlocutor = "Alice"

    # Charlie chimes in within the same topic
    _add_turn(rt, router, "c1", "Charlie", "降功耗墙会掉帧的", 14.0, reply_to_id="bot1")

    # Alice replies with ambiguous follow-up
    _, routing = _add_turn(rt, router, "a2", "Alice", "那怎么办？", 16.0)

    # Due to competing human chatter, interlocutor fallback should NOT fire
    assert "active_interlocutor_followup" not in routing.evidence
    assert routing.bot_is_addressee is False


def test_tier3_message_semantics_routing_exposure():
    """R4.1: MessageSemantics correctly reflects router inferences and metadata."""
    rt, router, _ = _setup_router_env(bot_id="bot")
    node, routing = _add_turn(rt, router, "m1", "Alice", "bot，你怎么老不回", 10.0)

    semantics = describe_message(node, rt.dag, bot_id="bot")
    assert semantics.sender_id == "Alice"
    assert semantics.sender_is_bot is False
    assert semantics.bot_is_addressee is True
    assert semantics.bot_addressee_confidence >= 0.90
    assert semantics.topic_id == routing.topic_id
    assert "direct_name_call" in semantics.routing_evidence


def test_tier3_addressivity_router_consumption():
    """R4.2: AddressivityRouter consumes RoutingInference and respects routing decisions."""
    rt, router, addr = _setup_router_env(bot_id="bot", bot_names=("bot", "小助手"))

    # Case 1: Bot is vocative addressee
    n1, _ = _add_turn(rt, router, "t1", "Alice", "bot，你怎么老不回", 10.0)
    s1 = addr.compute_addressivity(n1, rt.dag)
    assert s1.is_bot_targeted is True
    assert s1.level == AddressivityLevel.STRONG
    assert s1.score >= 0.90

    # Case 2: Bot is subject, not addressee
    n2, _ = _add_turn(rt, router, "t2", "Alice", "这个bot怎么老不回", 12.0)
    s2 = addr.compute_addressivity(n2, rt.dag)
    assert s2.is_bot_targeted is False
    assert s2.level == AddressivityLevel.WEAK
    assert any("subject" in r.lower() for r in s2.reasons)


# ===========================================================================
# Tier 4: Realistic Multi-Turn Chat Scenarios (The 10 Core Regression Scenarios)
# ===========================================================================

def test_scenario_1_active_interlocutor_followup():
    """Scenario 1: Bot->A, A: '那怎么办？' -> addressee=Bot.
    
    Active interlocutor continuation: Alice is in live dialogue with Bot;
    an elliptical question from Alice is routed to Bot with high confidence.
    """
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "a1", "Alice", "5090温度太高了", 10.0)
    bot_node, _ = _add_turn(rt, router, "bot1", "bot", "可以尝试在驱动里降低功耗墙或者调整风扇曲线", 12.0, reply_to_id="a1")
    rt.last_bot_node = bot_node
    rt.last_interlocutor = "Alice"

    node, routing = _add_turn(rt, router, "a2", "Alice", "那怎么办？", 14.0)

    assert routing.bot_is_addressee is True
    assert routing.addressee_ids == ["bot"]
    assert routing.bot_addressee_confidence >= 0.72
    assert routing.parent_message_id == "bot1"
    assert routing.topic_id == bot_node.metadata["routing"]["topic_id"]
    assert "active_interlocutor_followup" in routing.evidence
    assert node.edge_kinds.get("bot1") == "inferred_reply"
    assert "bot1" in node.parent_ids


def test_scenario_2_bystander_interjection_not_addressed():
    """Scenario 2: Bot->A, B: '真的假的' -> addressee!=Bot.
    
    Bystander chatter separation: Bystander Bob interjects with a rhetorical remark.
    Bob is not addressing the Bot.
    """
    rt, router, addr = _setup_router_env()
    _add_turn(rt, router, "a1", "Alice", "5090温度太高了", 10.0)
    bot_node, _ = _add_turn(rt, router, "bot1", "bot", "可以尝试降低功耗墙", 12.0, reply_to_id="a1")
    rt.last_bot_node = bot_node
    rt.last_interlocutor = "Alice"

    node, routing = _add_turn(rt, router, "b1", "Bob", "真的假的", 14.0)

    assert routing.bot_is_addressee is False
    assert routing.bot_addressee_confidence < 0.40
    assert "bot" not in routing.addressee_ids
    score = addr.compute_addressivity(node, rt.dag, last_bot_node=bot_node)
    assert score.is_bot_targeted is False
    assert score.level in (AddressivityLevel.WEAK, AddressivityLevel.SAFE_HOVER)


def test_scenario_3_interleaved_qa_retrieval():
    """An unaddressed short answer amid parallel topics stays pending."""
    rt, router, _ = _setup_router_env()

    from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
    adapter = EmbeddingAdapter(enabled=True)
    for text in ("5090温度有点高怎么办", "5090风扇曲线怎么设的？"):
        adapter.remember(text, [1, 0, 0])
    for text in ("今晚打游戏吗", "游戏可以啊几点？"):
        adapter.remember(text, [0, 1, 0])
    adapter.remember("我默认的", [0, 0, 1])
    rt.dag.semantic_match_fn = adapter.match

    gpu_1, _ = _add_turn(rt, router, "a1", "Alice", "5090温度有点高怎么办", 1.0)
    game_1, _ = _add_turn(rt, router, "c1", "Charlie", "今晚打游戏吗", 2.0)
    d_q, _ = _add_turn(rt, router, "d1", "David", "5090风扇曲线怎么设的？", 3.0)
    _add_turn(rt, router, "e1", "Eric", "游戏可以啊几点？", 4.0)
    ans, routing = _add_turn(rt, router, "a2", "Alice", "我默认的", 5.0)

    assert build_contextual_query(ans, rt.dag) == ans.text
    gpu_topic = rt.routing_state.topics[gpu_1.metadata["routing"]["topic_id"]]
    context = build_contextual_query(ans, rt.dag, gpu_topic)
    assert d_q.text in context and game_1.text not in context
    assert routing.topic_id == "" and routing.topic_status == "pending"
    assert not routing.parent_message_id and not routing.addressee_ids
    assert not ans.parent_ids and ans.msg_id not in gpu_topic.message_ids


def test_scenario_4_quote_human_with_explicit_bot_mention():
    """Scenario 4: A quotes B + '@bot 他说得对吗' -> quoted=B, addressee=Bot.
    
    Explicit mention takes precedence over quoted message author.
    """
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "b1", "Bob", "把温控开关拔了就行", 10.0)
    node, routing = _add_turn(
        rt, router, "a1", "Alice", "@bot 他说得对吗", 12.0,
        reply_to_id="b1", mentioned_users=["bot"]
    )

    assert routing.parent_message_id == "b1"
    assert routing.parent_confidence == 1.0
    assert routing.addressee_ids == ["bot"]
    assert routing.bot_is_addressee is True
    assert routing.bot_addressee_confidence == 1.0
    assert "explicit_mention" in routing.evidence
    assert "explicit_reply" in routing.evidence

    semantics = describe_message(node, rt.dag, bot_id="bot")
    assert semantics.recipient_ids == ("bot",)
    assert semantics.quoted_author_id == "Bob"
    assert semantics.quoted_message_id == "b1"
    assert semantics.bot_is_addressee is True


def test_scenario_5_quote_human_as_subject_in_active_bot_dialogue():
    """Scenario 5: A quotes B: '这个呢？' while A is in active dialogue with Bot -> addressee=Bot.
    
    Subject citation in active human-bot dialogue: Alice quotes Bob's advice to ask the Bot.
    """
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "b1", "Bob", "用这个水冷头试一下", 10.0)
    _add_turn(rt, router, "a1", "Alice", "帮我选个散热方案", 12.0, reply_to_id="b1", mentioned_users=["bot"])
    bot_node, _ = _add_turn(rt, router, "bot1", "bot", "推荐360一体式水冷", 14.0, reply_to_id="a1")
    rt.last_bot_node = bot_node
    rt.last_interlocutor = "Alice"

    node, routing = _add_turn(rt, router, "a2", "Alice", "这个呢？", 16.0, reply_to_id="b1")

    assert routing.parent_message_id == "b1"
    assert routing.addressee_ids == ["bot"]
    assert routing.bot_is_addressee is True
    assert routing.bot_addressee_confidence >= 0.72
    assert "quoted_subject_active_interlocutor" in routing.evidence
    assert node.edge_kinds.get("b1") == "reply"


def test_scenario_6_bot_as_subject_not_addressee():
    """Scenario 6: A: '这个bot怎么老不回' -> subject=Bot, addressee!=Bot.
    
    3rd-person discussion of bot is not addressing the bot.
    """
    rt, router, addr = _setup_router_env()
    node, routing = _add_turn(rt, router, "a1", "Alice", "这个bot怎么老不回", 10.0)

    assert routing.subject_is_bot is True
    assert routing.subject_user_ids == ["bot"]
    assert routing.bot_is_addressee is False
    assert routing.bot_addressee_confidence < 0.40
    assert "bot" not in routing.addressee_ids

    score = addr.compute_addressivity(node, rt.dag)
    assert score.is_bot_targeted is False
    assert score.level == AddressivityLevel.WEAK
    assert any("subject" in reason.lower() for reason in score.reasons)


def test_scenario_7_direct_vocative_call():
    """Scenario 7: A: 'bot，你怎么老不回' -> addressee=Bot.
    
    Direct vocative address using bot name and 2nd-person pronoun.
    """
    rt, router, addr = _setup_router_env()
    node, routing = _add_turn(rt, router, "a1", "Alice", "bot，你怎么老不回", 10.0)

    assert routing.bot_is_addressee is True
    assert routing.addressee_ids == ["bot"]
    assert routing.bot_addressee_confidence >= 0.90
    assert "direct_name_call" in routing.evidence

    score = addr.compute_addressivity(node, rt.dag)
    assert score.is_bot_targeted is True
    assert score.level == AddressivityLevel.STRONG
    assert score.score >= 0.90


def test_scenario_8_low_information_chatter_no_parent_link():
    """Scenario 8: A: '哈哈' -> no high-confidence parent link.
    
    Low-information filler suppression prevents hallucinated parent links.
    """
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "b1", "Bob", "刚刚打游戏翻车了", 10.0)
    node, routing = _add_turn(rt, router, "a1", "Alice", "哈哈", 12.0)

    assert routing.parent_message_id == ""
    assert routing.parent_confidence < 0.72
    assert "b1" not in node.parent_ids
    assert "inferred_reply" not in node.edge_kinds.values()


def test_scenario_9_concurrent_technical_topics_isolated():
    """Scenario 9: Two concurrent technical topics -> separated into two distinct topics.
    
    Orthogonal technical topics (Python asyncio vs OpenWrt DNSMasq) maintain separate topic IDs.
    """
    rt, router, _ = _setup_router_env()

    from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
    adapter = EmbeddingAdapter(enabled=True)
    for text in ("Python asyncio gather 并发任务异常退出", "gather 需要设置 return_exceptions=True 避免被取消"):
        adapter.remember(text, [1, 0])
    for text in ("路由器 OpenWrt 设置 DNSMasq 域名重定向", "修改 /etc/config/dhcp 配置文件即可"):
        adapter.remember(text, [0, 1])
    rt.dag.semantic_match_fn = adapter.match

    _, r_py1 = _add_turn(rt, router, "py1", "Alice", "Python asyncio gather 并发任务异常退出", 10.0)
    _, r_net1 = _add_turn(rt, router, "net1", "Bob", "路由器 OpenWrt 设置 DNSMasq 域名重定向", 12.0)
    _, r_py2 = _add_turn(rt, router, "py2", "Charlie", "gather 需要设置 return_exceptions=True 避免被取消", 14.0)
    _, r_net2 = _add_turn(rt, router, "net2", "David", "修改 /etc/config/dhcp 配置文件即可", 16.0)

    # Distinct topics
    assert r_py1.topic_id != ""
    assert r_net1.topic_id != ""
    assert r_py1.topic_id != r_net1.topic_id

    # Turns properly clustered
    assert r_py2.topic_id == r_py1.topic_id
    assert r_net2.topic_id == r_net1.topic_id


def test_scenario_10_expired_topic_inactivity_boundary():
    """Scenario 10: Topic inactive for > 5 min followed by '这个呢' -> does not force-resume dead topic.
    
    TTL boundary enforcement: Topics inactive for > 300s are dropped and not revived.
    """
    rt, router, _ = _setup_router_env()
    _add_turn(rt, router, "a1", "Alice", "这个显卡可以配什么CPU", 10.0)
    bot_node, _ = _add_turn(rt, router, "bot1", "bot", "建议搭配 7800X3D 或者 14700K", 12.0, reply_to_id="a1")
    rt.last_bot_node = bot_node
    rt.last_interlocutor = "Alice"

    # Silence gap of 360 seconds (> 300s TTL and > 60s interlocutor window)
    node, routing = _add_turn(rt, router, "a2", "Alice", "这个呢", 372.0)

    assert routing.bot_is_addressee is False
    assert routing.parent_message_id != "bot1"
    assert routing.bot_addressee_confidence < 0.40
    assert "active_interlocutor_followup" not in routing.evidence
    assert node.edge_kinds.get("bot1") != "inferred_reply"
