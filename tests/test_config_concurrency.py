"""Stale form writes must never undo unrelated or conflicting edits."""
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel
from .test_panel_components import Config, config_host


def panel_for(config):
    config.schema = {"enable": {"type": "bool", "default": True}, "bot_names": {"type": "list", "default": []}}
    config.save_config = AsyncMock(return_value=True)
    host = config_host(config)
    host._coerce_config = lambda value: value
    host.get_learning_policy_status = lambda: {"applied": False}
    return ConfigPanel(host)


@pytest.mark.asyncio
async def test_stale_field_conflict_is_atomic():
    config = Config(enable=False, bot_names=["current"])
    panel = panel_for(config)
    with pytest.raises(ValueError, match="配置冲突.*enable"):
        await panel.save_config_values({"enable": True, "bot_names": ["new"]},
                                       baseline={"enable": True, "bot_names": ["current"]})
    assert config == {"enable": False, "bot_names": ["current"]}
    config.save_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_fields_merge_with_other_tab_and_missing_baseline_rejected():
    config = Config(enable=False, bot_names=["old"])
    panel = panel_for(config)
    await panel.save_config_values({"bot_names": ["new"]}, baseline={"bot_names": ["old"]})
    assert config == {"enable": False, "bot_names": ["new"]}
    with pytest.raises(ValueError, match="配置冲突"):
        await panel.save_config_values({"enable": True}, baseline={})


@pytest.mark.asyncio
async def test_baseline_check_matches_panel_null_for_absent_key():
    config = Config()
    panel = panel_for(config)
    await panel.save_config_values({"enable": False}, baseline={"enable": None})
    assert config["enable"] is False
