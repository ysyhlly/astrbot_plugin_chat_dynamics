"""Account-scoped theme persistence, independent of the plugin's runtime config."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core import web_api
from .test_plugin_lifecycle import _plugin


@pytest.mark.asyncio
async def test_theme_is_persistent_and_isolated_by_authenticated_account(monkeypatch, offline_web_responses):
    plugin = _plugin()
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=True))
    caller = SimpleNamespace(username="alice")
    monkeypatch.setattr(web_api, "request", caller)

    async def body():
        return {"ui": "night"}

    monkeypatch.setattr(web_api, "_json_body", body)
    assert (await plugin._web.ui_preferences_get())["data"] == {"ui": None, "setup_dismissed": False}
    assert (await plugin._web.ui_preferences_save())["data"] == {"ui": "night", "saved": True}
    # Recreating the API object does not reset the stored preference.
    api = web_api.ConsoleWebAPI(plugin)
    monkeypatch.setattr(api, "_narrative_admin_allowed", AsyncMock(return_value=True))
    assert (await api.ui_preferences_get())["data"] == {"ui": "night", "setup_dismissed": False}
    caller.username = "bob"
    assert (await api.ui_preferences_get())["data"] == {"ui": None, "setup_dismissed": False}
    caller.username = "alice"
    assert (await api.ui_preferences_get())["data"] == {"ui": "night", "setup_dismissed": False}
    assert all("alice" not in key for key in plugin._kv)
    await plugin.terminate()


@pytest.mark.asyncio
async def test_setup_dismissed_is_persistent_and_isolated_by_account(monkeypatch, offline_web_responses):
    plugin = _plugin()
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=True))
    caller = SimpleNamespace(username="alice")
    monkeypatch.setattr(web_api, "request", caller)

    current_body = {"setup_dismissed": True}

    async def body():
        return current_body

    monkeypatch.setattr(web_api, "_json_body", body)
    assert (await plugin._web.ui_preferences_get())["data"] == {"ui": None, "setup_dismissed": False}
    assert (await plugin._web.ui_preferences_save())["data"] == {"setup_dismissed": True, "saved": True}
    assert (await plugin._web.ui_preferences_get())["data"] == {"ui": None, "setup_dismissed": True}

    current_body = {"ui": "day", "setup_dismissed": False}
    assert (await plugin._web.ui_preferences_save())["data"] == {
        "ui": "day",
        "setup_dismissed": False,
        "saved": True,
    }
    assert (await plugin._web.ui_preferences_get())["data"] == {"ui": "day", "setup_dismissed": False}

    caller.username = "bob"
    assert (await plugin._web.ui_preferences_get())["data"] == {"ui": None, "setup_dismissed": False}
    assert all("alice" not in key for key in plugin._kv)
    await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ui": "dark"},
        {"ui": []},
        {},
        {"ui": "night", "username": "bob"},
        {"setup_dismissed": 1},
        {"setup_dismissed": "true"},
        {"setup_dismissed": None},
        {"ui": "day", "setup_dismissed": 0},
    ],
)
async def test_invalid_preferences_do_not_write(monkeypatch, offline_web_responses, payload):
    plugin = _plugin()
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(web_api, "request", SimpleNamespace(username="alice"))

    async def body():
        return payload

    monkeypatch.setattr(web_api, "_json_body", body)
    assert (await plugin._web.ui_preferences_save())["status_code"] == 400
    assert plugin._kv == {}
    await plugin.terminate()


@pytest.mark.asyncio
async def test_preferences_require_login_and_report_storage_failure(monkeypatch, offline_web_responses):
    plugin = _plugin()
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=True))
    caller = SimpleNamespace(username=None)
    monkeypatch.setattr(web_api, "request", caller)
    assert (await plugin._web.ui_preferences_get())["status_code"] == 401
    assert (await plugin._web.ui_preferences_save())["status_code"] == 401
    caller.username = "alice"

    async def fail(*args):
        raise OSError("offline storage")

    async def body():
        return {"ui": "night"}

    monkeypatch.setattr(web_api, "_json_body", body)
    plugin.get_kv_data = fail
    plugin.put_kv_data = fail
    assert (await plugin._web.ui_preferences_get())["status_code"] == 503
    assert (await plugin._web.ui_preferences_save())["status_code"] == 503
    await plugin.terminate()
