"""Regression coverage for config fallback, quotas, and diagnostic state."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel
from astrbot_plugin_chat_dynamics.core.daily_rhythm import DailyRhythmGate
from astrbot_plugin_chat_dynamics.core.social_manners import SocialMannersGate
from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from .test_plugin_lifecycle import MockContext


@pytest.fixture
def fallback_plugin():
    plugin = ChatDynamicsPlugin(MockContext(), {"decision_mode": "legacy"})
    plugin.config["decision_mode"] = "persona_model"
    plugin._config_source_snapshot = plugin._config_snapshot()
    plugin._persona_fallback = "CD_AGENT_BRIDGE_UNAVAILABLE:test"
    plugin.persona_engine.bridge.check = Mock(return_value=False)
    plugin.persona_engine.bridge.diagnostic = plugin._persona_fallback
    plugin.save_config = Mock(return_value=True)
    return plugin


@pytest.mark.asyncio
async def test_unrelated_save_preserves_desired_persona_and_runtime_fallback(fallback_plugin):
    plugin = fallback_plugin
    panel = await plugin.save_config_values({"casual_emoji_enabled": True})
    plugin.save_config.assert_called_once()
    assert plugin.config["decision_mode"] == "persona_model"
    assert plugin._runtime_config.decision_mode == "legacy"
    assert plugin._runtime_config.casual_emoji_enabled is True
    assert plugin._persona_fallback == "CD_AGENT_BRIDGE_UNAVAILABLE:test"
    assert "decision_mode" in panel["mismatches"]
    assert "casual_emoji_enabled" not in panel["mismatches"]


@pytest.mark.asyncio
async def test_explicit_persona_reenable_during_fallback_rejects_before_saving(fallback_plugin):
    plugin = fallback_plugin
    before = dict(plugin.config)
    with pytest.raises(ValueError, match="CD_AGENT_BRIDGE_UNAVAILABLE"):
        await plugin.save_config_values({"decision_mode": "persona_model", "casual_emoji_enabled": True})
    plugin.save_config.assert_not_called()
    assert plugin.config == before
    assert plugin._runtime_config.decision_mode == "legacy"


@pytest.mark.asyncio
async def test_observe_preset_works_during_persona_fallback(fallback_plugin):
    plugin = fallback_plugin
    await plugin.apply_preset("observe")
    plugin.save_config.assert_called_once()
    assert plugin.config["decision_mode"] == "persona_model"
    assert plugin._runtime_config.decision_mode == "legacy"
    assert plugin.shadow_mode is True
    assert plugin._persona_fallback == "CD_AGENT_BRIDGE_UNAVAILABLE:test"


@pytest.mark.asyncio
async def test_recovered_bridge_restores_persona_on_save(fallback_plugin):
    plugin = fallback_plugin
    plugin.persona_engine.bridge.check = Mock(return_value=True)
    await plugin.save_config_values({"casual_emoji_enabled": True})
    assert plugin.config["decision_mode"] == plugin._runtime_config.decision_mode == "persona_model"
    assert plugin._persona_fallback == ""


@pytest.mark.parametrize("kind", ["int", "float"])
def test_oversized_integer_is_field_validation_error(kind):
    panel = ConfigPanel(SimpleNamespace())
    with pytest.raises(ValueError, match="test_field must be"):
        panel._normalize_config_update_value("test_field", 10 ** 400, {"type": kind})


@pytest.mark.parametrize("value", [True, False])
def test_numeric_boolean_uses_default_with_warning(value):
    defaults, _ = parse_runtime_config({})
    cfg, warnings = parse_runtime_config({"decision_timeout": value, "rhythm_goodnight_text_quota": value})
    assert cfg.decision_timeout == defaults.decision_timeout
    assert cfg.rhythm_goodnight_text_quota == defaults.rhythm_goodnight_text_quota
    assert any("decision_timeout" in warning for warning in warnings)
    assert any("rhythm_goodnight_text_quota" in warning for warning in warnings)


def test_config_drift_compares_groups_as_sets_and_missing_as_defaults():
    plugin = ChatDynamicsPlugin(MockContext(), {
        "decision_mode": "legacy", "takeover_groups": ["b", " a ", "b"],
        "exclude_groups": ["d", "c"],
    })
    panel = plugin.get_config_panel()
    assert panel["mismatches"] == []
    plugin.config["decision_timeout"] = None
    panel = plugin.get_config_panel()
    assert "decision_timeout" in panel["mismatches"]


@pytest.mark.parametrize("quota", [1, 2])
def test_goodnight_quota_counts_successful_sends_only(quota):
    cfg, _ = parse_runtime_config({"rhythm_goodnight_text_quota": quota, "rhythm_timezone": "UTC"})
    gate = DailyRhythmGate()
    stamp = datetime(2026, 3, 15, 23, tzinfo=timezone.utc).timestamp()
    first = gate.evaluate(session_id="s", text="晚安", cfg=cfg, now=stamp)
    assert first.consume_goodnight_quota
    # Failed send: no note_spoke, so the next request still has its full quota.
    for index in range(quota):
        verdict = gate.evaluate(session_id="s", text="晚安大家", cfg=cfg, now=stamp + 10 + index)
        assert verdict.allow and verdict.consume_goodnight_quota
        gate.note_spoke("s", verdict=verdict, now=stamp + 10 + index)
    exhausted = gate.evaluate(session_id="s", text="晚安", cfg=cfg, now=stamp + 30)
    assert not exhausted.allow
    explicit = gate.evaluate(session_id="s", text="晚安", explicit=True, cfg=cfg, now=stamp + 31)
    assert explicit.allow and not explicit.consume_goodnight_quota


def test_explicit_banter_expires_hype_diagnostic_without_blocking_reply():
    gate = SocialMannersGate()
    gate.note_intervene("s", hyped=True, now=1000)
    kwargs = {"session_id": "s", "user_id": "u", "text": "hi", "explicit": True, "occasion_kind": "banter"}
    recent = gate.evaluate(**kwargs, now=1100)
    assert recent.allow and recent.reason_code == "explicit_after_hype"
    expired = gate.evaluate(**kwargs, now=1281)
    assert expired.allow and expired.reason_code != "explicit_after_hype"


def test_missing_stored_default_still_reports_real_runtime_override():
    plugin = ChatDynamicsPlugin(MockContext(), {"decision_mode": "legacy"})
    plugin._runtime_config = replace(plugin._runtime_config, casual_emoji_enabled=True)
    assert "casual_emoji_enabled" in plugin.get_config_panel()["mismatches"]
