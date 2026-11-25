"""Unit tests for series_panels defensive context handling and read-only defaults."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from astrbot_plugin_chat_dynamics import series_panels
from astrbot_plugin_chat_dynamics.series_panels import (
    SeriesPanels,
    get_series_defaults,
    inherit_provider,
    inherit_umos,
    presence,
    require_series_admin,
    verify_series_csrf,
)


def test_get_series_defaults_handles_mock_and_non_iterable_contexts(tmp_path):
    for ctx in (
        None,
        Mock(),
        MagicMock(),
        SimpleNamespace(get_all_stars=lambda: 123),
        SimpleNamespace(get_all_stars=lambda: "not-stars"),
        SimpleNamespace(get_all_stars=Mock(side_effect=RuntimeError("boom"))),
    ):
        assert get_series_defaults(ctx) == {}
        assert inherit_provider("", ctx) == ""
        assert inherit_provider("explicit", ctx) == "explicit"
        assert inherit_umos([], ctx) == []
        assert inherit_umos(["qq:GroupMessage:1"], ctx) == ["qq:GroupMessage:1"]

        data = presence(ctx, "astrbot_plugin_chat_dynamics", root=tmp_path)
        assert data["schema_version"] == 1
        assert data["series_defaults"] == {}
        assert isinstance(data["plugins"], list)


def test_get_series_defaults_reads_sanitized_allowlisted_keys():
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": " p-reply ",
                "decision_provider": " p-decision ",
                "takeover_groups": ["qq:GroupMessage:42", ""],
                "persona_id": "anan",
                "ui_language": "zh-CN",
                "api_key": "must-not-leak",
            }

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=True,
                star_cls=ChatOwner(),
            )
        }
    )
    defaults = get_series_defaults(ctx)
    assert defaults["reply_provider"] == "p-reply"
    assert defaults["decision_provider"] == "p-decision"
    assert defaults["takeover_groups"] == ["qq:GroupMessage:42"]
    assert defaults["persona_id"] == "anan"
    assert defaults["ui_language"] == "zh-CN"
    assert "api_key" not in defaults


def test_series_panels_imports_cleanly_without_astrbot():
    plugin_root = Path(__file__).resolve().parents[1]
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(plugin_root)!r}); "
        "import series_panels, series_workspace; "
        "assert callable(series_panels.require_series_admin); "
        "assert 'DELETE' in series_workspace.PANEL_API_METHODS"
    )
    proc = subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def _install_fake_dashboard_auth(monkeypatch, handler):
    dash = sys.modules.get("astrbot.dashboard") or types.ModuleType("astrbot.dashboard")
    dash_api = sys.modules.get("astrbot.dashboard.api") or types.ModuleType(
        "astrbot.dashboard.api"
    )
    dash_auth = types.ModuleType("astrbot.dashboard.api.auth")
    dash_auth.require_dashboard_user = handler
    monkeypatch.setitem(sys.modules, "astrbot.dashboard", dash)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api", dash_api)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api.auth", dash_auth)


@pytest.mark.asyncio
async def test_require_series_admin_strict_identity_and_fail_closed(monkeypatch):
    async def return_admin(_raw):
        return "admin"

    _install_fake_dashboard_auth(monkeypatch, return_admin)
    ctx_ok = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "admin"}})
    req_ok = SimpleNamespace(
        _request=object(),
        username="admin",
        method="GET",
        headers={},
        cookies={},
    )
    assert await require_series_admin(ctx_ok, req_ok, check_csrf=True) == "admin"

    # Missing raw request -> reject
    with pytest.raises(PermissionError):
        await require_series_admin(ctx_ok, SimpleNamespace(_request=None))

    # Empty / missing dashboard.username -> fail-closed (regression for series_panels:1217)
    for bad_cfg in ({}, {"dashboard": {}}, {"dashboard": {"username": ""}}, {"dashboard": {"username": "   "}}):
        bad_ctx = SimpleNamespace(get_config=lambda cfg=bad_cfg: cfg)
        with pytest.raises(PermissionError):
            await require_series_admin(bad_ctx, req_ok)

    # Wrong admin username -> reject
    other_ctx = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "root"}})
    with pytest.raises(PermissionError):
        await require_series_admin(other_ctx, req_ok)

    # api_key: principal on request.username -> reject
    api_key_req = SimpleNamespace(_request=object(), username="api_key:k1", method="GET")
    with pytest.raises(PermissionError):
        await require_series_admin(ctx_ok, api_key_req)

    # api_key: principal returned by require_dashboard_user -> reject even if config matched
    async def return_api_key(_raw):
        return "api_key:k1"

    _install_fake_dashboard_auth(monkeypatch, return_api_key)
    api_key_cfg = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "api_key:k1"}})
    with pytest.raises(PermissionError):
        await require_series_admin(api_key_cfg, SimpleNamespace(_request=object(), method="GET"))

    # Mismatched request.username vs verified user -> reject
    _install_fake_dashboard_auth(monkeypatch, return_admin)
    mismatch_req = SimpleNamespace(_request=object(), username="other", method="GET")
    with pytest.raises(PermissionError):
        await require_series_admin(ctx_ok, mismatch_req)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_require_series_admin_csrf_enforcement(monkeypatch, method):
    async def return_admin(_raw):
        return "admin"

    _install_fake_dashboard_auth(monkeypatch, return_admin)
    ctx = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "admin"}})

    # Same-origin browser request passes
    ok_req = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={
            "Host": "dashboard.local:6185",
            "Origin": "http://dashboard.local:6185",
            "Sec-Fetch-Site": "same-origin",
        },
        cookies={},
    )
    assert await require_series_admin(ctx, ok_req, check_csrf=True) == "admin"

    # Default port normalization passes (https:443 / http:80)
    https_req = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={"Host": "bot.example.com:443", "Origin": "https://bot.example.com"},
        cookies={},
    )
    assert await require_series_admin(ctx, https_req, check_csrf=True) == "admin"

    # Bearer JWT without Origin/Referer passes (non-ambient credential)
    bearer_req = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={"Host": "dashboard.local", "Authorization": "Bearer jwt-token"},
        cookies={},
    )
    assert await require_series_admin(ctx, bearer_req, check_csrf=True) == "admin"

    # Cross-site Origin rejected
    evil_origin = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={"Host": "dashboard.local", "Origin": "https://evil.local"},
        cookies={},
    )
    with pytest.raises(PermissionError):
        await require_series_admin(ctx, evil_origin, check_csrf=True)

    # Origin: null (sandboxed iframe / redirect) rejected
    null_origin = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={"Host": "dashboard.local", "Origin": "null"},
        cookies={},
    )
    with pytest.raises(PermissionError):
        await require_series_admin(ctx, null_origin, check_csrf=True)

    # Sec-Fetch-Site: cross-site rejected
    cross_fetch = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={"Host": "dashboard.local", "Sec-Fetch-Site": "cross-site"},
        cookies={},
    )
    with pytest.raises(PermissionError):
        await require_series_admin(ctx, cross_fetch, check_csrf=True)

    # Cross-site Referer rejected
    evil_referer = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={"Host": "dashboard.local", "Referer": "https://evil.local/form"},
        cookies={},
    )
    with pytest.raises(PermissionError):
        await require_series_admin(ctx, evil_referer, check_csrf=True)

    # Stripped headers (no Origin, no Referer, no Sec-Fetch-Site, no Bearer/CSRF) -> fail-closed
    stripped_req = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={"Host": "dashboard.local"},
        cookies={},
    )
    with pytest.raises(PermissionError):
        await require_series_admin(ctx, stripped_req, check_csrf=True)

    # Cookie CSRF token mismatch rejected; matching token accepted
    bad_cookie_csrf = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={
            "Host": "dashboard.local",
            "Origin": "https://dashboard.local",
            "X-CSRF-Token": "wrong",
        },
        cookies={"csrf_token": "expected-token"},
    )
    with pytest.raises(PermissionError):
        verify_series_csrf(bad_cookie_csrf)

    good_cookie_csrf = SimpleNamespace(
        _request=object(),
        username="admin",
        method=method,
        headers={
            "Host": "dashboard.local",
            "Origin": "https://dashboard.local",
            "X-CSRF-Token": "expected-token",
        },
        cookies={"csrf_token": "expected-token"},
    )
    verify_series_csrf(good_cookie_csrf)


@pytest.mark.asyncio
async def test_series_panels_status_fails_closed_when_dashboard_username_empty(monkeypatch):
    async def return_admin(_raw):
        return "admin"

    _install_fake_dashboard_auth(monkeypatch, return_admin)
    monkeypatch.setattr(
        series_panels,
        "request",
        SimpleNamespace(_request=object(), username="admin", query={}),
    )
    monkeypatch.setattr(
        series_panels,
        "json_response",
        lambda data, status_code=200: {"status_code": status_code, **data},
    )

    empty_ctx = SimpleNamespace(get_config=lambda: {"dashboard": {"username": ""}}, get_all_stars=lambda: [])
    panel_bad = SeriesPanels(SimpleNamespace(context=empty_ctx), "astrbot_plugin_chat_dynamics")
    res_bad = await panel_bad.status()
    assert res_bad["status_code"] == 403

    good_ctx = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "admin"}}, get_all_stars=lambda: [])
    panel_ok = SeriesPanels(SimpleNamespace(context=good_ctx), "astrbot_plugin_chat_dynamics")
    res_ok = await panel_ok.status()
    assert res_ok["status_code"] == 200
    assert res_ok["status"] == "ok"
