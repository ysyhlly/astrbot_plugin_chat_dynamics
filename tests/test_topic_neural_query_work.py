"""Neural topic scoring does not compute discarded hashed query vectors."""
from astrbot_plugin_chat_dynamics.core import topic_resolution
from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import TopicState


def test_neural_scoring_skips_hashing_and_keeps_score(monkeypatch):
    adapter = EmbeddingAdapter(enabled=True)
    dag = ConversationDAG(semantic_match_fn=adapter.match)
    first = dag.add_message("a", "A", "GPU cooling temperature", timestamp=1)
    query = dag.add_message("b", "B", "GPU cooling advice", timestamp=2)
    for node in (first, query):
        adapter.remember(node.text, [1, 0])
    topic = TopicState("gpu", message_ids=["a"])
    resolver = topic_resolution.TopicResolver()
    expected = resolver.score_topic(query, dag, topic, {"a": .9})

    def unexpected_hash(text):
        raise AssertionError("Fully neural scoring must not hash discarded vectors")

    monkeypatch.setattr(topic_resolution, "hashed_embedding", unexpected_hash)
    assert resolver.score_topic(query, dag, topic, {"a": .9}) == expected
