"""Security regression tests: Dashboard admin auth and CSRF guards on all Chat Dynamics Web APIs."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_chat_dynamics.core import decision_status, web_api
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin


def _install_dashboard_auth(monkeypatch, handler):
    dash = sys.modules.get("astrbot.dashboard") or types.ModuleType("astrbot.dashboard")
    dash_api = sys.modules.get("astrbot.dashboard.api") or types.ModuleType(
        "astrbot.dashboard.api"
    )
    dash_auth = types.ModuleType("astrbot.dashboard.api.auth")
    dash_auth.require_dashboard_user = handler
    monkeypatch.setitem(sys.modules, "astrbot.dashboard", dash)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api", dash_api)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api.auth", dash_auth)


def _make_req(
    *,
    method: str = "GET",
    username: str | None = "admin",
    has_raw: bool = True,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
):
    raw = SimpleNamespace(method=method, headers=headers or {}, cookies=cookies or {}) if has_raw else None
    return SimpleNamespace(
        method=method,
        username=username,
        headers=headers or {},
        cookies=cookies or {},
        query={},
        args={},
        _request=raw,
        json=AsyncMock(return_value={}),
    )


CONSOLE_READ_HANDLERS = [
    "overview",
    "sessions",
    "session",
    "presets",
    "config_get",
    "integrations",
    "providers",
    "page_nav",
    "ui_preferences_get",
    "notebook_get",
    "narrative_get",
    "read_air",
    "replay",
    "annotations_get",
    "setup_groups",
    "series_matrix_get",
    "series_search_get",
    "series_telemetry_get",
    "series_health_get",
    "series_templates_get",
]

CONSOLE_WRITE_HANDLERS = [
    ("cool", "POST"),
    ("uncool", "POST"),
    ("reset", "POST"),
    ("apply_preset", "POST"),
    ("config_save", "POST"),
    ("config_apply", "POST"),
    ("ui_preferences_save", "POST"),
    ("notebook_post", "POST"),
    ("narrative_post", "POST"),
    ("annotations_post", "POST"),
    ("setup_probe", "POST"),
    ("setup_activate_companions", "POST"),
    ("series_matrix_toggle_post", "POST"),
    ("series_matrix_batch_post", "POST"),
    ("series_preflight_post", "POST"),
    ("series_templates_post", "POST"),
    ("series_templates_delete", "DELETE"),
]


def test_security_matrix_covers_every_registered_web_route():
    registered = []
    ctx = SimpleNamespace(
        registered_web_apis=registered,
        register_web_api=lambda *row: registered.append(row),
    )
    plugin = SimpleNamespace(context=ctx)
    console_api = web_api.ConsoleWebAPI(plugin)
    console_api.register()
    decision_api = decision_status.DecisionStatusWebAPI(plugin)
    decision_api.register()

    console_names = {
        handler.__name__
        for _route, handler, _methods, _desc in registered
        if getattr(handler, "__self__", None) is console_api
    }
    decision_names = {
        handler.__name__
        for _route, handler, _methods, _desc in registered
        if getattr(handler, "__self__", None) is decision_api
    }
    covered_console = set(CONSOLE_READ_HANDLERS) | {name for name, _ in CONSOLE_WRITE_HANDLERS}
    assert console_names == covered_console
    assert decision_names == {"status", "history"}


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", CONSOLE_READ_HANDLERS)
@pytest.mark.parametrize("attack", ["no_raw", "api_key", "empty_expected", "wrong_user"])
async def test_console_read_endpoints_reject_unauthorized(
    monkeypatch, offline_web_responses, handler_name, attack
):
    plugin = _plugin({"enable": True})
    if attack == "empty_expected":
        plugin.context.get_config = lambda: {"dashboard": {"username": ""}}
    else:
        plugin.context.get_config = lambda: {"dashboard": {"username": "admin"}}

    async def auth_fn(_raw):
        if attack == "api_key":
            return "api_key:token-1"
        if attack == "wrong_user":
            return "guest"
        return "admin"

    _install_dashboard_auth(monkeypatch, auth_fn)
    req = _make_req(
        method="GET",
        username="api_key:token-1" if attack == "api_key" else ("guest" if attack == "wrong_user" else "admin"),
        has_raw=(attack != "no_raw"),
    )
    monkeypatch.setattr(web_api, "request", req)
    res = await getattr(plugin._web, handler_name)()
    assert res["status_code"] == 403
    await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name,method", CONSOLE_WRITE_HANDLERS)
@pytest.mark.parametrize(
    "attack",
    [
        "no_raw",
        "api_key",
        "empty_expected",
        "cross_site_origin",
        "null_origin",
        "sec_fetch_cross_site",
        "stripped_csrf_headers",
    ],
)
async def test_console_write_endpoints_reject_unauthorized_and_csrf(
    monkeypatch, offline_web_responses, handler_name, method, attack
):
    plugin = _plugin({"enable": True})
    plugin.context.get_config = (
        (lambda: {"dashboard": {"username": ""}})
        if attack == "empty_expected"
        else (lambda: {"dashboard": {"username": "admin"}})
    )

    async def auth_fn(_raw):
        return "api_key:token-1" if attack == "api_key" else "admin"

    _install_dashboard_auth(monkeypatch, auth_fn)

    headers: dict[str, str] = {"Host": "dashboard.local"}
    if attack == "cross_site_origin":
        headers["Origin"] = "https://evil.local"
    elif attack == "null_origin":
        headers["Origin"] = "null"
    elif attack == "sec_fetch_cross_site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif attack == "stripped_csrf_headers":
        headers = {"Host": "dashboard.local"}
    else:
        headers["Origin"] = "https://dashboard.local"
        headers["Sec-Fetch-Site"] = "same-origin"

    req = _make_req(
        method=method,
        username="api_key:token-1" if attack == "api_key" else "admin",
        has_raw=(attack != "no_raw"),
        headers=headers,
    )
    monkeypatch.setattr(web_api, "request", req)
    res = await getattr(plugin._web, handler_name)()
    assert res["status_code"] == 403
    await plugin.terminate()


@pytest.mark.asyncio
async def test_decision_status_and_history_enforce_admin_and_csrf(
    monkeypatch, offline_web_responses
):
    plugin = _plugin({"enable": True})
    plugin.context.get_config = lambda: {"dashboard": {"username": "admin"}}
    api = decision_status.DecisionStatusWebAPI(plugin)

    async def auth_admin(_raw):
        return "admin"

    _install_dashboard_auth(monkeypatch, auth_admin)

    # api_key rejected on status GET
    monkeypatch.setattr(web_api, "request", _make_req(method="GET", username="api_key:k1"))
    assert (await api.status())["status_code"] == 403

    # Cross-site POST rejected on history
    monkeypatch.setattr(
        web_api,
        "request",
        _make_req(
            method="POST",
            username="admin",
            headers={"Host": "dashboard.local", "Origin": "https://evil.local"},
        ),
    )
    assert (await api.history())["status_code"] == 403

    # Valid admin GET passes
    monkeypatch.setattr(web_api, "request", _make_req(method="GET", username="admin"))
    assert (await api.status())["status_code"] == 200
    await plugin.terminate()


@pytest.mark.asyncio
async def test_config_save_reset_matrix_search_pass_for_valid_admin_with_csrf(
    monkeypatch, offline_web_responses
):
    plugin = _plugin({"enable": True, "takeover_all": False, "takeover_groups": ["room-9"]})
    plugin.context.get_config = lambda: {"dashboard": {"username": "admin"}}
    plugin.context.get_all_stars = lambda: []

    async def auth_admin(_raw):
        return "admin"

    _install_dashboard_auth(monkeypatch, auth_admin)

    # series_matrix_get and series_search_get succeed for admin
    monkeypatch.setattr(web_api, "request", _make_req(method="GET", username="admin"))
    monkeypatch.setattr(web_api, "query_value", lambda name: {"q": "room-9", "limit": "10"}.get(name, ""))
    assert (await plugin._web.series_search_get())["status_code"] == 200
    monkeypatch.setattr(web_api, "query_value", lambda name: "")
    assert (await plugin._web.series_matrix_get())["status_code"] == 200

    # config_save succeeds with same-origin CSRF headers
    plugin.save_config_values = AsyncMock(return_value={"saved": True})
    monkeypatch.setattr(
        web_api,
        "request",
        _make_req(
            method="POST",
            username="admin",
            headers={
                "Host": "dashboard.local",
                "Origin": "https://dashboard.local",
                "Sec-Fetch-Site": "same-origin",
            },
        ),
    )
    monkeypatch.setattr(web_api, "_json_body", AsyncMock(return_value={"config": {"enabled": True}}))
    saved = await plugin._web.config_save()
    assert saved["status_code"] == 200
    assert saved["data"] == {"saved": True}

    # reset passes auth+CSRF guard (returns 404 for unknown session, NOT 403)
    plugin._resolve_session_key = Mock(return_value=None)
    monkeypatch.setattr(web_api, "_json_body", AsyncMock(return_value={"session_key": "missing"}))
    reset_res = await plugin._web.reset()
    assert reset_res["status_code"] == 404
    await plugin.terminate()
