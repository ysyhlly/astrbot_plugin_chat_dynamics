"""A stored topic conclusion and the reasons recorded for it must agree.

Evidence explains the conclusion. A back-fill, burst or rerank that resolves a
pending topic has to drop the codes that justified the previous conclusion, or
replay, annotations and future weight fitting read a flag and a reason that
contradict each other.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.routing_contract import (
    commit_topic_evidence,
    topic_evidence_is_consistent,
)
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter, TopicResolver


ROOT = Path(__file__).resolve().parents[1]
STALE_TOPIC_CODES = {"topic_ambiguous", "topic_not_formed"}


@pytest.mark.parametrize("routing, expected", [
    # A resolved conclusion drops the codes that justified the pending one.
    ({"topic_ambiguous": False, "topic_status": "committed",
      "evidence": ["topic_ambiguous", "topic_profile", "topic_ambiguous"]},
     ["topic_profile"]),
    ({"topic_ambiguous": False, "topic_status": "committed",
      "evidence": ["explicit_mention", "topic_not_formed"]},
     ["explicit_mention"]),
    # Unresolved conclusions keep them.
    ({"topic_ambiguous": True, "topic_status": "pending",
      "evidence": ["topic_ambiguous", "topic_profile"]},
     ["topic_ambiguous", "topic_profile"]),
    ({"topic_ambiguous": True, "topic_status": "unformed",
      "evidence": ["topic_not_formed", "topic_ambiguous"]},
     ["topic_not_formed", "topic_ambiguous"]),
    # An unresolved status outranks a stale false flag.
    ({"topic_ambiguous": False, "topic_status": "pending",
      "evidence": ["topic_ambiguous"]},
     ["topic_ambiguous"]),
    ({"evidence": []}, []),
])
def test_commit_topic_evidence_only_drops_contradictions(routing, expected):
    assert commit_topic_evidence(routing) == expected
    assert commit_topic_evidence(routing, "topic_llm_rerank") == [*expected, "topic_llm_rerank"]
    # Re-committing a code that is already recorded changes nothing.
    if expected:
        assert commit_topic_evidence(routing, expected[0]) == expected


@pytest.mark.parametrize("routing, consistent", [
    ({"topic_ambiguous": False, "topic_status": "committed", "evidence": ["topic_profile"]}, True),
    ({"topic_ambiguous": False, "topic_status": "committed", "evidence": ["topic_ambiguous"]}, False),
    ({"topic_ambiguous": False, "topic_status": "committed", "evidence": ["topic_not_formed"]}, False),
    ({"topic_ambiguous": True, "topic_status": "pending", "evidence": ["topic_ambiguous"]}, True),
    ({"topic_ambiguous": False, "topic_status": "unformed", "evidence": ["topic_not_formed"]}, True),
    ({"topic_ambiguous": False, "evidence": []}, True),
])
def test_consistency_predicate(routing, consistent):
    assert topic_evidence_is_consistent(routing) is consistent


def assert_every_snapshot_is_consistent(runtime):
    for node in runtime.dag.nodes.values():
        routing = node.metadata.get("routing")
        if routing:
            assert topic_evidence_is_consistent(routing), (node.msg_id, routing)


def test_burst_backfill_clears_the_unformed_reason():
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG())
    # The default router only forms a topic from a real discussion burst, so the
    # first fragment of a merged turn stays unformed until the burst is proven.
    router = ThreadRouter()

    def add(mid, user, timestamp, turn, text):
        return runtime.dag.add_message(mid, user, text, timestamp=timestamp,
                                       metadata={"turn_id": turn, "topic_source_text": text})

    for i, timestamp in enumerate((98, 99, 105)):
        router.route(runtime, add(f"h{i}", "B" if i % 2 == 0 else "C", timestamp,
                                  f"history-{i}", "显卡风扇温度调节方案第%d个配置" % i))
    first = add("f0", "A", 100, "batch-A", "显卡风扇温度调节方案需要检查")
    last = add("f1", "A", 110, "batch-A", "显卡风扇温度调节方案需要改进")
    runtime.dag.link_related(last.msg_id, first.msg_id)
    assert router.route(runtime, first).topic_status == "unformed"
    assert "topic_not_formed" in first.metadata["routing"]["evidence"]
    router.route(runtime, last)
    routing = first.metadata["routing"]
    assert routing["topic_status"] == "committed" and not routing["topic_ambiguous"]
    assert "topic_burst_confirmed" in routing["evidence"]
    assert not STALE_TOPIC_CODES.intersection(routing["evidence"])
    assert_every_snapshot_is_consistent(runtime)


class PendingResolver(TopicResolver):
    def resolve(self, node, dag, state, matches, explicit_parent=None):
        if node.msg_id == "pending":
            return "a", .55, True, ["topic_ambiguous"], "b", [(.55, "a"), (.53, "b")]
        return ("b" if node.msg_id == "follow" else node.msg_id), .9, False, [], None, []


def pending_runtime():
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG())
    router = ThreadRouter(topic_resolver=PendingResolver(), require_intense_dialogue=False)
    for index, mid in enumerate(("a", "b", "pending"), 1):
        node = runtime.dag.add_message(mid, mid, "substantive topic " + mid, timestamp=index)
        router.route(runtime, node)
    return runtime, router


def test_pending_followup_clears_the_ambiguous_reason():
    runtime, router = pending_runtime()
    pending = runtime.dag.nodes["pending"]
    assert "topic_ambiguous" in pending.metadata["routing"]["evidence"]
    follow = runtime.dag.add_message("follow", "other", "explicit independent content", timestamp=4, reply_to_id="pending")
    router.route(runtime, follow)
    routing = pending.metadata["routing"]
    assert routing["topic_status"] == "committed" and routing["topic_id"] == "b"
    assert "pending_followup" in routing["evidence"]
    assert "topic_ambiguous" not in routing["evidence"]
    assert_every_snapshot_is_consistent(runtime)


@pytest.mark.asyncio
async def test_rerank_commit_clears_the_ambiguous_reason():
    runtime, router = pending_runtime()
    pending = runtime.dag.nodes["pending"]
    pending.metadata["routing"]["addressee_confidence"] = .95

    async def rerank(**kwargs):
        return SimpleNamespace(choice="topic", topic_id="a")

    await router.rerank_pending(runtime, pending, SimpleNamespace(rerank=rerank))
    routing = pending.metadata["routing"]
    assert routing["topic_id"] == "a" and not routing["topic_ambiguous"]
    assert routing["topic_status"] == "committed"
    assert "topic_llm_rerank" in routing["evidence"]
    assert "topic_ambiguous" not in routing["evidence"]
    # Recipient uncertainty stays explicit and independent of the topic commit.
    assert routing["addressee_ambiguous"] is False
    assert routing["ambiguous"] is False
    assert_every_snapshot_is_consistent(runtime)


@pytest.mark.parametrize("fixture", ["routing_golden.json", "routing_multiturn.json", "active_dialogue_golden.json"])
def test_replayed_fixtures_never_store_a_stale_topic_reason(fixture):
    cases = json.loads((ROOT / "tests" / "fixtures" / fixture).read_text(encoding="utf-8"))
    for case in cases:
        runtime = SessionRuntime("golden", "golden", "golden", bot_id="bot", dag=ConversationDAG())
        router = ThreadRouter(require_intense_dialogue=False)
        names = case.get("bot_names", ["群间"])
        for i, message in enumerate(case["messages"]):
            node = runtime.dag.add_message(str(i), message.get("user", "A"), message["text"],
                                           timestamp=1000.0 + i, mentioned_users=message.get("mentions", []),
                                           reply_to_id=message.get("reply_to"))
            router.route(runtime, node, bot_names=names)
            if node.user_id == "bot":
                runtime.last_bot_node = node
        assert_every_snapshot_is_consistent(runtime)
