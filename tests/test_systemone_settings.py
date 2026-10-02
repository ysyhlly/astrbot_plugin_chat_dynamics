"""Native decision models stay separate from text models and save atomically."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel
from .test_panel_components import Config, config_host


def make_panel():
    config = Config(decision_backend="kev", decision_mode="persona_model", decision_learning_mode="active")
    host = config_host(config)
    panel = ConfigPanel(host)
    config.schema = ConfigPanel(SimpleNamespace(config={}))._config_schema()
    config.save_config = AsyncMock(return_value=True)
    native = SimpleNamespace(is_systemone_provider=True, provider_config={
        "id": "commandcode/jev", "model": "typesafe/jev", "type": "systemone_catalog", "key": ["secret"]})
    text = SimpleNamespace(provider_config={"id": "reply-model", "model": "text-model"})
    host.context = SimpleNamespace(get_all_providers=lambda: [native, text],
        get_provider_by_id=lambda pid: {"commandcode/jev": native, "reply-model": text}.get(pid))
    host.jev = SimpleNamespace(is_systemone_router=True)
    host._coerce_config = lambda cfg: cfg
    host.get_learning_policy_status = lambda: {}
    return panel, config, host


def test_model_list_identifies_native_decision_models_without_credentials():
    panel, _, _ = make_panel()
    models = panel.list_available_providers()
    assert [row["id"] for row in models["systemone"]] == ["commandcode/jev"]
    assert models["systemone_router_ready"]
    assert "secret" not in str(models)
    assert not next(row for row in models["chat"] if row["id"] == "reply-model")["systemone"]


@pytest.mark.asyncio
async def test_valid_jev_selection_saves_provider_id_without_copying_credentials():
    panel, config, _ = make_panel()
    await panel.save_config_values({"decision_provider": "commandcode/jev"})
    assert config["decision_provider"] == "commandcode/jev"
    assert config["decision_learning_mode"] == "active"  # Retired value has no consumer.
    assert not any(key in config for key in ("jev_base_url", "jev_model", "jev_api_key_env"))
    config.save_config.assert_awaited_once()


@pytest.mark.parametrize("provider,learning,ready,match", [
    ("reply-model", "off", True, "类型不匹配"),
    ("missing", "off", True, "未加载"),
    ("", "off", True, "请选择"),
    ("commandcode/jev", "off", False, "连接尚未就绪"),
])
@pytest.mark.asyncio
async def test_invalid_jev_selection_does_not_save_or_mutate(provider, learning, ready, match):
    panel, config, host = make_panel()
    original = dict(config)
    host.jev.is_systemone_router = ready
    with pytest.raises(ValueError, match=match):
        await panel.save_config_values({"decision_provider": provider})
    assert dict(config) == original
    config.save_config.assert_not_awaited()
    assert not host._config_save_in_progress


@pytest.mark.asyncio
async def test_decision_model_cannot_be_saved_as_reply_model():
    panel, config, _ = make_panel()
    with pytest.raises(ValueError, match="只用于决策"):
        await panel.save_config_values({"reply_provider": "commandcode/jev"})
    assert "reply_provider" not in config
    config.save_config.assert_not_awaited()
