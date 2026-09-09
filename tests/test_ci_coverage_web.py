"""Console failure contracts: bounded inputs, unavailable backends and isolation."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_chat_dynamics.core import web_api as web


@pytest.fixture
def api(monkeypatch, offline_web_responses):
    monkeypatch.setattr(web, "request", NS(username="admin"))
    monkeypatch.setattr(web, "query_value", lambda name: "")
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value={}))
    plugin = NS(_shutting_down=False, _sessions={}, _metric=Mock())
    return web.ConsoleWebAPI(plugin)


ENDPOINTS = ["overview", "sessions", "session", "cool", "reset", "config_get",
             "config_save", "providers", "config_apply", "presets", "apply_preset",
             "read_air", "replay", "notebook_get", "notebook_post", "page_nav"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_shutdown_refuses_operations(api, endpoint):
    api.plugin._shutting_down = True
    response = await getattr(api, endpoint)()
    assert response["status_code"] == 503
    assert "shutting down" in response["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ENDPOINTS + ["ui_preferences_get", "ui_preferences_save"])
async def test_rate_limited_operation_does_not_touch_backend(api, endpoint, monkeypatch):
    monkeypatch.setattr(api, "_rate_limit", lambda *args: {"status_code": 429})
    assert await getattr(api, endpoint)() == {"status_code": 429}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,method,body", [
    ("config_get", "get_config_panel", {}), ("providers", "list_available_providers", {}),
    ("presets", "preset_catalog", {}), ("config_apply", "apply_stored_config", {}),
    ("config_save", "save_config_values", {"config": {}}),
    ("apply_preset", "apply_preset", {"name": "quiet", "confirm": True}),
    ("notebook_get", "notebook_list", {}),
    ("notebook_post", "notebook_mutate_async", {"action": "forget"}),
    ("ui_preferences_get", "get_kv_data", {}),
    ("ui_preferences_save", "put_kv_data", {"ui": "night"}),
])
async def test_backend_errors_are_sanitized(api, monkeypatch, endpoint, method, body):
    backend = Mock(side_effect=RuntimeError("secret backend detail"))
    setattr(api.plugin, method, backend)
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value=body))
    monkeypatch.setattr(web, "query_value", lambda name: "room")
    response = await getattr(api, endpoint)()
    assert response["status_code"] == 503
    assert "secret" not in str(response)
    backend.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,snapshot", [("session", "snapshot_session_or_none"),
    ("read_air", "snapshot_overview"), ("replay", "scene_replay_snapshot")])
async def test_snapshot_failure_returns_service_unavailable(api, monkeypatch, endpoint, snapshot):
    monkeypatch.setattr(web, "query_value", lambda name: "room")
    monkeypatch.setattr(web, snapshot, Mock(side_effect=RuntimeError("private")))
    assert (await getattr(api, endpoint)())["status_code"] == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["cool", "reset"])
@pytest.mark.parametrize("mode,status", [("missing",400),("long",400),("unknown",404),
    ("disabled",404),("failure",503),("success",200)])
async def test_session_mutation_guards(api, monkeypatch, endpoint, mode, status):
    body = {} if mode == "missing" else {"session_key": "x" * 257 if mode == "long" else "room"}
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value=body))
    api.plugin._resolve_session_key = Mock(return_value=None if mode == "unknown" else "room")
    api.plugin.is_group_takeover_enabled = Mock(return_value=mode != "disabled")
    operation = AsyncMock(side_effect=RuntimeError("private") if mode == "failure" else None,
                          return_value=True)
    setattr(api.plugin, "_cool_session_async" if endpoint == "cool" else "_reset_session_state_async", operation)
    monkeypatch.setattr(web, "snapshot_session_or_none", lambda *args: {"session_key": "room"})
    response = await getattr(api, endpoint)()
    assert response["status_code"] == status
    assert operation.call_count == (1 if mode in ("success", "failure") else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["replay", "notebook_get", "session"])
async def test_oversized_query_identifier(api, monkeypatch, endpoint):
    monkeypatch.setattr(web, "query_value", lambda name: "x" * 257)
    assert (await getattr(api, endpoint)())["status_code"] == 400


@pytest.mark.asyncio
async def test_notebook_read_write_and_validation(api, monkeypatch):
    assert (await api.notebook_get())["status_code"] == 400
    assert (await api.notebook_post())["status_code"] == 400
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value=[]))
    assert (await api.notebook_post())["status_code"] == 400
    monkeypatch.setattr(web, "query_value", lambda name: "room")
    api.plugin.notebook_list = Mock(return_value={"entries": []})
    assert (await api.notebook_get())["data"] == {"entries": []}
    body = {"action": "forget", "id": "entry"}
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value=body))
    api.plugin.notebook_mutate = Mock(return_value={"forgotten": True})
    assert (await api.notebook_post())["data"] == {"forgotten": True}
    api.plugin.notebook_mutate.assert_called_once_with("forget", body)
    api.plugin.notebook_mutate.side_effect = ValueError("unknown entry")
    assert (await api.notebook_post())["status_code"] == 400


@pytest.mark.asyncio
async def test_config_values_alias_and_preset_validation(api, monkeypatch):
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value={"values": {"enabled": True}}))
    api.plugin.save_config_values = AsyncMock(return_value={"saved": True})
    assert (await api.config_save())["data"]["saved"]
    api.plugin.save_config_values.assert_awaited_once_with({"enabled": True})
    for name in ([], "x" * 33):
        monkeypatch.setattr(web, "request_json", AsyncMock(return_value={"name": name, "confirm": True}))
        assert (await api.apply_preset())["status_code"] == 400


@pytest.mark.asyncio
async def test_read_air_scoped_summary_includes_partner_status(api, monkeypatch):
    from astrbot_plugin_chat_dynamics.core import dashboard
    monkeypatch.setattr(web, "query_value", lambda name: "room")
    monkeypatch.setattr(web, "snapshot_overview", lambda plugin: {
        "sessions": [{"session_id": "room", "group_id": "g"}],
        "presence_knob": 2, "selflearning": {"enabled": True}, "read_air": {}})
    scoped = Mock(return_value={})
    monkeypatch.setattr(dashboard, "_read_air_summary", scoped)
    result = (await api.read_air())["data"]
    assert result["presence_knob"] == 2
    assert result["selflearning"] == {"enabled": True}
    assert result["sessions"][0]["session_key"] == "room"
    assert scoped.call_args.kwargs["session_key"] == "room"


def test_rate_limiter_expires_old_accounts(api, monkeypatch):
    monkeypatch.setattr(web.time, "monotonic", lambda: 1000)
    api._rate_buckets = {("GET", str(i)): [0] for i in range(4097)}
    assert api._rate_limit("GET", 1) is None
    assert len(api._rate_buckets) == 1
    response = api._rate_limit("GET", 1)
    assert response["status_code"] == 429
    assert response["headers"]["Retry-After"] == "60"
    api.plugin._metric.assert_called_once_with("rate_limited")
