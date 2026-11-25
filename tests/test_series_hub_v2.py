"""Tests for v2.0 series_presence badges/pulse, group control matrix, federated search, and console/sunset UI contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import astrbot_plugin_chat_dynamics.core.web_api as web_api
import astrbot_plugin_chat_dynamics.series_panels as series_panels
from astrbot_plugin_chat_dynamics.core.series_hub import (
    build_series_matrix,
    federated_series_search,
    toggle_series_matrix,
)
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin

ROOT = Path(__file__).resolve().parents[1]


def _make_star_entry(identity: str, owner: object, *, activated: bool = True, version: str = "2.0.0"):
    return SimpleNamespace(
        name=identity,
        activated=activated,
        version=version,
        star_cls=owner,
    )


def test_presence_v2_aggregates_badges_pulse_with_ttl_cache_and_isolation(tmp_path):
    series_panels.invalidate_presence_cache()
    calls = {"learning": 0}

    class LearningOwner:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_summary(self):
            calls["learning"] += 1
            return {
                "badge": {"count": 3, "tone": "warm", "label": "3 项待审"},
                "pulse": {"headline": "今日已审查 12 条", "updated_at": "2026-11-21T01:20:00+08:00"},
            }

    class BrokenOwner:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_summary(self):
            raise RuntimeError("boom")

    class AsyncBadOwner:
        async def get_series_summary(self):
            return {"badge": {"count": 99, "tone": "danger", "label": "bad"}}

    learning_owner = LearningOwner()
    broken_owner = BrokenOwner()
    async_owner = AsyncBadOwner()

    ctx = SimpleNamespace(
        get_all_stars=lambda: [
            _make_star_entry("astrbot_plugin_dynamics_learning", learning_owner),
            _make_star_entry("astrbot_plugin_dynamics_horizons", broken_owner),
            _make_star_entry("astrbot_plugin_dynamics_games", async_owner),
        ]
    )

    first = series_panels.presence(ctx, "astrbot_plugin_chat_dynamics", root=tmp_path)
    by_id = {p["id"]: p for p in first["plugins"]}
    assert by_id["astrbot_plugin_chat_dynamics"]["label"] == "对话主控"
    assert by_id["astrbot_plugin_group_chronicle"]["label"] == "群文化沿革"
    assert by_id["astrbot_plugin_dynamics_learning"]["badge"] == {
        "count": 3,
        "tone": "warm",
        "label": "3 项待审",
    }
    assert by_id["astrbot_plugin_dynamics_learning"]["pulse"] == {
        "headline": "今日已审查 12 条",
        "updated_at": "2026-11-21T01:20:00+08:00",
    }
    assert by_id["astrbot_plugin_dynamics_horizons"]["badge"] is None
    assert by_id["astrbot_plugin_dynamics_horizons"]["pulse"] is None
    assert by_id["astrbot_plugin_dynamics_games"]["badge"] is None
    assert calls["learning"] == 1

    # Cached within TTL
    second = series_panels.presence(ctx, "astrbot_plugin_chat_dynamics", root=tmp_path)
    assert calls["learning"] == 1
    assert {p["id"]: p for p in second["plugins"]}["astrbot_plugin_dynamics_learning"]["badge"]["count"] == 3

    # Cache invalidation forces re-read
    series_panels.invalidate_presence_cache("astrbot_plugin_dynamics_learning")
    series_panels.presence(ctx, "astrbot_plugin_chat_dynamics", root=tmp_path)
    assert calls["learning"] == 2
    series_panels.invalidate_presence_cache()


def test_chat_dynamics_star_hooks_standalone():
    plugin = _plugin({"takeover_all": False, "takeover_groups": ["10001"]})
    plugin._get_or_create_runtime("qq:GroupMessage:10001", group_id="10001", umo="qq:GroupMessage:10001", bot_id="bot")

    summary = plugin.get_series_summary()
    assert summary["badge"]["count"] >= 1
    assert summary["badge"]["tone"] in {"accent", "warm", "info", "danger", "muted"}
    assert summary["pulse"]["headline"]
    assert summary["pulse"]["updated_at"]

    status = plugin.get_series_scope_status("qq:GroupMessage:10001")
    assert status["enabled"] is True
    assert status["supported"] is True
    assert status["version"]

    hits = plugin.series_search_items("10001")
    assert any(item["kind"] == "group" and item["plugin_id"] == "astrbot_plugin_chat_dynamics" for item in hits)


@pytest.mark.asyncio
async def test_series_matrix_build_and_optimistic_lock_toggle():
    series_panels.invalidate_presence_cache()
    plugin = _plugin({"takeover_all": False, "takeover_groups": ["g1"]})
    plugin._get_or_create_runtime("qq:GroupMessage:g1", group_id="g1", umo="qq:GroupMessage:g1", bot_id="bot")

    learning_state = {"enabled": True, "version": "l-v1"}

    class FakeLearning:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            return {
                "enabled": learning_state["enabled"],
                "supported": True,
                "persona_id": "anan",
                "activity_today": 4,
                "summary": "开启" if learning_state["enabled"] else "暂停",
                "version": learning_state["version"],
            }

        async def set_series_scope_enabled(self, umo: str, enabled: bool, expected_version=None):
            if expected_version and expected_version != learning_state["version"]:
                return {"ok": False, "conflict": True, "version": learning_state["version"]}
            learning_state["enabled"] = enabled
            learning_state["version"] = "l-v2"
            return {"ok": True, "enabled": enabled, "version": "l-v2", "summary": "开启" if enabled else "暂停"}

    class FakeReadonlyWorld:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            return {"enabled": True, "supported": False, "persona_id": "anan", "summary": "运行中"}

    plugin.context.get_all_stars = lambda: [
        _make_star_entry("astrbot_plugin_chat_dynamics", plugin),
        _make_star_entry("astrbot_plugin_dynamics_learning", FakeLearning()),
        _make_star_entry("astrbot_plugin_dynamics_world", FakeReadonlyWorld()),
    ]

    matrix = await build_series_matrix(plugin)
    assert len(matrix["plugins"]) == 8
    row = next(g for g in matrix["groups"] if g["umo"] == "qq:GroupMessage:g1")
    assert row["persona_id"] == "anan"
    assert row["cells"]["astrbot_plugin_chat_dynamics"]["enabled"] is True
    assert row["cells"]["astrbot_plugin_dynamics_learning"]["enabled"] is True
    assert row["cells"]["astrbot_plugin_dynamics_learning"]["supported"] is True
    assert row["cells"]["astrbot_plugin_dynamics_world"]["supported"] is False
    # Missing/unloaded sibling degrades cleanly (unloaded when sibling folder exists on disk)
    assert row["cells"]["astrbot_plugin_dynamics_games"]["state"] in {"missing", "unloaded"}

    # Stale expected_version -> 409 conflict
    code, err = await toggle_series_matrix(
        plugin,
        umo="qq:GroupMessage:g1",
        plugin_id="astrbot_plugin_dynamics_learning",
        enabled=False,
        expected_version="stale-token",
    )
    assert code == 409
    assert err["conflict"] is True

    # Valid expected_version -> toggles sibling cleanly
    code, ok_res = await toggle_series_matrix(
        plugin,
        umo="qq:GroupMessage:g1",
        plugin_id="astrbot_plugin_dynamics_learning",
        enabled=False,
        expected_version="l-v1",
    )
    assert code == 200
    assert ok_res["enabled"] is False
    assert ok_res["version"] == "l-v2"

    # Read-only sibling -> 501 (using updated matrix_version after previous toggle)
    code, ro_res = await toggle_series_matrix(
        plugin,
        umo="qq:GroupMessage:g1",
        plugin_id="astrbot_plugin_dynamics_world",
        enabled=False,
        expected_version=ok_res["matrix_version"],
    )
    assert code == 501

    # Toggle Chat Dynamics itself -> updates takeover_groups cleanly
    fresh = await build_series_matrix(plugin, scope="qq:GroupMessage:g1")
    cd_ver = fresh["groups"][0]["cells"]["astrbot_plugin_chat_dynamics"]["version"]
    code, cd_res = await toggle_series_matrix(
        plugin,
        umo="qq:GroupMessage:g1",
        plugin_id="astrbot_plugin_chat_dynamics",
        enabled=False,
        expected_version=cd_ver,
    )
    assert code == 200
    assert cd_res["enabled"] is False
    assert plugin.is_group_takeover_enabled("g1") is False


@pytest.mark.asyncio
async def test_series_matrix_toggle_web_api_requires_admin(monkeypatch, offline_web_responses):
    plugin = _plugin({"takeover_all": True})
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=False))
    res = await plugin._web.series_matrix_toggle_post()
    assert res["ok"] is False
    assert res["status_code"] == 403


@pytest.mark.asyncio
async def test_federated_series_search_timeout_budget_and_isolation():
    plugin = _plugin({"takeover_all": False, "takeover_groups": ["star-tea"]})
    plugin._get_or_create_runtime("qq:GroupMessage:star-tea", group_id="star-tea", umo="qq:GroupMessage:star-tea", bot_id="bot")

    class FastLearning:
        def series_search_items(self, query: str, scope: str = "", limit: int = 8):
            return [
                {
                    "kind": "proposal",
                    "title": f"提案匹配 {query}",
                    "subtitle": "群间学习 · 待审",
                    "plugin_id": "astrbot_plugin_dynamics_learning",
                    "page": "review",
                    "deep_link": {"tab": "review"},
                }
            ]

    class SlowMemory:
        async def series_search_items(self, query: str, scope: str = "", limit: int = 8):
            await asyncio.sleep(1.5)
            return [{"kind": "memory", "title": "too slow"}]

    class BrokenHorizons:
        def series_search_items(self, query: str, scope: str = "", limit: int = 8):
            raise RuntimeError("broken")

    plugin.context.get_all_stars = lambda: [
        _make_star_entry("astrbot_plugin_chat_dynamics", plugin),
        _make_star_entry("astrbot_plugin_dynamics_learning", FastLearning()),
        _make_star_entry("astrbot_plugin_dynamics_memory", SlowMemory()),
        _make_star_entry("astrbot_plugin_dynamics_horizons", BrokenHorizons()),
    ]

    result = await federated_series_search(plugin, query="star", timeout_seconds=0.12)
    assert result["elapsed_ms"] < 600
    assert "astrbot_plugin_dynamics_memory" in result["timed_out_plugins"]
    plugin_ids = {item["plugin_id"] for item in result["items"]}
    assert "astrbot_plugin_chat_dynamics" in plugin_ids
    assert "astrbot_plugin_dynamics_learning" in plugin_ids


@pytest.mark.asyncio
async def test_series_search_and_matrix_web_endpoints(monkeypatch, offline_web_responses):
    plugin = _plugin({"takeover_all": False, "takeover_groups": ["room-9"]})
    plugin.context.get_all_stars = lambda: [_make_star_entry("astrbot_plugin_chat_dynamics", plugin)]

    # Unauthenticated requests to series_search_get and series_matrix_get are rejected
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=False))
    assert (await plugin._web.series_search_get())["status_code"] == 403
    assert (await plugin._web.series_matrix_get())["status_code"] == 403

    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(web_api, "query_value", lambda name: {"q": "room-9", "limit": "10"}.get(name, ""))
    search_res = await plugin._web.series_search_get()
    assert search_res["ok"] is True
    assert any("room-9" in item["title"] or "room-9" in item["subtitle"] for item in search_res["data"]["items"])

    monkeypatch.setattr(web_api, "query_value", lambda name: "")
    matrix_res = await plugin._web.series_matrix_get()
    assert matrix_res["ok"] is True
    assert len(matrix_res["data"]["plugins"]) == 8
    assert any(g["group_id"] == "room-9" for g in matrix_res["data"]["groups"])


def test_v2_frontend_console_config_and_sunset_contracts():
    console_html = (ROOT / "pages/app/console/index.html").read_text(encoding="utf-8")
    assert 'id="tab-matrix"' in console_html
    assert 'id="view-matrix"' in console_html
    assert 'id="matrixTableBody"' in console_html
    assert 'id="clock"' not in console_html
    assert "本地时间" not in console_html
    assert "对话主控" in console_html

    config_html = (ROOT / "pages/app/config/index.html").read_text(encoding="utf-8")
    assert "对话主控" in config_html

    learning_html = (ROOT / "pages/app/learning/index.html").read_text(encoding="utf-8")
    assert 'id="legacyLearningNotice"' in learning_html
    assert 'id="btnOpenDynamicsLearning"' in learning_html

    memory_html = (ROOT / "pages/app/memory/index.html").read_text(encoding="utf-8")
    assert 'id="legacyMemoryNotice"' in memory_html
    assert 'id="btnOpenDynamicsMemory"' in memory_html


@pytest.mark.asyncio
async def test_series_matrix_prefers_group_effective_persona_over_host_default():
    """Per-group persona reported by sibling plugins must override host default for row label & filtering."""
    series_panels.invalidate_presence_cache()
    scope1 = "qq:GroupMessage:g1"
    scope2 = "qq:GroupMessage:g2"
    plugin = _plugin({"takeover_all": False, "takeover_groups": [scope1, scope2]})
    plugin._get_or_create_runtime(scope1, group_id="g1", umo=scope1, bot_id="bot")
    plugin._get_or_create_runtime(scope2, group_id="g2", umo=scope2, bot_id="bot")
    plugin.get_series_shared_config = lambda: {"persona_id": "host-default"}

    class FakeLearning:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            if umo == scope1:
                return {"enabled": True, "supported": False, "persona_id": "group-persona", "version": 1}
            return {"enabled": True, "supported": False, "persona_id": "", "version": 1}

    class FakeWorldDefault:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            return {"enabled": True, "supported": False, "persona_id": "host-default", "version": 1}

    plugin.context.get_all_stars = lambda: [
        _make_star_entry("astrbot_plugin_chat_dynamics", plugin),
        _make_star_entry("astrbot_plugin_dynamics_world", FakeWorldDefault()),
        _make_star_entry("astrbot_plugin_dynamics_learning", FakeLearning()),
    ]

    matrix = await build_series_matrix(plugin)
    rows = {g["umo"]: g for g in matrix["groups"]}
    # g1 has a group-specific persona from learning -> overrides host-default
    assert rows[scope1]["persona_id"] == "group-persona"
    assert rows[scope1]["cells"]["astrbot_plugin_dynamics_learning"]["persona_id"] == "group-persona"
    # g2 has no group-specific override -> falls back cleanly to host-default
    assert rows[scope2]["persona_id"] == "host-default"

    filtered_group = await build_series_matrix(plugin, persona="group-persona")
    assert [g["umo"] for g in filtered_group["groups"]] == [scope1]

    filtered_default = await build_series_matrix(plugin, persona="host-default")
    assert [g["umo"] for g in filtered_default["groups"]] == [scope2]


@pytest.mark.asyncio
async def test_federated_series_search_isolates_slow_sync_plugin_without_blocking_loop():
    """A slow synchronous search hook must run off the event loop, time out on budget, and keep fast results."""
    import time as _time

    plugin = _plugin({"takeover_all": False, "takeover_groups": ["star-sync"]})
    plugin._get_or_create_runtime(
        "qq:GroupMessage:star-sync", group_id="star-sync", umo="qq:GroupMessage:star-sync", bot_id="bot"
    )

    class FastSyncLearning:
        def series_search_items(self, query: str, scope: str = "", limit: int = 8):
            return [
                {
                    "kind": "proposal",
                    "title": f"快插件命中 {query}",
                    "subtitle": "群间学习 · 待审",
                    "plugin_id": "astrbot_plugin_dynamics_learning",
                    "page": "review",
                    "deep_link": {"tab": "review", "id": "p-1"},
                }
            ]

    class SlowSyncMemory:
        def series_search_items(self, query: str, scope: str = "", limit: int = 8):
            _time.sleep(0.8)
            return [{"kind": "memory", "title": f"慢同步 {query}"}]

    plugin.context.get_all_stars = lambda: [
        _make_star_entry("astrbot_plugin_chat_dynamics", plugin),
        _make_star_entry("astrbot_plugin_dynamics_learning", FastSyncLearning()),
        _make_star_entry("astrbot_plugin_dynamics_memory", SlowSyncMemory()),
    ]

    ticks = 0

    async def _heartbeat():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.02)
            ticks += 1

    hb = asyncio.create_task(_heartbeat())
    t0 = _time.monotonic()
    result = await federated_series_search(plugin, query="star", timeout_seconds=0.12)
    wall_ms = (_time.monotonic() - t0) * 1000.0
    await hb

    # Must return near the 120ms budget, NOT wait 800ms for SlowSyncMemory.
    assert wall_ms < 500
    assert result["elapsed_ms"] < 500
    # Event loop remained responsive while SlowSyncMemory was sleeping in a worker thread.
    assert ticks >= 3
    assert "astrbot_plugin_dynamics_memory" in result["timed_out_plugins"]
    plugin_ids = {item["plugin_id"] for item in result["items"]}
    assert "astrbot_plugin_chat_dynamics" in plugin_ids
    assert "astrbot_plugin_dynamics_learning" in plugin_ids
    assert "astrbot_plugin_dynamics_memory" not in plugin_ids


@pytest.mark.asyncio
async def test_series_matrix_toggle_excludes_group_under_takeover_all():
    """Toggling off a group when takeover_all=True must exclude it and report enabled=False."""
    series_panels.invalidate_presence_cache()
    plugin = _plugin({"takeover_all": True})
    scope = "qq:GroupMessage:room42"
    plugin._get_or_create_runtime(scope, group_id="room42", umo=scope, bot_id="bot")
    plugin.context.get_all_stars = lambda: [_make_star_entry("astrbot_plugin_chat_dynamics", plugin)]

    before = await build_series_matrix(plugin, scope=scope)
    cd_cell = before["groups"][0]["cells"]["astrbot_plugin_chat_dynamics"]
    assert cd_cell["enabled"] is True

    code, toggled = await toggle_series_matrix(
        plugin,
        umo=scope,
        plugin_id="astrbot_plugin_chat_dynamics",
        enabled=False,
        expected_version=cd_cell["version"],
    )
    assert code == 200
    assert toggled["enabled"] is False
    assert plugin.is_group_takeover_enabled("room42") is False

    after = await build_series_matrix(plugin, scope=scope)
    assert after["groups"][0]["cells"]["astrbot_plugin_chat_dynamics"]["enabled"] is False


