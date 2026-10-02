"""Percentage configuration reaches both learned and direct decision paths."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext, PersonaSnapshot
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
from .test_jev_decision_layer import jev_plugin as _jev_plugin, answers

jev_plugin = _jev_plugin


@pytest.mark.parametrize('value', [0, 60, 60.5, 100, '60'])
def test_percentage_configuration(value):
    cfg, warnings = parse_runtime_config({'reply_probability_threshold': value})
    assert cfg.reply_probability_threshold == float(value)
    assert not any('reply_probability_threshold' in w for w in warnings)


@pytest.mark.parametrize('value', [-1, 101, float('nan'), float('inf'), True])
def test_invalid_threshold_is_not_silently_used(value):
    cfg, warnings = parse_runtime_config({'reply_probability_threshold': value})
    assert cfg.reply_probability_threshold == 70
    assert any('reply_probability_threshold' in w for w in warnings)


@pytest.mark.parametrize('value', [-1, 101, float('nan'), True, 'wrong'])
def test_panel_rejects_invalid_values(value):
    from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel
    panel = ConfigPanel(SimpleNamespace())
    with pytest.raises(ValueError):
        panel._normalize_config_update_value('reply_probability_threshold', value, {'type':'float'})


def turn():
    return TurnContext('room', 'user', '安安帮我看看',
        (MessageSnapshot('m1', 'user', '安安帮我看看'),), (), 1, 1, 0, False)


@pytest.mark.parametrize('probability,expected', [(0.599, 'ignore'), (0.6, 'reply'), (0.601, 'reply')])
@pytest.mark.asyncio
async def test_live_engine_uses_configured_threshold_for_direct_jev(jev_plugin, probability, expected):
    p = jev_plugin[0]
    p._runtime_config = replace(p._runtime_config, reply_probability_threshold=60)
    p.jev = SimpleNamespace(evaluate=AsyncMock(return_value=answers(join={'type':'noul','noul':probability})))
    item = ModelTurn(turn(), (), {}, False)
    persona = PersonaSnapshot('fingerprint', 'cid', 'persona', '慢热')
    decision = await p.persona_engine._decide_layer(item, persona, 'observing')
    assert decision.action == expected
