"""Unit tests for Milestone M2: DAG Conversation Graph & Addressivity Router.

Validates:
1. DAG node insertion, parent-child edge linking via reply_to_id.
2. Multi-branch threaded context reconstruction up to max_depth/max_nodes.
3. Node pruning by TTL and maximum node capacity with bidirectional edge cleanup.
4. Addressivity scoring:
   - Strong direct address (@bot, name call, quote reply).
   - Exclusion logic (@other, reply to other).
   - Safe hover range (0.40 - 0.70) for topical continuations and temporal proximity.
   - Weak ambient chat classification.
"""

from __future__ import annotations

import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import (
    AddressivityLevel,
    AddressivityRouter,
)
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG


# ---------------------------------------------------------------------------
# DAG Graph Construction & Traversal Tests
# ---------------------------------------------------------------------------


def test_dag_reconciles_out_of_order_reply_and_rejects_cycles():
    dag = ConversationDAG(session_id="room")
    child = dag.add_message("child", "u2", "later", timestamp=2.0, reply_to_id="parent")
    assert child.parent_ids == set()

    parent = dag.add_message("parent", "u1", "earlier", timestamp=1.0)
    assert child.parent_ids == {"parent"}
    assert parent.child_ids == {"child"}
    assert dag.link_related("parent", "child") is False
    assert dag.link_related("parent", "parent") is False


def test_context_candidates_do_not_mix_unrelated_human_branch():
    dag = ConversationDAG(session_id="room")
    dag.add_message("other", "bob", "unrelated branch", timestamp=1.0)
    dag.add_message("bot", "bot", "prior answer", timestamp=2.0)
    dag.add_message("leaf", "alice", "new root", timestamp=3.0)

    ids = [node.msg_id for node in dag.get_context_for_message("leaf", bot_id="bot")]
    assert ids == ["bot", "leaf"]


def test_mention_edge_inherits_thread_id():
    """Mentions create mention edges for addressee resolution, but do NOT merge thread_id."""
    dag = ConversationDAG(session_id="room")
    alice = dag.add_message("a1", "alice", "我先说方案", timestamp=1.0)
    bob = dag.add_message(
        "b1", "bob", "这个可以", timestamp=2.0, mentioned_users=["alice"]
    )
    assert "a1" in bob.parent_ids
    assert bob.edge_kinds["a1"] == "mention"
    assert bob.thread_id != alice.thread_id
    assert bob.thread_id == "b1"


def test_semantic_similarity_does_not_implicitly_link():
    dag = ConversationDAG(session_id="room")
    first = dag.add_message("t1", "alice", "python asyncio gather 调度", timestamp=1.0)
    second = dag.add_message("t2", "bob", "asyncio gather 怎么用", timestamp=2.0)
    assert not second.parent_ids
    assert second.thread_id != first.thread_id


def test_thread_context_includes_sibling_reply_branch():
    dag = ConversationDAG(session_id="room")
    dag.add_message("root", "alice", "这个问题怎么处理", timestamp=1.0)
    dag.add_message("bot", "bot", "可以先看日志", timestamp=2.0, reply_to_id="root")
    dag.add_message("sib", "carol", "我这边也复现了", timestamp=3.0, reply_to_id="root")
    ids = [node.msg_id for node in dag.get_thread_context("bot")]
    assert ids == ["root", "bot", "sib"]


def test_pending_hover_followup_accumulates_addressivity():
    dag = ConversationDAG(session_id="room")
    bot = dag.add_message("bot", "bot", "这个方案可以继续优化", timestamp=1.0)
    first = dag.add_message("h1", "alice", "这个方案", timestamp=2.0)
    second = dag.add_message("h2", "alice", "这个方案然后呢", timestamp=3.0)
    router = AddressivityRouter(bot_id="bot")

    initial = router.compute_addressivity(first, dag, last_bot_node=bot)
    resolved = router.compute_addressivity(second, dag, last_bot_node=bot, prior_hover=first)

    assert initial.level == AddressivityLevel.SAFE_HOVER
    assert resolved.level == AddressivityLevel.STRONG
    assert resolved.topic_relevance > 0


def test_hover_queue_accumulates_two_fragments():
    dag = ConversationDAG(session_id="room")
    bot = dag.add_message("bot", "bot", "这个方案可以继续优化", timestamp=1.0)
    first = dag.add_message("h1", "alice", "这个方案", timestamp=2.0)
    second = dag.add_message("h2", "alice", "还能再快一点吗", timestamp=3.0)
    third = dag.add_message("h3", "alice", "这个方案然后呢", timestamp=4.0)
    router = AddressivityRouter(bot_id="bot")

    queued = router.compute_addressivity(
        third, dag, last_bot_node=bot, prior_hovers=[first, second]
    )
    assert queued.level == AddressivityLevel.STRONG

def test_dag_basic_node_addition():
    """Verify nodes are correctly stored and retrieved in chronological order."""
    dag = ConversationDAG(session_id="group_1", max_nodes=50)
    n1 = dag.add_message("m1", "user_a", "Hello world", timestamp=100.0)
    n2 = dag.add_message("m2", "user_b", "Hi user A", timestamp=101.0, reply_to_id="m1")

    assert dag.get_node("m1") == n1
    assert dag.get_node("m2") == n2
    assert "m1" in n2.parent_ids
    assert "m2" in n1.child_ids
    assert dag.chronological_ids == ["m1", "m2"]


def test_dag_thread_context_extraction():
    """Verify thread context traces backward along parent/reply links."""
    dag = ConversationDAG(session_id="group_1")
    # Linear thread: m1 -> m2 -> m3
    dag.add_message("m1", "user_a", "Question: How do I use Python?", timestamp=100.0)
    dag.add_message("m_unrelated", "user_c", "Lunch was tasty", timestamp=101.0)
    dag.add_message("m2", "bot_1", "You can install Python from python.org", timestamp=102.0, reply_to_id="m1")
    dag.add_message("m3", "user_a", "Thanks! How about pip?", timestamp=103.0, reply_to_id="m2")

    context = dag.get_thread_context("m3", max_depth=5)
    context_ids = [n.msg_id for n in context]

    # Must contain m1, m2, m3 in chronological order, and exclude m_unrelated
    assert context_ids == ["m1", "m2", "m3"]
    assert "m_unrelated" not in context_ids


def test_dag_pruning_by_capacity():
    """Verify exceeding max_nodes evicts oldest nodes and cleans edge references."""
    dag = ConversationDAG(session_id="group_1", max_nodes=3)
    dag.add_message("m1", "u1", "First", timestamp=1.0)
    dag.add_message("m2", "u2", "Second", timestamp=2.0, reply_to_id="m1")
    dag.add_message("m3", "u3", "Third", timestamp=3.0, reply_to_id="m2")

    assert len(dag.nodes) == 3
    assert dag.get_node("m1") is not None

    # Adding 4th node triggers pruning of m1
    dag.add_message("m4", "u4", "Fourth", timestamp=4.0)

    assert len(dag.nodes) == 3
    assert dag.get_node("m1") is None
    assert dag.get_node("m2") is not None
    # m2 parent reference to evicted m1 should be gone
    assert "m1" not in dag.get_node("m2").parent_ids


def test_dag_pruning_by_ttl():
    """Verify TTL eviction drops stale nodes."""
    dag = ConversationDAG(session_id="group_1", max_nodes=100, ttl_seconds=60.0)
    dag.add_message("m_old", "u1", "Old message", timestamp=100.0)
    dag.add_message("m_new", "u2", "Fresh message", timestamp=180.0)

    pruned = dag.prune(current_time=190.0)
    assert pruned == 1
    assert dag.get_node("m_old") is None
    assert dag.get_node("m_new") is not None


# ---------------------------------------------------------------------------
# Addressivity Router Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def addressivity_router():
    return AddressivityRouter(
        bot_id="bot_42",
        bot_names=["AstrBot", "小助手", "bot"],
        strong_threshold=0.70,
        hover_threshold=0.40,
    )


def test_addressivity_explicit_mention(addressivity_router):
    """Rule 1a: Explicit @mention of bot results in STRONG addressivity (1.0)."""
    dag = ConversationDAG()
    node = dag.add_message("m1", "user_1", "你好呀", timestamp=100.0, mentioned_users=["bot_42"])

    score = addressivity_router.compute_addressivity(node, dag)
    assert score.level == AddressivityLevel.STRONG
    assert score.score == 1.0
    assert score.is_bot_targeted is True
    assert score.target_user_id == "bot_42"


def test_addressivity_bot_name_invocation(addressivity_router):
    """Rule 1b: Message containing bot nickname results in STRONG addressivity."""
    dag = ConversationDAG()
    node = dag.add_message("m1", "user_1", "小助手，帮我看一下这个问题", timestamp=100.0)

    score = addressivity_router.compute_addressivity(node, dag)
    assert score.level == AddressivityLevel.STRONG
    assert score.score >= 0.90
    assert score.is_bot_targeted is True


def test_addressivity_explicit_quote_reply(addressivity_router):
    """Rule 1c: Direct quote-reply to a previous bot message is STRONG (0.98)."""
    dag = ConversationDAG()
    bot_msg = dag.add_message("bot_m1", "bot_42", "这是当前的架构设计文档。", timestamp=100.0)
    user_reply = dag.add_message("u_m2", "user_1", "第二点没太看懂", timestamp=105.0, reply_to_id="bot_m1")

    score = addressivity_router.compute_addressivity(user_reply, dag, last_bot_node=bot_msg)
    assert score.level == AddressivityLevel.STRONG
    assert score.score >= 0.95
    assert score.is_bot_targeted is True


def test_addressivity_explicit_exclusion_reply_to_human(addressivity_router):
    """Rule 1c/1d: Explicit reply to human user is WEAK even if right after bot message."""
    dag = ConversationDAG()
    bot_msg = dag.add_message("bot_m1", "bot_42", "明天天气很好。", timestamp=100.0)
    dag.add_message("human_m", "alice", "大家去聚餐吗？", timestamp=101.0)
    user_reply = dag.add_message("u_m2", "bob", "去啊，去哪家？", timestamp=102.0, reply_to_id="human_m")

    score = addressivity_router.compute_addressivity(user_reply, dag, last_bot_node=bot_msg)
    assert score.level == AddressivityLevel.WEAK
    assert score.score <= 0.20
    assert score.is_bot_targeted is False
    assert score.target_user_id == "alice"


def test_addressivity_safe_hover_continuation(addressivity_router):
    """Safe Hover: Message with follow-up cue & keyword overlap within 10s is buffered in SAFE_HOVER (0.4 - 0.7)."""
    dag = ConversationDAG()
    bot_msg = dag.add_message(
        "bot_m1", "bot_42",
        "Python 异步编程中可以使用 asyncio.gather 同时调度多个任务。",
        timestamp=100.0,
    )
    # User asks follow-up immediately without @bot or quote
    follow_up = dag.add_message(
        "u_m2", "user_1",
        "为什么不能用多线程呢？",
        timestamp=104.0,  # 4s later, immediate follow-up
    )

    score = addressivity_router.compute_addressivity(follow_up, dag, last_bot_node=bot_msg)
    # Should land in SAFE_HOVER range [0.40, 0.70)
    assert score.level == AddressivityLevel.SAFE_HOVER
    assert 0.40 <= score.score < 0.70
    assert score.is_bot_targeted is False  # Safe hover does NOT immediately trigger response


def test_addressivity_wake_without_at_is_not_strong(addressivity_router):
    dag = ConversationDAG()
    node = dag.add_message("m1", "user_1", "在吗", timestamp=100.0, metadata={"is_wake": True})
    score = addressivity_router.compute_addressivity(node, dag)
    assert score.level == AddressivityLevel.WEAK
    assert score.is_bot_targeted is False


def test_addressivity_ascii_substring_does_not_match_bot(addressivity_router):
    """'bot' must not match inside 'both' / 'robot'."""
    dag = ConversationDAG()
    node = dag.add_message("m1", "user_1", "I like both ideas and this robot vacuum", timestamp=100.0)
    score = addressivity_router.compute_addressivity(node, dag)
    assert score.level == AddressivityLevel.WEAK
    assert score.is_bot_targeted is False


def test_addressivity_at_token_is_strong(addressivity_router):
    dag = ConversationDAG()
    node = dag.add_message("m1", "user_1", "帮我看下 @AstrBot", timestamp=100.0, mentioned_users=["AstrBot"])
    score = addressivity_router.compute_addressivity(node, dag)
    assert score.level == AddressivityLevel.STRONG
    assert score.is_bot_targeted is True


def test_addressivity_weak_unrelated_ambient_chatter(addressivity_router):
    """Weak: Unrelated chatter after elapsed time is classified as WEAK."""
    dag = ConversationDAG()
    bot_msg = dag.add_message("bot_m1", "bot_42", "计算结果是 42。", timestamp=100.0)
    # Stale, unrelated chatter
    chatter = dag.add_message("u_m2", "user_99", "今晚吃牛肉火锅怎么样", timestamp=350.0)

    score = addressivity_router.compute_addressivity(chatter, dag, last_bot_node=bot_msg)
    assert score.level == AddressivityLevel.WEAK
    assert score.score < 0.40
    assert score.is_bot_targeted is False


def test_duplicate_message_id_is_idempotent():
    dag = ConversationDAG(session_id="g")
    original = dag.add_message("m1", "u1", "first", timestamp=1.0)
    child = dag.add_message("m2", "u2", "reply", timestamp=2.0, reply_to_id="m1")
    duplicate = dag.add_message("m1", "u1", "changed", timestamp=3.0)

    assert duplicate is original
    assert dag.chronological_ids == ["m1", "m2"]
    assert original.child_ids == {"m2"}
    assert child.parent_ids == {"m1"}
    assert original.text == "first"


def test_current_node_is_excluded_from_intervening_count(addressivity_router):
    dag = ConversationDAG()
    bot_msg = dag.add_message("bot_m1", "bot_42", "Python 异步编程可以用 asyncio。", timestamp=100.0)
    current = dag.add_message("u_m2", "user_1", "为什么呢", timestamp=104.0)

    score = addressivity_router.compute_addressivity(current, dag, last_bot_node=bot_msg)
    assert "Zero intervening messages since bot output" in score.reasons


def test_cjk_bot_name_requires_vocative_boundary(addressivity_router):
    addressivity_router.bot_names = {"助手"}
    dag = ConversationDAG()
    unrelated = dag.add_message("m1", "u1", "助手席很好", timestamp=1.0)
    directed = dag.add_message("m2", "u1", "助手帮我看看", timestamp=2.0)

    assert addressivity_router.compute_addressivity(unrelated, dag).level == AddressivityLevel.WEAK
    assert addressivity_router.compute_addressivity(directed, dag).level == AddressivityLevel.STRONG


# ---------------------------------------------------------------------------
# Milestone 1: Decoupling, Inferred Replies, and Thread Invariants
# ---------------------------------------------------------------------------


def test_legacy_semantic_methods_removed():
    """R1.1: ConversationDAG must not have maybe_link_semantic or _semantic_parent."""
    dag = ConversationDAG(session_id="test")
    assert hasattr(dag, "maybe_link_semantic") is False
    assert hasattr(dag, "_semantic_parent") is False
    # semantic_match_fn is retained for backward compatibility
    assert hasattr(dag, "semantic_match_fn") is True


def test_add_message_creates_zero_heuristic_edges():
    """R1.2: add_message creates zero heuristic/semantic edges for similar messages."""
    dag = ConversationDAG(session_id="test")
    m1 = dag.add_message("m1", "alice", "这段代码怎么挂了", timestamp=1.0)
    m2 = dag.add_message("m2", "bob", "Python 接口报错了", timestamp=2.0)

    assert not m2.parent_ids
    assert not m2.edge_kinds
    assert "semantic" not in m2.edge_kinds.values()
    assert "inferred_reply" not in m2.edge_kinds.values()
    assert m2.thread_id == "m2"
    assert m2.metadata.get("topic_id") == ""


def test_link_inferred_reply_lifecycle():
    """R1.4: link_inferred_reply successfully links parent, sets metadata, and adopts thread."""
    dag = ConversationDAG(session_id="test")
    p = dag.add_message("p1", "alice", "谁有最新的配置文件？", timestamp=1.0)
    c = dag.add_message("c1", "bob", "在这里，你可以参考这个。", timestamp=2.0)

    assert c.thread_id == "c1"
    ok = dag.link_inferred_reply("c1", "p1", confidence=0.88, reason="qa_fit")
    assert ok is True

    assert "p1" in c.parent_ids
    assert "c1" in p.child_ids
    assert c.edge_kinds["p1"] == "inferred_reply"
    assert c.thread_id == p.thread_id  # Downward thread adoption
    assert c.metadata["inferred_parent_id"] == "p1"
    assert c.metadata["inferred_parent_confidence"] == 0.88
    assert c.metadata["inferred_parent_reason"] == "qa_fit"


def test_link_inferred_reply_rejects_cycles():
    """R1.4: link_inferred_reply rejects direct and multi-hop cycle creation."""
    dag = ConversationDAG(session_id="test")
    dag.add_message("p1", "alice", "Root", timestamp=1.0)
    dag.add_message("c1", "bob", "Middle", timestamp=2.0, reply_to_id="p1")
    dag.add_message("leaf", "carol", "Leaf", timestamp=3.0, reply_to_id="c1")

    # Direct cycle: c1 -> p1 already exists (p1 is parent of c1), attempting p1 -> c1
    assert dag.link_inferred_reply("p1", "c1", confidence=0.9) is False
    # Multi-hop cycle: p1 -> leaf attempting to link p1 -> leaf
    assert dag.link_inferred_reply("p1", "leaf", confidence=0.9) is False
    # Mid-hop cycle: c1 -> leaf attempting to link c1 -> leaf
    assert dag.link_inferred_reply("c1", "leaf", confidence=0.9) is False


def test_link_inferred_reply_rejects_causal_inversion():
    """R1.4: link_inferred_reply rejects responses that precede candidate parents."""
    dag = ConversationDAG(session_id="test")
    p = dag.add_message("p1", "alice", "Parent", timestamp=10.0)
    c = dag.add_message("c1", "bob", "Earlier message", timestamp=5.0)

    assert dag.link_inferred_reply("c1", "p1", confidence=0.85) is False
    assert "p1" not in c.parent_ids


def test_link_inferred_reply_rejects_self_loop_and_invalid_nodes():
    """R1.4: link_inferred_reply validates IDs and node existence."""
    dag = ConversationDAG(session_id="test")
    dag.add_message("m1", "alice", "Msg", timestamp=1.0)

    assert dag.link_inferred_reply("m1", "m1", confidence=0.9) is False
    assert dag.link_inferred_reply("", "m1", confidence=0.9) is False
    assert dag.link_inferred_reply("m1", "nonexistent", confidence=0.9) is False
    assert dag.link_inferred_reply("m1", "m2", confidence=-0.1) is False
    assert dag.link_inferred_reply("m1", "m2", confidence=1.5) is False


def test_link_inferred_reply_platform_reply_priority():
    """R1.4: Platform explicit replies take priority over inferred reply links."""
    dag = ConversationDAG(session_id="test")
    dag.add_message("p1", "alice", "Explicit quote target", timestamp=1.0)
    dag.add_message("p2", "charlie", "Other question", timestamp=1.5)
    c = dag.add_message("c1", "bob", "Answer to p1", timestamp=2.0, reply_to_id="p1")

    # c1 already has explicit reply to p1; cannot link inferred reply to p2
    assert dag.link_inferred_reply("c1", "p2", confidence=0.95) is False
    assert "p2" not in c.parent_ids
    assert c.edge_kinds.get("p1") == "reply"


def test_link_inferred_reply_replaces_prior_inferred_parent():
    """R1.4: link_inferred_reply cleanly replaces prior inferred parent."""
    dag = ConversationDAG(session_id="test")
    p1 = dag.add_message("p1", "alice", "Q1", timestamp=1.0)
    p2 = dag.add_message("p2", "charlie", "Q2", timestamp=1.5)
    c = dag.add_message("c1", "bob", "Answer", timestamp=2.0)

    assert dag.link_inferred_reply("c1", "p1", confidence=0.75, reason="first_pass") is True
    assert "p1" in c.parent_ids
    assert "c1" in p1.child_ids
    assert c.thread_id == "p1"

    # Re-route to p2
    assert dag.link_inferred_reply("c1", "p2", confidence=0.92, reason="second_pass") is True
    assert "p1" not in c.parent_ids
    assert "c1" not in p1.child_ids
    assert "p2" in c.parent_ids
    assert "c1" in p2.child_ids
    assert c.edge_kinds["p2"] == "inferred_reply"
    assert c.thread_id == "p2"
    assert c.metadata["inferred_parent_id"] == "p2"


def test_unlink_inferred_reply_lifecycle():
    """R1.4: unlink_inferred_reply cleans up edge and resets thread_id."""
    dag = ConversationDAG(session_id="test")
    p = dag.add_message("p1", "alice", "Q1", timestamp=1.0)
    c = dag.add_message("c1", "bob", "Answer", timestamp=2.0)

    dag.link_inferred_reply("c1", "p1", confidence=0.8, reason="initial")
    assert c.thread_id == "p1"

    # Targeted unlink
    assert dag.unlink_inferred_reply("c1", parent_id="p1") is True
    assert "p1" not in c.parent_ids
    assert "c1" not in p.child_ids
    assert "p1" not in c.edge_kinds
    assert "inferred_parent_id" not in c.metadata
    assert c.thread_id == "c1"

    # Unlink when no inferred edge exists returns False
    assert dag.unlink_inferred_reply("c1", parent_id="p1") is False


def test_topic_id_metadata_does_not_mutate_thread_id():
    """R1.3: Setting topic_id in node.metadata does not alter node.thread_id."""
    dag = ConversationDAG(session_id="test")
    node = dag.add_message("m1", "alice", "Starting discussion", timestamp=1.0)
    assert node.thread_id == "m1"

    # Update macro topic state in metadata
    node.metadata["topic_id"] = "topic_general_arch"
    assert node.thread_id == "m1"
    assert node.metadata["topic_id"] == "topic_general_arch"


def test_adopt_thread_downward_dfs_prevents_parent_and_sibling_corruption():
    """R1.3: _adopt_thread downward DFS does not corrupt ancestor or sibling thread IDs."""
    dag = ConversationDAG(session_id="test")
    p = dag.add_message("P", "alice", "Parent", timestamp=1.0)
    c1 = dag.add_message("C1", "bob", "Child 1", timestamp=2.0, reply_to_id="P")
    c2 = dag.add_message("C2", "carol", "Child 2", timestamp=3.0, reply_to_id="P")
    q = dag.add_message("Q", "david", "Other Root", timestamp=4.0)

    assert p.thread_id == "P"
    assert c1.thread_id == "P"
    assert c2.thread_id == "P"

    # Adopt C1 into Q's thread
    dag._adopt_thread("C1", q.thread_id)

    # C1 adopts Q, but P and C2 remain in P
    assert c1.thread_id == "Q"
    assert p.thread_id == "P"
    assert c2.thread_id == "P"

