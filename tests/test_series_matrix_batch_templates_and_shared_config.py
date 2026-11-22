"""Unit tests for concurrent series matrix, cache invalidation, batch toggle, templates CRUD, and shared config helpers."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import astrbot_plugin_chat_dynamics.core.web_api as web_api
import astrbot_plugin_chat_dynamics.series_panels as series_panels
from astrbot_plugin_chat_dynamics.core.series_hub import (
    batch_toggle_series_matrix,
    build_series_matrix,
    invalidate_series_matrix_cache,
    toggle_series_matrix,
)
from astrbot_plugin_chat_dynamics.core.series_templates import (
    BUILTIN_TEMPLATE_IDS,
    SeriesTemplateStore,
)
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin


def _make_star_entry(
    identity: str, owner: object, *, activated: bool = True, version: str = "2.0.0"
):
    return SimpleNamespace(
        name=identity,
        activated=activated,
        version=version,
        star_cls=owner,
    )


@pytest.mark.asyncio
async def test_series_matrix_concurrent_fetch_and_ttl_cache_invalidation():
    series_panels.invalidate_presence_cache()
    invalidate_series_matrix_cache()
    plugin = _plugin(
        {
            "takeover_all": False,
            "takeover_groups": ["qq:GroupMessage:g1", "qq:GroupMessage:g2"],
        }
    )
    plugin._get_or_create_runtime("qq:GroupMessage:g1", group_id="g1", umo="qq:GroupMessage:g1", bot_id="bot")
    plugin._get_or_create_runtime("qq:GroupMessage:g2", group_id="g2", umo="qq:GroupMessage:g2", bot_id="bot")

    hook_calls = {"count": 0}

    class SlowAsyncSibling:
        def __init__(self, pid: str, *, fail: bool = False):
            self.pid = pid
            self.fail = fail

        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        async def get_series_scope_status(self, umo: str):
            hook_calls["count"] += 1
            await asyncio.sleep(0.06)
            if self.fail:
                raise RuntimeError("boom")
            return {"enabled": True, "supported": True, "version": "v1", "summary": "开启"}

        async def set_series_scope_enabled(self, umo: str, enabled: bool, expected_version=None):
            return {"ok": True, "enabled": enabled, "version": "v2"}

    siblings = [
        ("astrbot_plugin_dynamics_world", SlowAsyncSibling("world")),
        ("astrbot_plugin_dynamics_learning", SlowAsyncSibling("learning")),
        ("astrbot_plugin_dynamics_memory", SlowAsyncSibling("memory")),
        ("astrbot_plugin_dynamics_horizons", SlowAsyncSibling("horizons", fail=True)),
        ("astrbot_plugin_dynamics_games", SlowAsyncSibling("games")),
    ]
    plugin.context.get_all_stars = lambda: [
        _make_star_entry("astrbot_plugin_chat_dynamics", plugin),
        *(_make_star_entry(pid, owner) for pid, owner in siblings),
    ]

    # 2 groups x 5 siblings x 60ms = 600ms if sequential; concurrent must finish well under 300ms
    t0 = time.monotonic()
    first = await build_series_matrix(plugin)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.35
    assert hook_calls["count"] == 10
    row1 = next(g for g in first["groups"] if g["umo"] == "qq:GroupMessage:g1")
    assert row1["cells"]["astrbot_plugin_dynamics_horizons"]["state"] == "error"
    assert row1["cells"]["astrbot_plugin_dynamics_world"]["enabled"] is True

    # Second call within 3s TTL hits cache (zero new hook calls)
    second = await build_series_matrix(plugin)
    assert hook_calls["count"] == 10
    assert second["version"] == first["version"]

    # Clearing presence cache immediately invalidates the 3s matrix cache
    series_panels.invalidate_presence_cache()
    third = await build_series_matrix(plugin)
    assert hook_calls["count"] == 20
    assert third["version"] == first["version"]

    # Mutation via toggle_series_matrix invalidates cache immediately
    cd_ver = row1["cells"]["astrbot_plugin_chat_dynamics"]["version"]
    code, _ = await toggle_series_matrix(
        plugin,
        umo="qq:GroupMessage:g1",
        plugin_id="astrbot_plugin_chat_dynamics",
        enabled=False,
        expected_version=cd_ver,
    )
    assert code == 200
    calls_after_toggle = hook_calls["count"]
    await build_series_matrix(plugin, force_refresh=True)
    assert hook_calls["count"] > calls_after_toggle


@pytest.mark.asyncio
async def test_series_matrix_displays_core_exclusion_and_batch_toggle_details():
    series_panels.invalidate_presence_cache()
    invalidate_series_matrix_cache()
    plugin = _plugin({"takeover_all": True, "exclude_groups": ["g_ex"]})
    plugin._get_or_create_runtime("qq:GroupMessage:g_ex", group_id="g_ex", umo="qq:GroupMessage:g_ex", bot_id="bot")
    plugin._get_or_create_runtime("qq:GroupMessage:g_ok", group_id="g_ok", umo="qq:GroupMessage:g_ok", bot_id="bot")

    mem_state = {"enabled": True, "version": "m1"}

    class ToggleMemory:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            return {"enabled": mem_state["enabled"], "supported": True, "version": mem_state["version"], "summary": "开启" if mem_state["enabled"] else "暂停"}

        async def set_series_scope_enabled(self, umo: str, enabled: bool, expected_version=None):
            mem_state["enabled"] = enabled
            mem_state["version"] = "m2"
            return {"ok": True, "enabled": enabled, "version": "m2", "summary": "开启" if enabled else "暂停"}

    class ReadonlyWorld:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            return {"enabled": True, "supported": False, "summary": "运行中"}

    class ConflictLearning:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            return {"enabled": True, "supported": True, "version": "l1", "summary": "开启"}

        async def set_series_scope_enabled(self, umo: str, enabled: bool, expected_version=None):
            return {"ok": False, "conflict": True, "version": "l99", "summary": "版本冲突"}

    class BrokenGames:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_scope_status(self, umo: str):
            return {"enabled": True, "supported": True, "version": "g1", "summary": "开启"}

        async def set_series_scope_enabled(self, umo: str, enabled: bool, expected_version=None):
            raise RuntimeError("disk io failed")

    plugin.context.get_all_stars = lambda: [
        _make_star_entry("astrbot_plugin_chat_dynamics", plugin),
        _make_star_entry("astrbot_plugin_dynamics_memory", ToggleMemory()),
        _make_star_entry("astrbot_plugin_dynamics_world", ReadonlyWorld()),
        _make_star_entry("astrbot_plugin_dynamics_learning", ConflictLearning()),
        _make_star_entry("astrbot_plugin_dynamics_games", BrokenGames()),
    ]

    matrix = await build_series_matrix(plugin)
    ex_row = next(g for g in matrix["groups"] if g["umo"] == "qq:GroupMessage:g_ex")
    assert ex_row["excluded_by_core"] is True
    assert ex_row["cells"]["astrbot_plugin_chat_dynamics"]["excluded_by_core"] is True
    assert "主控排除" in ex_row["cells"]["astrbot_plugin_chat_dynamics"]["summary"]
    assert "主控排除" in ex_row["cells"]["astrbot_plugin_dynamics_memory"]["summary"]

    ok_row = next(g for g in matrix["groups"] if g["umo"] == "qq:GroupMessage:g_ok")
    # Stale expected_version -> 409
    code_conflict, err_conflict = await batch_toggle_series_matrix(
        plugin,
        umo="qq:GroupMessage:g_ok",
        expected_version="stale-ver",
        template_id="core_only",
    )
    assert code_conflict == 409
    assert err_conflict["conflict"] is True

    # Valid expected_version applying core_only template
    code, res = await batch_toggle_series_matrix(
        plugin,
        umo="qq:GroupMessage:g_ok",
        expected_version=ok_row["version"],
        template_id="core_only",
    )
    assert code == 200
    assert res["ok"] is True
    assert res["template_id"] == "core_only"
    applied_ids = {item["plugin_id"] for item in res["applied"]}
    skipped_map = {item["plugin_id"]: item["reason"] for item in res["skipped"]}
    failed_ids = {item["plugin_id"] for item in res["failed"]}

    assert {"astrbot_plugin_chat_dynamics", "astrbot_plugin_dynamics_memory"} <= applied_ids
    assert mem_state["enabled"] is False
    assert skipped_map["astrbot_plugin_dynamics_world"] == "readonly"
    assert skipped_map["astrbot_plugin_dynamics_learning"] == "conflict"
    assert skipped_map["astrbot_plugin_dynamics_horizons"] == "uninstalled"
    assert "astrbot_plugin_dynamics_games" in failed_ids
    assert res["matrix_version"]
    assert res["row"]["umo"] == "qq:GroupMessage:g_ok"


def test_series_templates_builtins_and_custom_crud(tmp_path):
    store_path = tmp_path / "series-templates.json"
    store = SeriesTemplateStore(store_path)

    all_tpls = store.list_all()
    assert [t["id"] for t in all_tpls] == [
        "full_companion",
        "light_chat",
        "observer_archive",
        "core_only",
    ]
    assert all(t["builtin"] and t["readonly"] for t in all_tpls)
    assert all(store.get("full_companion")["states"].values())
    assert store.get("light_chat")["states"]["astrbot_plugin_visual_media"] is True
    assert store.get("light_chat")["states"]["astrbot_plugin_dynamics_games"] is False
    assert store.get("observer_archive")["states"]["astrbot_plugin_group_chronicle"] is True
    assert store.get("observer_archive")["states"]["astrbot_plugin_dynamics_world"] is False
    assert sum(store.get("core_only")["states"].values()) == 1

    # Built-ins are immutable
    for bid in BUILTIN_TEMPLATE_IDS:
        with pytest.raises(ValueError):
            store.upsert({"id": bid, "name": "hack", "states": {"astrbot_plugin_chat_dynamics": True}})
        with pytest.raises(ValueError):
            store.delete(bid)

    # Create & update custom template with atomic persistence
    created = store.upsert(
        {
            "id": "study_room",
            "name": "自习室",
            "description": "仅开主控与学习",
            "strategy_hint": "专注自习",
            "states": {
                "astrbot_plugin_chat_dynamics": True,
                "astrbot_plugin_dynamics_learning": True,
            },
        }
    )
    assert created["id"] == "study_room"
    assert created["builtin"] is False
    assert created["readonly"] is False
    assert created["states"]["astrbot_plugin_dynamics_learning"] is True
    assert created["states"]["astrbot_plugin_dynamics_games"] is False

    # Reload from disk
    reloaded = SeriesTemplateStore(store_path)
    loaded = reloaded.get("study_room")
    assert loaded is not None
    assert loaded["name"] == "自习室"

    # Delete custom template
    assert reloaded.delete("study_room") is True
    assert reloaded.delete("study_room") is False
    assert reloaded.get("study_room") is None


def test_shared_config_timezone_quiet_hours_and_series_panels_helpers():
    # Use deterministic wall clock via time_service
    # 2026-11-22 02:30 Asia/Shanghai (UTC+8) = 2026-11-21 18:30 UTC
    shanghai_night_ts = datetime(2026, 11, 22, 2, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    # 2026-11-22 14:30 Asia/Shanghai (UTC+8) = 2026-11-22 06:30 UTC
    shanghai_day_ts = datetime(2026, 11, 22, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    plugin = _plugin(
        {
            "takeover_all": False,
            "takeover_groups": ["10001"],
            "exclude_groups": ["99999", "qq:GroupMessage:88888"],
            "reply_provider": "prov-reply",
            "rhythm_timezone": "Invalid/Zone_Name",
            "daily_rhythm_enabled": True,
            "rhythm_force_sleep": False,
        }
    )
    plugin.time_service = SimpleNamespace(time=lambda: 100.0, wall_time=lambda: shanghai_night_ts)

    shared = plugin.get_series_shared_config()
    # Invalid timezone falls back to Asia/Shanghai
    assert shared["rhythm_timezone"] == "Asia/Shanghai"
    assert shared["quiet_hours"]["in_quiet_hours"] is True
    assert shared["quiet_hours"]["reason"] == "overnight_sleep_window"
    assert shared["quiet_hours"]["local_hour"] == 2

    # Daytime -> awake (in_quiet_hours=False)
    plugin.time_service = SimpleNamespace(time=lambda: 100.0, wall_time=lambda: shanghai_day_ts)
    assert plugin.get_series_shared_config()["quiet_hours"]["in_quiet_hours"] is False

    # force_sleep=True overrides daytime -> in_quiet_hours=True
    plugin._runtime_config = plugin._runtime_config.__class__(
        **{**plugin._runtime_config.__dict__, "rhythm_force_sleep": True}
    )
    assert plugin.get_series_shared_config()["quiet_hours"]["in_quiet_hours"] is True
    assert plugin.get_series_shared_config()["quiet_hours"]["reason"] == "force_sleep"

    # daily_rhythm_enabled=False disables quiet hours even if force_sleep=True
    plugin._runtime_config = plugin._runtime_config.__class__(
        **{**plugin._runtime_config.__dict__, "daily_rhythm_enabled": False}
    )
    assert plugin.get_series_shared_config()["quiet_hours"]["in_quiet_hours"] is False

    # Re-enable daily rhythm at night with America/New_York (where 18:30 UTC is 13:30 EST -> awake!)
    plugin._runtime_config = plugin._runtime_config.__class__(
        **{
            **plugin._runtime_config.__dict__,
            "daily_rhythm_enabled": True,
            "rhythm_force_sleep": False,
            "rhythm_timezone": "America/New_York",
        }
    )
    plugin.time_service = SimpleNamespace(time=lambda: 100.0, wall_time=lambda: shanghai_night_ts)
    ny_shared = plugin.get_series_shared_config()
    assert ny_shared["rhythm_timezone"] == "America/New_York"
    assert ny_shared["quiet_hours"]["in_quiet_hours"] is False

    # Verify series_panels helpers against this ChatDynamics owner
    ctx = SimpleNamespace(
        get_all_stars=lambda: [_make_star_entry("astrbot_plugin_chat_dynamics", plugin)]
    )
    assert series_panels.resolve_series_timezone_name(ctx) == "America/New_York"
    assert series_panels.resolve_series_timezone_name(ctx, explicit_tz="Asia/Tokyo") == "Asia/Tokyo"
    assert getattr(series_panels.resolve_series_timezone(ctx), "key", "") == "America/New_York"
    assert series_panels.resolve_series_provider(ctx) == "prov-reply"
    assert series_panels.resolve_series_provider(ctx, explicit_provider="own-p") == "own-p"
    assert series_panels.is_series_group_excluded(ctx, umo="qq:GroupMessage:99999") is True
    assert series_panels.is_series_group_excluded(ctx, group_id="88888") is True
    assert series_panels.is_series_group_excluded(ctx, umo="qq:GroupMessage:10001") is False
    assert series_panels.is_series_in_quiet_hours(ctx) is False

    # Standalone (no Chat Dynamics) defaults cleanly
    empty_ctx = SimpleNamespace(get_all_stars=lambda: [])
    assert series_panels.resolve_series_timezone_name(empty_ctx) == "Asia/Shanghai"
    assert series_panels.resolve_series_provider(empty_ctx, fallback_provider="fb") == "fb"
    assert series_panels.is_series_group_excluded(empty_ctx, umo="qq:GroupMessage:99999") is False
    assert series_panels.is_series_in_quiet_hours(empty_ctx) is False


@pytest.mark.asyncio
async def test_web_endpoints_telemetry_batch_and_templates_auth_and_crud(
    monkeypatch, offline_web_responses, tmp_path
):
    plugin = _plugin({"takeover_all": False, "takeover_groups": ["room-1"]})
    plugin.series_templates = SeriesTemplateStore(tmp_path / "tpl.json")
    plugin.context.get_all_stars = lambda: [_make_star_entry("astrbot_plugin_chat_dynamics", plugin)]

    # All 3 endpoints require admin
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=False))
    assert (await plugin._web.series_telemetry_get())["status_code"] == 403
    assert (await plugin._web.series_matrix_batch_post())["status_code"] == 403
    assert (await plugin._web.series_templates_get())["status_code"] == 403
    assert (await plugin._web.series_templates_post())["status_code"] == 403
    assert (await plugin._web.series_templates_delete())["status_code"] == 403

    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", AsyncMock(return_value=True))

    # Telemetry GET validation + happy path
    monkeypatch.setattr(web_api, "query_value", lambda name: {"window": "bad"}.get(name, ""))
    assert (await plugin._web.series_telemetry_get())["status_code"] == 400
    monkeypatch.setattr(web_api, "query_value", lambda name: {"window": "24h"}.get(name, ""))
    tel = await plugin._web.series_telemetry_get()
    assert tel["ok"] is True
    assert tel["data"]["window"] == "24h"
    assert "live_queue" in tel["data"]

    # Templates GET + POST create + POST delete + DELETE
    tpl_list = await plugin._web.series_templates_get()
    assert tpl_list["ok"] is True
    assert len(tpl_list["data"]["templates"]) == 4

    monkeypatch.setattr(
        web_api,
        "_json_body",
        AsyncMock(
            return_value={
                "id": "my_tpl",
                "name": "自定义测试",
                "states": {"astrbot_plugin_chat_dynamics": True},
            }
        ),
    )
    created = await plugin._web.series_templates_post()
    assert created["ok"] is True
    assert created["data"]["template"]["id"] == "my_tpl"

    # Batch apply custom template
    monkeypatch.setattr(
        web_api,
        "_json_body",
        AsyncMock(return_value={"umo": "qq:GroupMessage:room-1", "template_id": "my_tpl"}),
    )
    batched = await plugin._web.series_matrix_batch_post()
    assert batched["ok"] is True
    assert batched["data"]["template_id"] == "my_tpl"

    # Delete via POST action=delete (workspace-safe)
    monkeypatch.setattr(
        web_api,
        "_json_body",
        AsyncMock(return_value={"action": "delete", "id": "my_tpl"}),
    )
    deleted = await plugin._web.series_templates_post()
    assert deleted["ok"] is True
    assert deleted["data"]["deleted"] is True

    # Re-create and delete via HTTP DELETE query param
    monkeypatch.setattr(
        web_api,
        "_json_body",
        AsyncMock(
            return_value={
                "id": "my_tpl2",
                "name": "自定义2",
                "states": {"astrbot_plugin_chat_dynamics": True},
            }
        ),
    )
    assert (await plugin._web.series_templates_post())["ok"] is True
    monkeypatch.setattr(web_api, "query_value", lambda name: {"id": "my_tpl2"}.get(name, ""))
    del_http = await plugin._web.series_templates_delete()
    assert del_http["ok"] is True
    assert del_http["data"]["deleted"] is True
