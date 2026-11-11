import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / '_conf_schema.json').read_text('utf-8'))


def visible(**changes):
    values = {key: item['default'] for key, item in SCHEMA.items()}
    values.update(changes)
    return {key for key, item in SCHEMA.items() if not item.get('invisible')
            and all(type(values.get(name)) is type(expected) and values[name] == expected
                    for name, expected in item.get('condition', {}).items())}


def test_common_native_configuration_is_small_and_advanced_values_remain_declared():
    common = visible()
    assert 20 <= len(common) <= 30
    assert {'enable', 'show_advanced_options', 'participation_backend', 'reply_provider', 'discussion_judgment_mode'} <= common
    assert 'decision_prompt' not in common
    assert 'decision_prompt' in visible(show_advanced_options=True)
    assert 'topic_commit_threshold' in SCHEMA
    assert SCHEMA['decision_prompt']['editor_mode']
    assert SCHEMA['reply_prompt']['editor_mode']


def test_native_conditions_select_only_the_chosen_model_family():
    assert 'decision_provider' in visible()
    assert 'decision_llm_provider' not in visible()
    llm = visible(participation_backend='llm', discussion_judgment_mode='llm')
    assert {'decision_llm_provider', 'discussion_llm_provider'} <= llm
    assert not {'decision_provider', 'discussion_reranker_provider'} & llm
    assert 'decision_llm_timeout' in visible(participation_backend='llm', show_advanced_options=True)
    assert 'jev_timeout' not in visible(participation_backend='llm', show_advanced_options=True)


def test_native_optional_features_do_not_display_inactive_children():
    assert 'narrative_sessions' not in visible()
    assert 'narrative_sessions' in visible(narrative_enabled=True)
    assert 'narrative_sessions' not in visible(narrative_enabled=True, narrative_all_groups=True)
    assert 'topic_reranker_provider' not in visible(topic_reranker_enabled=False)
    assert 'discussion_judgment_mode' not in visible(conversation_router_enabled=False)
    assert 'topic_commit_threshold' not in visible(show_advanced_options=True)
    assert 'topic_commit_threshold' in visible(show_advanced_options=True, rule_topic_routing_enabled=False)


def test_every_condition_references_a_real_flat_setting():
    for key, item in SCHEMA.items():
        assert item['type'] != 'object', 'no implicit nesting migration'
        assert key not in item.get('condition', {})
        for reference in item.get('condition', {}):
            assert reference in SCHEMA
