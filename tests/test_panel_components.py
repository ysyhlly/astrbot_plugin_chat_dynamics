"""Component contracts without constructing the AstrBot plugin orchestrator."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel


class Config(dict):
    schema = {"enable": {"type": "bool"}}


def config_host(config):
    return SimpleNamespace(
        config=config, _config_lock=asyncio.Lock(), _config_save_in_progress=False,
        _validate_runtime_config=lambda cfg: None,
        _sync_runtime_from_config=lambda **kwargs: None,
        _metric=lambda *args: None,
    )


@pytest.mark.asyncio
async def test_config_failed_save_restores_absent_key_and_flag():
    config = Config()
    config.save_config = AsyncMock(return_value=False)
    host = config_host(config)
    with pytest.raises(RuntimeError, match="returned false"):
        await ConfigPanel(host).save_config_values({"enable": False})
    assert "enable" not in config
    assert not host._config_save_in_progress
    assert not host._config_lock.locked()


@pytest.mark.asyncio
async def test_config_cancelled_save_rolls_back_under_lock():
    config = Config(enable=True)
    entered = asyncio.Event()

    async def save():
        assert host._config_lock.locked()
        entered.set()
        await asyncio.Future()

    config.save_config = save
    host = config_host(config)
    task = asyncio.create_task(ConfigPanel(host).save_config_values({"enable": False}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert config == {"enable": True}
    assert not host._config_save_in_progress
    assert not host._config_lock.locked()


def test_config_schema_fallback_resolves_plugin_root():
    schema = ConfigPanel(SimpleNamespace(config={}))._config_schema()
    assert schema["enable"]["type"] == "bool"





@pytest.mark.asyncio
async def test_config_success_uses_component_panel_without_host_wrappers():
    config = Config(enable=True)
    config.save_config = AsyncMock(return_value=True)
    host = config_host(config)
    host._coerce_config = lambda value: value
    host.get_learning_policy_status = lambda: {"applied": False}
    panel = await ConfigPanel(host).save_config_values({"enable": "false"})
    assert panel["stored"]["enable"] is False
    assert panel["effective"]["enable"] is False
    assert panel["mismatches"] == []
    config.save_config.assert_awaited_once()
