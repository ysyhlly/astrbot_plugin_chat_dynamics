"""Apply-time invariants must be checked before persisting new configuration."""
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from .test_plugin_lifecycle import MockContext


@pytest.fixture
def plugin():
    instance = ChatDynamicsPlugin(MockContext(), {"decision_mode": "legacy", "enable": True})
    instance.persona_engine.bridge.check = Mock(return_value=False)
    instance.persona_engine.bridge.diagnostic = "CD_AGENT_BRIDGE_UNAVAILABLE:test"
    return instance


@pytest.mark.asyncio
async def test_invalid_persona_change_never_reaches_storage_or_live_config(plugin):
    stored = dict(plugin.config)
    original = plugin._runtime_config
    def save():
        stored.update(plugin.config)
        return True
    plugin.save_config = Mock(side_effect=save)
    with pytest.raises(RuntimeError, match="CD_AGENT_BRIDGE_UNAVAILABLE"):
        await plugin.save_config_values({"decision_mode": "persona_model", "enable": False})
    plugin.save_config.assert_not_called()
    assert plugin.config == stored == {"decision_mode": "legacy", "enable": True}
    assert plugin._runtime_config is original
    assert plugin.decision_mode == "legacy" and plugin.enabled
    reloaded = ChatDynamicsPlugin(MockContext(), dict(stored))
    assert reloaded.decision_mode == "legacy"


def test_failed_hot_apply_keeps_previous_runtime_snapshot(plugin):
    previous = plugin._runtime_config
    plugin.config["decision_mode"] = "persona_model"
    with pytest.raises(RuntimeError, match="CD_AGENT_BRIDGE_UNAVAILABLE"):
        plugin._sync_runtime_from_config()
    assert plugin._runtime_config is previous
    assert plugin.decision_mode == "legacy"


@pytest.mark.asyncio
async def test_preflight_precedes_saver_and_only_accepted_snapshot_is_applied(plugin):
    calls = []
    def check():
        calls.append("validate")
        assert plugin.decision_mode == "legacy"
        return True
    def save():
        calls.append("save")
        assert plugin.config["decision_mode"] == "persona_model"
        assert plugin._runtime_config.decision_mode == "legacy"
        return True
    plugin.persona_engine.bridge.check = Mock(side_effect=check)
    plugin.save_config = save
    await plugin.save_config_values({"decision_mode": "persona_model"})
    assert calls == ["validate", "save"]
    assert plugin._runtime_config.decision_mode == plugin.decision_mode == "persona_model"


@pytest.mark.asyncio
async def test_preset_also_checks_host_before_saving(plugin, monkeypatch):
    from astrbot_plugin_chat_dynamics import main
    monkeypatch.setitem(main._PRESETS, "test_persona", {"decision_mode": "persona_model"})
    before = dict(plugin.config)
    plugin.save_config = Mock(return_value=True)
    with pytest.raises(RuntimeError, match="CD_AGENT_BRIDGE_UNAVAILABLE"):
        await plugin.apply_preset("test_persona")
    plugin.save_config.assert_not_called()
    assert plugin.config == before
    assert plugin.decision_mode == plugin._runtime_config.decision_mode == "legacy"


@pytest.mark.asyncio
async def test_preset_cancel_restores_candidate_values(plugin):
    import asyncio
    before = dict(plugin.config)
    entered = asyncio.Event()
    async def save():
        entered.set()
        await asyncio.Event().wait()
    plugin.save_config = save
    task = asyncio.create_task(plugin.apply_preset("observe"))
    await entered.wait()
    plugin._sync_runtime_from_config()
    assert plugin.shadow_mode is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plugin.config == before
    assert not plugin.shadow_mode
    assert not plugin._config_save_in_progress
