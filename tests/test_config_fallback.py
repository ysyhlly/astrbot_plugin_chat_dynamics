"""Current configuration edits must preserve a degraded bridge until it recovers."""
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from .test_plugin_lifecycle import _plugin


@pytest.mark.asyncio
@pytest.mark.parametrize('stored_mode', [None, 'legacy', 'persona_model'])
async def test_repeated_config_edits_preserve_fallback_until_bridge_recovers(monkeypatch, stored_mode):
    def unavailable(bridge):
        bridge.diagnostic = 'CD_AGENT_BRIDGE_UNAVAILABLE:test'
        return False

    monkeypatch.setattr(AstrBotAgentBridge, 'check', unavailable)
    stored = {} if stored_mode is None else {'decision_mode': stored_mode}
    p = _plugin(stored)
    p.save_config = Mock(return_value=True)
    try:
        assert p._runtime_config.decision_mode == 'legacy'
        diagnostic = p._persona_fallback
        await p.save_config_values({'shadow_mode': True})
        await p.save_config_values({'presence_knob': 'lively'})
        p.config['reply_probability_threshold'] = 65
        p.refresh_config()
        assert p._runtime_config.decision_mode == 'legacy'
        assert p.shadow_mode and p.presence_knob == 'lively'
        assert p._runtime_config.reply_probability_threshold == 65
        assert p._persona_fallback == diagnostic
        assert p.config.get('decision_mode') == stored_mode
        assert p.save_config.call_count == 2

        p.persona_engine.bridge.check = Mock(return_value=True)
        await p.save_config_values({'shadow_mode': False})
        assert p._runtime_config.decision_mode == 'persona_model'
        assert not p.shadow_mode
        assert p._persona_fallback == ''
        assert p.config.get('decision_mode') == stored_mode
    finally:
        await p.terminate()
