import json
from dataclasses import asdict

from .test_sdk_smoke import PLUGIN_DIR
from astrbot.core.config import AstrBotConfig
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config


def test_native_host_preserves_flat_values_while_adding_display_preference(tmp_path):
    schema = json.loads((PLUGIN_DIR / '_conf_schema.json').read_text('utf-8'))
    legacy = {key: entry['default'] for key, entry in schema.items() if key != 'show_advanced_options'}
    legacy.update(participation_backend='llm', decision_provider='native-kept', decision_llm_provider='chat-selected',
                  reply_probability_threshold=63, rule_topic_routing_enabled=False, topic_commit_threshold=.64,
                  decision_prompt='自定义决策\n第二行', reply_prompt='自定义回复', peer_bot_ids=['123'],
                  narrative_rpc_timeout_seconds=17)
    path = tmp_path / 'plugin-config.json'
    path.write_text(json.dumps(legacy), 'utf-8')
    before, _ = parse_runtime_config(legacy)
    config = AstrBotConfig(str(path), schema=schema)
    assert all(config[key] == value for key, value in legacy.items())
    assert list(config) == list(schema)
    after, _ = parse_runtime_config(config)
    assert asdict(before) == asdict(after)
    config['show_advanced_options'] = True
    config.save_config()
    saved = json.loads(path.read_text('utf-8-sig'))
    assert all(saved[key] == value for key, value in legacy.items())
    assert set(saved) - set(legacy) == {'show_advanced_options'}
