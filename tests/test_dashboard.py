"""Tests for the plugin page snapshot layer and Web APIs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import astrbot_plugin_chat_dynamics.core.web_api as web_api

from astrbot_plugin_chat_dynamics.core.dashboard import (
    _mask_identifier,
    snapshot_overview,
    snapshot_session_or_none,
)
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin, PLUGIN_NAME
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockContext, _plugin


@pytest.fixture(autouse=True)
def deterministic_web_responses(monkeypatch):
    """Keep handler unit tests independent of Quart/Starlette app contexts."""

    def json_response(data=None, *, status_code=200, headers=None):
        payload = dict(data) if isinstance(data, dict) else {"data": data}
        if status_code != 200:
            payload["status_code"] = status_code
        return payload

    def error_response(message, *, status_code=400, data=None, headers=None):
        return json_response(
            {"status": "error", "message": message, "data": data, "ok": False, "error": message},
            status_code=status_code,
            headers=headers,
        )

    monkeypatch.setattr(web_api, "json_response", json_response)
    monkeypatch.setattr(web_api, "error_response", error_response)


def test_overview_empty_whitelist():
    plugin = ChatDynamicsPlugin(
        context=MockContext(),
        config={"enable": True, "takeover_all": False, "takeover_groups": []},
    )
    data = snapshot_overview(plugin)
    assert data["enabled"] is True
    assert data["takeover_all"] is False
    assert data["session_count"] == 0
    assert data["sessions"] == []


def test_overview_includes_live_session_metrics():
    plugin = _plugin({"takeover_all": True, "console_show_message_content": True})
    plugin.vibe_analyzer.record_message("group_live", "哈哈 😂", timestamp=plugin.time_service.time())
    plugin.vibe_analyzer.record_message("group_live", "这个梗绝了", timestamp=plugin.time_service.time())
    plugin.arbiter.trigger_cooling("group_live", duration_seconds=600, current_time=plugin.time_service.time())
    dag = plugin._get_or_create_dag("group_live")
    dag.add_message("m1", "u1", "这个梗绝了", timestamp=plugin.time_service.time())

    data = snapshot_overview(plugin)
    assert data["session_count"] >= 1
    assert data["cooling_count"] >= 1
    row = next(item for item in data["sessions"] if item["session_id"] == "group_live")
    assert row["cooling"] is True
    assert row["dag_nodes"] == 1
    assert len(row["rate_series"]) == 12
    assert row["mode_source"] == "telemetrics"
    assert row["vibe_llm_snapshot_count"] == 0
    assert row["vibe_llm_in_flight"] is False
    assert data["vibe_llm_enabled"] is False
    assert data["pipeline_mode"] == "filter"
    assert "unicode_emoji_ratio" in row
    assert "media_ratio" in row
    assert "scene_tags" in row
    assert "emotion_tags" in row


def test_session_detail_includes_dag_nodes():
    plugin = _plugin({"takeover_all": True, "console_show_message_content": True})
    dag = plugin._get_or_create_dag("g1")
    dag.add_message("a", "u1", "先说一句", timestamp=1.0)
    dag.add_message("b", "bot_42", "我接上了", timestamp=2.0, reply_to_id="a")
    plugin.addressivity_router.bot_id = "bot_42"
    plugin._last_bot_nodes["g1"] = dag.get_node("b")

    detail = snapshot_session_or_none(plugin, "g1")
    assert detail is not None
    assert detail["last_bot_text"]
    assert any(node["is_bot"] for node in detail["nodes"])
    bot_node = next(node for node in detail["nodes"] if node["is_bot"])
    assert bot_node["parent_ids"] == ["a"]


def test_missing_session_id_is_none():
    plugin = _plugin()
    assert snapshot_session_or_none(plugin, "") is None


@pytest.mark.parametrize(
    ("identifier", "masked"),
    (("a", "*"), ("abc", "***"), ("abcd", "****")),
)
def test_short_identifiers_are_fully_masked(identifier, masked):
    assert _mask_identifier(identifier) == masked


@pytest.mark.asyncio
async def test_web_api_overview_and_session_guard():
    plugin = _plugin({"takeover_all": True, "console_show_message_content": True})
    plugin.vibe_analyzer.set_mode("room_9", GroupChatMode.FAST_BANTER)
    plugin._get_or_create_dag("room_9").add_message("n1", "u1", "hi", timestamp=1.0)

    overview = await plugin.web_api_overview()
    assert overview["ok"] is True
    assert overview["status"] == "ok"
    assert overview["error"] is None
    assert overview["data"]["session_count"] >= 1
    for metric in ("message_received", "speech_withheld", "send_failed", "session_evicted", "rate_limited", "active_tasks"):
        assert metric in overview["data"]["metrics"]

    missing = await plugin.web_api_session()
    assert missing["ok"] is False
    assert missing["data"] is None
    assert missing["status_code"] == 400

    plugin.arbiter.trigger_cooling("room_9", duration_seconds=900, current_time=plugin.time_service.time())
    detail = snapshot_session_or_none(plugin, "room_9")
    assert detail["cooling"] is True
    plugin._reset_session_state("room_9")
    after = snapshot_session_or_none(plugin, "room_9")
    assert after["dag_nodes"] == 0
    assert after["cooling"] is False


@pytest.mark.asyncio
async def test_web_api_registers_console_routes():
    ctx = MockContext()
    ChatDynamicsPlugin(context=ctx, config={"enable": True})
    routes = [item[0] for item in ctx.web_routes]
    assert f"/{PLUGIN_NAME}/overview" in routes
    assert f"/{PLUGIN_NAME}/sessions" in routes
    assert f"/{PLUGIN_NAME}/session" in routes
    assert f"/{PLUGIN_NAME}/cool" in routes
    assert f"/{PLUGIN_NAME}/reset" in routes
    assert f"/{PLUGIN_NAME}/presets" in routes
    assert f"/{PLUGIN_NAME}/preset/apply" in routes
    assert f"/{PLUGIN_NAME}/page_nav" in routes
    assert f"/{PLUGIN_NAME}/replay" in routes


def test_live_config_changes_takeover():
    cfg = {"enable": True, "takeover_all": True, "takeover_groups": []}
    plugin = ChatDynamicsPlugin(context=MockContext(), config=cfg)
    assert plugin.is_group_takeover_enabled("g1") is True
    cfg["enable"] = False
    plugin.refresh_config()
    assert plugin.is_group_takeover_enabled("g1") is False
    cfg["enable"] = True
    cfg["takeover_all"] = False
    plugin.refresh_config()
    assert plugin.is_group_takeover_enabled("g1") is False


def test_idle_sessions_are_pruned():
    plugin = _plugin({"takeover_all": True})
    dag = plugin._get_or_create_dag("old_room")
    dag.add_message("m1", "u1", "hi", timestamp=10.0)
    plugin._vibe_msg_counts["old_room"] = 3
    plugin._umo_by_session["old_room"] = "mock:old_room"
    plugin._prune_idle_sessions(now=10.0 + 3600.0 + 5.0)
    assert "old_room" not in plugin.dags
    assert "old_room" not in plugin._vibe_msg_counts


def test_console_page_files_exist():
    root = Path(__file__).resolve().parents[1] / "pages" / "console"
    for name in ("index.html", "app.js", "style.css", "_page.json"):
        assert (root / name).is_file()
    html = (root / "index.html").read_text(encoding="utf-8")
    assert "群聊动态控制台" in html
    assert "./app.js" in html
    assert "./style.css" in html


@pytest.mark.asyncio
async def test_web_api_rejects_unknown_session_and_invalid_minutes(monkeypatch):
    plugin = _plugin({"takeover_all": True})

    async def unknown_body():
        return {"session_key": "unknown", "minutes": 15}

    monkeypatch.setattr(web_api, "_json_body", unknown_body)
    unknown = await plugin.web_api_cool()
    assert unknown["ok"] is False
    assert unknown["status"] == "error"
    assert unknown["error"] == "unknown session"
    assert unknown["status_code"] == 404

    key = "mock:GroupMessage:g1"
    plugin._get_or_create_runtime(key, group_id="g1", umo=key, bot_id="bot")

    async def invalid_body():
        return {"session_key": key, "minutes": float("nan")}

    monkeypatch.setattr(web_api, "_json_body", invalid_body)
    invalid = await plugin.web_api_cool()
    assert invalid["ok"] is False
    assert invalid["status_code"] == 400


@pytest.mark.asyncio
async def test_web_api_returns_503_during_shutdown():
    plugin = _plugin()
    plugin._shutting_down = True
    result = await plugin.web_api_overview()
    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"] == "plugin is shutting down"
    assert result["status_code"] == 503


def test_console_uses_bridge_without_direct_fetch_fallback():
    js = (Path(__file__).resolve().parents[1] / "pages" / "console" / "app.js").read_text(encoding="utf-8")
    assert "fetch(" not in js
    assert "session_key" in js
    assert "detailRequestToken" in js
    assert "withTimeout" in js
    assert "REQUEST_TIMEOUT_MS" in js
    assert "badgeVibe" in js
    assert "vibe_llm_snapshot_count" in js
    assert "payload.status === \"error\"" in js
    assert "escapeHtml" in js
    assert js.count("bridge.apiPost(endpoint, body)") == 1
    assert "/api/" not in js


@pytest.mark.asyncio
async def test_web_api_uses_keyword_only_status_code(monkeypatch):
    """Official astrbot.api.web.json_response rejects positional status_code."""
    import astrbot_plugin_chat_dynamics.core.web_api as web_api_mod

    def official_json_response(data=None, *, status_code=200, headers=None):
        payload = dict(data) if isinstance(data, dict) else {"data": data}
        payload["status_code"] = status_code
        return payload

    monkeypatch.setattr(web_api_mod, "json_response", official_json_response)
    plugin = _plugin({"takeover_all": True})
    result = await plugin.web_api_overview()
    assert result["status"] == "ok"
    assert result["status_code"] == 200
    assert result["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_web_api_cool_success_and_does_not_invent_session(monkeypatch):
    plugin = _plugin({"takeover_all": True})
    key = "mock:GroupMessage:g1"
    plugin._get_or_create_runtime(key, group_id="g1", umo=key, bot_id="bot")

    async def ok_body():
        return {"session_key": key, "minutes": 20}

    monkeypatch.setattr(web_api, "_json_body", ok_body)
    result = await plugin.web_api_cool()
    assert result["ok"] is True
    assert result["error"] is None
    assert plugin.arbiter.is_in_deep_cooling(key, current_time=plugin.time_service.time()) is True

    listed_only = _plugin({"takeover_all": False, "takeover_groups": ["listed"]})

    async def listed_body():
        return {"session_key": "listed", "minutes": 15}

    monkeypatch.setattr(web_api, "_json_body", listed_body)
    missing = await listed_only.web_api_cool()
    assert missing["status_code"] == 404
    assert "listed" not in listed_only._sessions


@pytest.mark.asyncio
async def test_reset_invalidates_and_cancels_pending_generation():
    plugin = _plugin({"takeover_all": True})
    key = "mock:GroupMessage:g1"
    runtime = plugin._get_or_create_runtime(key, group_id="g1", umo=key, bot_id="bot")
    runtime.revision = 7
    pending_task = asyncio.create_task(asyncio.sleep(60))
    runtime.generation_task = pending_task
    plugin._in_flight.add(key)
    plugin._get_or_create_dag(key).add_message("n1", "u1", "待回复", timestamp=1.0)

    plugin._reset_session_state(key)
    await asyncio.gather(pending_task, return_exceptions=True)

    assert pending_task.cancelled()
    assert runtime.revision == 8
    assert runtime.generation_task is None
    assert key not in plugin._in_flight
    assert runtime.dag is not None and runtime.dag.nodes == {}


@pytest.mark.asyncio
async def test_reset_cancels_in_flight_vibe_classification():
    plugin = _plugin({"takeover_all": True})
    key = "mock:GroupMessage:g1"
    plugin._get_or_create_runtime(key, group_id="g1", umo=key, bot_id="bot")
    vibe_task = asyncio.create_task(asyncio.sleep(60))
    plugin._vibe_llm_tasks.add(vibe_task)
    plugin._vibe_llm_tasks_by_session[key] = vibe_task

    plugin._reset_session_state(key)
    await asyncio.gather(vibe_task, return_exceptions=True)

    assert vibe_task.cancelled()
    assert key not in plugin._vibe_llm_tasks_by_session


@pytest.mark.asyncio
async def test_web_api_cool_invalidates_generation_and_checks_resolved_group(monkeypatch):
    plugin = _plugin({"takeover_all": True})
    key = "mock:GroupMessage:g1"
    runtime = plugin._get_or_create_runtime(key, group_id="g1", umo=key, bot_id="bot")
    runtime.revision = 2
    pending_task = asyncio.create_task(asyncio.sleep(60))
    runtime.generation_task = pending_task

    async def cool_body():
        return {"session_key": key, "minutes": 10}

    monkeypatch.setattr(web_api, "_json_body", cool_body)
    result = await plugin.web_api_cool()
    await asyncio.gather(pending_task, return_exceptions=True)

    assert result["ok"] is True
    assert pending_task.cancelled()
    assert runtime.revision == 3
    assert runtime.generation_task is None

    bypass = _plugin({"takeover_all": False, "takeover_groups": ["listed-key"]})
    bypass._get_or_create_runtime("listed-key", group_id="excluded-group", umo="listed-key", bot_id="bot")

    async def bypass_body():
        return {"session_key": "listed-key", "minutes": 10}

    monkeypatch.setattr(web_api, "_json_body", bypass_body)
    rejected = await bypass.web_api_cool()
    assert rejected["status_code"] == 404


@pytest.mark.asyncio
async def test_cool_cancels_in_flight_vibe_before_returning():
    plugin = _plugin({"takeover_all": True, "vibe_llm_enabled": True})
    key = "mock:GroupMessage:g1"
    plugin._get_or_create_runtime(key, group_id="g1", umo=key, bot_id="bot")
    plugin._vibe_msg_counts[key] = 12
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_refresh(session_id, text, now):
        started.set()
        await release.wait()
        plugin.vibe_analyzer.mark_llm_snapshot(session_id, now)

    plugin._refresh_vibe_from_llm = slow_refresh
    plugin._schedule_vibe_llm(key, "in flight", now=500.0)
    task = plugin._vibe_llm_tasks_by_session[key]
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert await plugin._cool_session_async(key, 10.0) is True
    assert task.cancelled()
    assert key not in plugin._vibe_llm_tasks_by_session
    assert plugin.vibe_analyzer.has_llm_snapshot(key) is False


@pytest.mark.asyncio
async def test_web_api_reset_returns_cleared_detail(monkeypatch):
    plugin = _plugin({"takeover_all": True, "console_show_message_content": True})
    key = "mock:GroupMessage:g1"
    plugin._get_or_create_runtime(key, group_id="g1", umo=key, bot_id="bot")
    plugin._get_or_create_dag(key).add_message("n1", "u1", "消息", timestamp=1.0)
    plugin.arbiter.trigger_cooling(key, duration_seconds=60, current_time=plugin.time_service.time())

    async def reset_body():
        return {"session_key": key}

    monkeypatch.setattr(web_api, "_json_body", reset_body)
    result = await plugin.web_api_reset()

    assert result["ok"] is True
    assert result["data"]["dag_nodes"] == 0
    assert result["data"]["nodes"] == []
    assert result["data"]["cooling"] is False


def test_effective_config_honors_zero_quota_and_day_share():
    plugin = _plugin({"proactive_quota_per_hour": 0, "rhythm_day_share_slots": 0})
    values = plugin.get_effective_config()
    assert values["proactive_quota_per_hour"] == 0
    assert values["rhythm_day_share_slots"] == 0


def test_overview_honors_zero_proactive_quota_and_day_share_slots():
    plugin = _plugin({"proactive_quota_per_hour": 0, "rhythm_day_share_slots": 0})
    data = snapshot_overview(plugin)
    assert data["useful_proactive"]["quota_per_hour"] == 0
    assert data["daily_rhythm"]["day_share_slots"] == 0
    assert data["read_air"]["proactive_cap"] == 0
    assert data["read_air"]["thermometer"]["proactive_cap"] == 0
