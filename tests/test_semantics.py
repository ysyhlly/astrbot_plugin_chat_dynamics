"""Hashed embedding + concept classifier tests."""

from __future__ import annotations

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.semantics import (
    classify_message,
    hashed_embedding,
    semantic_match,
)


def test_hashed_embedding_is_deterministic_and_normalized():
    first = hashed_embedding("这个 Python 接口报错了")
    second = hashed_embedding("这个 Python 接口报错了")
    assert first == second
    assert len(first) == 64
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def test_paraphrase_is_closer_than_unrelated_chatter():
    tech_a = "这段代码怎么挂了"
    tech_b = "Python 接口报错了"
    food = "今晚吃牛肉火锅怎么样"
    paraphrase = semantic_match(tech_a, tech_b)
    unrelated = semantic_match(tech_a, food)
    assert paraphrase.score > unrelated.score
    assert paraphrase.should_link() is True
    assert unrelated.should_link() is False
    assert "technical_help" in paraphrase.shared_scenes


def test_concept_classifier_labels_scene_and_emotion():
    scenes, emotions = classify_message("这个 Python 接口报错了，好崩溃")
    assert "technical_help" in scenes
    assert "negative" in emotions


def test_semantic_paraphrase_requires_explicit_candidate_retrieval():
    dag = ConversationDAG(session_id="room")
    first = dag.add_message("t1", "alice", "这段代码怎么挂了", timestamp=1.0)
    second = dag.add_message("t2", "bob", "Python 接口报错了", timestamp=2.0)
    # add_message() creates zero semantic edges directly
    assert not second.parent_ids
    assert not second.edge_kinds
    assert second.thread_id != first.thread_id
    assert second.thread_id == "t2"

    # Explicit candidate promotion via link_inferred_reply()
    assert dag.link_inferred_reply(second.msg_id, first.msg_id, confidence=0.85, reason="semantic_paraphrase") is True
    assert "t1" in second.parent_ids
    assert second.edge_kinds["t1"] == "inferred_reply"
    assert second.thread_id == first.thread_id
    assert second.metadata["inferred_parent_id"] == "t1"
    assert second.metadata["inferred_parent_confidence"] == 0.85
    assert second.metadata["inferred_parent_reason"] == "semantic_paraphrase"
    assert "t2" in first.child_ids
