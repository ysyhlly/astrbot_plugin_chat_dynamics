"""Contract tests for plugin page side-nav (signed content_path, no tokenless fallback)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import astrbot_plugin_chat_dynamics.core.web_api as web_api
from astrbot_plugin_chat_dynamics.core.web_api import PLUGIN_NAME
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "pages"
PAGE_DIRS = ("console", "config", "today", "manners", "memory", "replay")


def test_daytime_rules_do_not_hardcode_night_fills():
    base = (PAGES / "shared" / "base.css").read_text(encoding="utf-8")
    assert "background: rgba(7, 16, 23, 0.72)" not in base
    assert "background: rgba(7, 16, 23, 0.35)" not in base
    assert "background: rgba(7, 16, 23, 0.28)" not in base
    assert "rgba(7, 16, 23, 0.36)" not in base
    assert "background: var(--input-bg)" in base
    shell = (PAGES / "shared" / "shell.css").read_text(encoding="utf-8")
    assert "rgba(18, 36, 45, 0.98)" not in shell
    assert "var(--panel-grad" in shell


def test_daytime_ui_is_default_and_night_tokens_remain():
    base = (PAGES / "shared" / "base.css").read_text(encoding="utf-8")
    assert "--bg: #f3efe6" in base
    assert 'html[data-ui-theme="night"]' in base
    assert "--bg: #071017" in base
    nav_js = (PAGES / "shared" / "plugin_nav.js").read_text(encoding="utf-8")
    assert "chat_dynamics_ui" in nav_js
    assert "export function applyUi" in nav_js
    assert "export function resolvedUi" in nav_js
    for name in PAGE_DIRS:
        html = (PAGES / name / "index.html").read_text(encoding="utf-8")
        assert "chat_dynamics_ui" in html
        assert 'content="light dark"' in html
        assert 'id="pluginSideNav"' in html


def test_plugin_nav_copies_match_shared():
    for asset in ("plugin_nav.js", "plugin_nav.css", "theme.js", "theme.css"):
        canonical = (PAGES / "shared" / asset).read_bytes()
        assert canonical, f"shared/{asset} is empty"
        for name in PAGE_DIRS:
            copy = (PAGES / name / asset).read_bytes()
            assert copy == canonical, f"{name}/{asset} drifted from shared"


def test_plugin_nav_rejects_opaque_origin_and_tokenless_fallback():
    source = (PAGES / "shared" / "plugin_nav.js").read_text(encoding="utf-8")
    assert "window.location.href" in source
    assert "asset_token" in source
    assert "signedContentHref" in source
    assert "new URL(contentPath, window.location.origin)" not in source
    assert "window.location.assign(siblingContentUrl" not in source
    assert "stay on current page" in source
    for name in PAGE_DIRS:
        html = (PAGES / name / "index.html").read_text(encoding="utf-8")
        assert 'id="pluginSideNav"' in html
        assert "../shared/" not in html


def test_signed_content_href_js_contract():
    source = (PAGES / "shared" / "plugin_nav.js").read_text(encoding="utf-8")
    start = source.index("export function signedContentHref")
    end = source.index("export function siblingContentUrl")
    body = source[start:end]
    assert 'base !== "null"' in body
    assert "/[?&]asset_token=/.test(href)" in body


@pytest.mark.asyncio
async def test_page_nav_returns_signed_path(monkeypatch, offline_web_responses):
    plugin = _plugin()

    class Pages:
        async def get_plugin_page_entry_config(self, **kwargs):
            assert kwargs["page_name"] == "today"
            assert kwargs["username"] == "admin"
            return {
                "content_path": (
                    f"/api/plugin/page/content/{PLUGIN_NAME}/today/?asset_token=fresh"
                )
            }

    req = SimpleNamespace(
        username="admin",
        plugin_name=PLUGIN_NAME,
        headers={},
        _request=SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(services=SimpleNamespace(plugin_pages=Pages()))
            )
        ),
    )
    monkeypatch.setattr(web_api, "query_value", lambda name: "today" if name in {"page", "page_name"} else "")
    monkeypatch.setattr(web_api, "request", SimpleNamespace(_get_current=lambda: req))
    result = await plugin._web.page_nav()
    assert result["ok"] is True
    assert result["data"]["page"] == "today"
    assert "asset_token=fresh" in result["data"]["content_path"]
    assert f"/{PLUGIN_NAME}/today/" in result["data"]["content_path"].replace("%2F", "/")


@pytest.mark.asyncio
async def test_page_nav_unauthorized_without_username(monkeypatch, offline_web_responses):
    plugin = _plugin()
    req = SimpleNamespace(username=None, plugin_name=PLUGIN_NAME, headers={}, _request=object())
    monkeypatch.setattr(web_api, "query_value", lambda name: "today" if name == "page" else "")
    monkeypatch.setattr(web_api, "request", SimpleNamespace(_get_current=lambda: req))
    result = await plugin._web.page_nav()
    assert result["ok"] is False
    assert result["status_code"] == 401
    assert result["error"] == "unauthorized"
