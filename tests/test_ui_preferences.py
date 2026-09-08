"""Account-scoped theme persistence, independent of the plugin's runtime config."""

from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import web_api
from .test_plugin_lifecycle import _plugin


@pytest.mark.asyncio
async def test_theme_is_persistent_and_isolated_by_authenticated_account(monkeypatch, offline_web_responses):
    plugin = _plugin()
    caller = SimpleNamespace(username="alice")
    monkeypatch.setattr(web_api, "request", caller)

    async def body():
        return {"ui": "night"}

    monkeypatch.setattr(web_api, "_json_body", body)
    assert (await plugin._web.ui_preferences_get())["data"] == {"ui": None}
    assert (await plugin._web.ui_preferences_save())["data"] == {"ui": "night", "saved": True}
    # Recreating the API object does not reset the stored preference.
    api = web_api.ConsoleWebAPI(plugin)
    assert (await api.ui_preferences_get())["data"] == {"ui": "night"}
    caller.username = "bob"
    assert (await api.ui_preferences_get())["data"] == {"ui": None}
    caller.username = "alice"
    assert (await api.ui_preferences_get())["data"] == {"ui": "night"}
    assert all("alice" not in key for key in plugin._kv)
    await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"ui": "dark"}, {"ui": []}, {}, {"ui": "night", "username": "bob"}])
async def test_invalid_preferences_do_not_write(monkeypatch, offline_web_responses, payload):
    plugin = _plugin()
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
