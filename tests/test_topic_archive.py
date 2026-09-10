"""Archive retrieval must stay conservative, bounded and session isolated."""
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG, ConversationNode
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime, TopicState
from astrbot_plugin_chat_dynamics.core.topic_archive import TopicArchive
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter


TEXT = "graphics card power supply voltage settings"


def add(archive, dag, key="old", text=TEXT, timestamp=10):
    dag.add_message(key, "alice", text, timestamp=timestamp)
    return archive.archive(TopicState(key, [key], {"alice"}, timestamp), dag, timestamp + 301)


def test_reopens_original_id_after_dag_eviction_without_mutating_graph():
    archive, dag = TopicArchive(), ConversationDAG()
    add(archive, dag)
    dag.nodes.clear()
    result = archive.retrieve(ConversationNode("new", "bob", TEXT, 600), dag)
    assert result and result.topic_id == "old"
    assert not dag.nodes
    assert result.topic.participants == frozenset({"alice"})


def test_short_or_unrelated_turns_do_not_reopen_even_with_high_semantic_score():
    archive, dag = TopicArchive(), ConversationDAG()
    add(archive, dag)
    dag.semantic_match_fn = lambda *_: SimpleNamespace(score=1.0)
    for text in ("ok", "this one?", "network router firewall configuration issue"):
        assert archive.retrieve(ConversationNode("new", "alice", text, 600), dag) is None


def test_competing_summaries_require_margin():
    archive, dag = TopicArchive(), ConversationDAG()
    add(archive, dag)
    add(archive, dag, "other")
    assert archive.retrieve(ConversationNode("new", "alice", TEXT, 600), dag) is None


def test_bounds_ttl_and_session_isolation():
    archive, dag = TopicArchive(max_topics=2, ttl_seconds=1000), ConversationDAG()
    for index in range(3):
        add(archive, dag, str(index), timestamp=10 + index)
    assert set(archive.entries) == {"1", "2"}
    assert not TopicArchive().entries
    archive.prune(1012)
    assert set(archive.entries) == {"2"}
    archive.prune(1013)
    assert not archive.entries


def test_pop_clear_and_summary_size():
    archive, dag = TopicArchive(), ConversationDAG()
    entry = add(archive, dag, text=TEXT * 100)
    assert len(entry.summary) <= 320
    assert archive.pop("old") is entry
    add(archive, dag)
    archive.clear()
    assert not archive.entries


def test_nonfinite_match_is_ignored():
    archive, dag = TopicArchive(), ConversationDAG()
    add(archive, dag)
    dag.semantic_match_fn = lambda *_: SimpleNamespace(score=float("nan"))
    assert archive.retrieve(ConversationNode("new", "alice", TEXT, 600), dag) is None


def test_evicted_topic_uses_cached_text_not_exemplar_message_ids():
    archive, dag = TopicArchive(), ConversationDAG()
    topic = TopicState("old", ["message-id-with-several-tokens"], {"alice"}, 10)
    topic.exemplar_messages = [("message-id-with-several-tokens", 0.9)]
    assert archive.archive(topic, dag, 400) is None
    topic.summary_excerpts = [TEXT]
    archived = archive.archive(topic, dag, 400)
    assert archived and archived.summary == TEXT
    assert "message-id" not in archived.summary


def test_router_prunes_and_reopens_old_topic_without_inferred_parent():
    dag = ConversationDAG(session_id="room")
    runtime = SessionRuntime(session_key="room", group_id="room", umo="test:GroupMessage:room",
                             bot_id="bot", dag=dag)
    router = ThreadRouter(require_intense_dialogue=False)
    old = dag.add_message("old", "alice", TEXT, timestamp=10)
    old_result = router.route(runtime, old)
    new = dag.add_message("new", "bob", TEXT, timestamp=600)
    result = router.route(runtime, new)
    assert result.topic_id == old_result.topic_id == "old"
    assert "topic_reopen" in result.evidence
    assert not result.parent_message_id
    assert not new.parent_ids
    assert not result.bot_is_addressee
    assert "old" in runtime.routing_state.topics
    assert "old" not in runtime.routing_state.archive.entries
