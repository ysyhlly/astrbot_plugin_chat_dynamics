from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState, TopicState
from astrbot_plugin_chat_dynamics.core.topic_resolution import TopicResolver, build_contextual_query


def test_profile_rebuild_excludes_fillers_and_reassigned_message():
    dag = ConversationDAG()
    dag.add_message('a', 'A', 'GPU cooling fan temperature', timestamp=1)
    dag.add_message('b', 'B', '好的', timestamp=2)
    topic = TopicState('t', message_ids=['a', 'b'])
    TopicResolver.rebuild_profile(topic, dag)
    assert topic.centroid_vector and topic.centroid_space == 'hashed'
    assert [mid for mid, _ in topic.exemplar_messages] == ['a']
    topic.message_ids = ['b']
    TopicResolver.rebuild_profile(topic, dag)
    assert topic.centroid_vector is None
    assert topic.exemplar_messages == []
    assert topic.summary_excerpts == []


def test_candidate_context_does_not_borrow_unrelated_speaker_history():
    dag = ConversationDAG()
    dag.add_message('a', 'A', 'GPU cooling fan temperature', timestamp=1)
    dag.add_message('b', 'A', 'Tonight game schedule', timestamp=2)
    node = dag.add_message('c', 'A', '那这个呢', timestamp=3)
    assert build_contextual_query(node, dag) == node.text
    query = build_contextual_query(node, dag, TopicState('gpu', message_ids=['a']))
    assert 'GPU cooling' in query and 'Tonight' not in query


def test_elliptical_similarity_cannot_self_confirm_from_context():
    dag = ConversationDAG()
    dag.add_message('a', 'A', 'GPU cooling fan temperature', timestamp=1)
    node = dag.add_message('b', 'A', '这个呢', timestamp=2)
    topic = TopicState('t', message_ids=['a'])
    assert TopicResolver().score_topic(node, dag, topic, {'a': 0.0}) < 0.2


def test_quote_can_start_new_topic_without_losing_reply():
    dag = ConversationDAG()
    parent = dag.add_message('a', 'A', 'GPU cooling fan temperature', timestamp=1)
    parent.metadata['routing'] = {'topic_id': 'gpu'}
    node = dag.add_message('b', 'B', '另外，明天去海边旅行带什么？', timestamp=2, reply_to_id='a')
    state = RoutingState(topics={'gpu': TopicState('gpu', message_ids=['a'])})
    result = TopicResolver().resolve(node, dag, state, {'a': 0.0}, parent)
    assert result[0] == 'b' and 'topic_boundary' in result[3]
    assert node.reply_to_id == 'a'


def test_short_quote_inherits_and_known_interlocutor_scores_higher():
    dag = ConversationDAG()
    parent = dag.add_message('a', 'B', 'GPU cooling fan temperature', timestamp=1)
    parent.metadata['routing'] = {'topic_id': 'gpu', 'addressee_ids': ['A']}
    node = dag.add_message('b', 'A', '那这个呢', timestamp=2, reply_to_id='a')
    topic = TopicState('gpu', message_ids=['a'])
    resolver = TopicResolver()
    assert resolver.resolve(node, dag, RoutingState(topics={'gpu': topic}), {'a': 0}, parent)[0] == 'gpu'
    known = resolver.score_topic(node, dag, topic, {'a': 0})
    parent.metadata['routing']['addressee_ids'] = []
    node.reply_to_id = None
    unknown = resolver.score_topic(node, dag, topic, {'a': 0})
    assert known > unknown


def test_pending_parent_is_not_a_topic_anchor():
    dag = ConversationDAG()
    parent = dag.add_message('a', 'A', '这个呢', timestamp=1)
    parent.metadata['routing'] = {'topic_id': '', 'topic_status': 'pending'}
    node = dag.add_message('b', 'B', '那这个呢', timestamp=2, reply_to_id='a')
    result = TopicResolver().resolve(node, dag, RoutingState(), {'a': 1}, parent)
    assert result[0] == 'b'
    assert 'topic_reply_continuation' not in result[3]


def test_profile_scoring_excludes_future_and_expired_turns():
    dag = ConversationDAG()
    dag.add_message('old', 'A', 'GPU cooling fan temperature', timestamp=1)
    node = dag.add_message('now', 'A', 'GPU cooling fan temperature', timestamp=400)
    dag.add_message('future', 'A', 'GPU cooling fan temperature', timestamp=500)
    topic = TopicState('gpu', message_ids=['old', 'now', 'future'])
    assert TopicResolver().score_topic(node, dag, topic, {'old': 1, 'now': 1, 'future': 1}) == 0
    assert topic.updated_at == 500
    assert topic.recent_message_ids == ['now', 'future']


def test_neural_centroid_requires_complete_same_dimension_cache():
    from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
    adapter = EmbeddingAdapter(enabled=True, provider_id='test-model')
    dag = ConversationDAG(semantic_match_fn=adapter.match)
    dag.add_message('a', 'A', 'The graphics card is overheating', timestamp=1)
    dag.add_message('b', 'B', 'Reduce GPU temperature', timestamp=2)
    node = dag.add_message('c', 'C', '显卡散热怎么办', timestamp=3)
    topic = TopicState('gpu', message_ids=['a', 'b'])
    resolver = TopicResolver()
    for text in (dag.nodes['a'].text, dag.nodes['b'].text, node.text):
        adapter.remember(text, [1, 0])
    score = resolver.score_topic(node, dag, topic, {'a': .95, 'b': .95})
    assert topic.centroid_space.startswith('neural:test-model:')
    assert score >= resolver.join_threshold
    adapter.remember(dag.nodes['b'].text, [1, 0, 0])
    resolver.score_topic(node, dag, topic, {'a': .95, 'b': .95})
    assert topic.centroid_space == 'hashed'
    adapter.configure(provider_id='replacement-model')
    resolver.score_topic(node, dag, topic, {'a': 0, 'b': 0})
    assert topic.centroid_space == 'hashed'


def test_unchanged_profile_reuses_vectors_across_query_timestamps(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import topic_resolution
    calls = []
    original = topic_resolution.hashed_embedding

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(topic_resolution, 'hashed_embedding', counted)
    dag = ConversationDAG()
    dag.add_message('a', 'A', 'GPU cooling fan temperature', timestamp=1)
    first = dag.add_message('b', 'B', 'GPU cooling advice', timestamp=2)
    second = dag.add_message('c', 'B', 'GPU cooling advice', timestamp=3)
    topic = TopicState('gpu', message_ids=['a'])
    resolver = TopicResolver()
    resolver.score_topic(first, dag, topic, {'a': 1})
    resolver.score_topic(second, dag, topic, {'a': 1})
    assert calls.count(dag.nodes['a'].text) == 1
    assert len(topic._profile_cache) <= 2


def test_remember_rebuilds_only_reassigned_topics(monkeypatch):
    dag = ConversationDAG()
    node = dag.add_message('a', 'A', 'GPU cooling fan temperature', timestamp=1)
    state = RoutingState(topics={
        'old': TopicState('old', message_ids=['a']),
        'untouched': TopicState('untouched'),
    })
    calls = []
    original = TopicResolver.rebuild_profile

    def counted(topic, *args, **kwargs):
        calls.append(topic.topic_id)
        return original(topic, *args, **kwargs)

    monkeypatch.setattr(TopicResolver, 'rebuild_profile', staticmethod(counted))
    TopicResolver.remember(state, node, 'new', dag)
    assert calls == ['old', 'new']
    assert state.topics['old'].participants == set()
    assert state.topics['new'].participants == {'A'}


def test_profile_cache_tracks_content_routing_and_eviction():
    dag = ConversationDAG()
    parent = dag.add_message('parent', 'P', 'GPU cooling temperature', timestamp=1)
    node = dag.add_message('a', 'A', 'Reduce fan temperature', timestamp=2)
    topic = TopicState('gpu', message_ids=['a'])
    TopicResolver.rebuild_profile(topic, dag)
    node.reply_to_id = 'parent'
    node.metadata['routing'] = {'addressee_ids': ['B']}
    node.text = 'Travel itinerary planning'
    TopicResolver.rebuild_profile(topic, dag)
    assert topic.interlocutor_affinity == {('A', 'P'): 1, ('A', 'B'): 1}
    assert topic.label == 'Travel itinerary planning'
    parent.user_id = 'Q'
    TopicResolver.rebuild_profile(topic, dag)
    assert ('A', 'Q') in topic.interlocutor_affinity
    del dag.nodes['a']
    TopicResolver.rebuild_profile(topic, dag)
    assert topic.participants == set()
    assert topic.centroid_vector is None
    assert topic.summary_excerpts == []


def test_cached_historical_view_expires_without_rewinding_public_profile():
    dag = ConversationDAG()
    dag.add_message('a', 'A', 'GPU cooling temperature', timestamp=1)
    future = dag.add_message('future', 'B', 'GPU cooling advice', timestamp=500)
    query = dag.add_message('query', 'A', 'GPU cooling temperature', timestamp=2)
    topic = TopicState('gpu', message_ids=['a', 'future'])
    resolver = TopicResolver()
    initial = resolver.score_topic(query, dag, topic, {'a': 1, 'future': 1})
    assert initial > 0
    assert resolver.score_topic(query, dag, topic, {'a': 1, 'future': 1}) == initial
    query.timestamp = 400
    assert resolver.score_topic(query, dag, topic, {'a': 1, 'future': 1}) == 0
    assert topic.updated_at == future.timestamp
    assert topic.recent_message_ids == ['a', 'future']
    assert len(topic._profile_cache) <= 2


def test_profile_cache_neural_warmup_same_dimension_update_and_query_fallback():
    from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
    adapter = EmbeddingAdapter(enabled=True, provider_id='model')
    dag = ConversationDAG(semantic_match_fn=adapter.match)
    node = dag.add_message('a', 'A', 'GPU cooling temperature', timestamp=1)
    topic = TopicState('gpu', message_ids=['a'])
    TopicResolver.rebuild_profile(topic, dag)
    assert topic.centroid_space == 'hashed'
    adapter.remember(node.text, [1, 0])
    TopicResolver.rebuild_profile(topic, dag)
    assert topic.centroid_vector == [1, 0]
    adapter.remember(node.text, [0, 1])
    TopicResolver.rebuild_profile(topic, dag)
    assert topic.centroid_vector == [0, 1]
    TopicResolver.rebuild_profile(topic, dag, query_text='uncached query')
    assert topic.centroid_space == 'hashed'
