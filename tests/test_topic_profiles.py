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
