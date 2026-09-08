from __future__ import annotations

import asyncio

import pytest

import astrbot_plugin_chat_dynamics.core.web_api as web_api
import astrbot_plugin_chat_dynamics.core.web_compat as web_compat
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter
from astrbot_plugin_chat_dynamics.core.dashboard import snapshot_session_or_none
from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer
from astrbot_plugin_chat_dynamics.core.platform_bridge import parse_group_event
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import At, MockEvent, _plugin


class _Response:
    completion_text = "ok"


@pytest.mark.asyncio
async def test_web_compat_accepts_legacy_json_method_without_default_keyword(monkeypatch):
    class LegacyRequest:
        def json(self):
            return {"name": "active"}

    monkeypatch.setattr(web_compat, "request", LegacyRequest())
    assert await web_compat.request_json({}) == {"name": "active"}


@pytest.mark.asyncio
async def test_provider_resolution_matrix_prefers_dedicated_then_legacy_then_umo():
    calls: list[dict] = []

    class Context:
        async def get_current_chat_provider_id(self, umo):
            calls.append({"lookup": umo})
            return "umo-provider"

        async def llm_generate(self, **kwargs):
            calls.append(kwargs)
            return _Response()

    adapter = LLMAdapter(
        Context(),
        configured_provider_id="legacy-provider",
        reply_provider_id="reply-provider",
        vibe_provider_id="vibe-provider",
    )
    await adapter.generate(prompt="r", umo="room", system_prompt="", purpose="reply")
    await adapter.generate(prompt="v", umo="room", system_prompt="", purpose="vibe")
    assert calls[0]["chat_provider_id"] == "reply-provider"
    assert calls[1]["chat_provider_id"] == "vibe-provider"

    adapter.configure("legacy-provider")
    await adapter.generate(prompt="r", umo="room", system_prompt="", purpose="reply")
    await adapter.generate(prompt="v", umo="room", system_prompt="", purpose="vibe")
    assert calls[-2]["chat_provider_id"] == "legacy-provider"
    assert calls[-1]["chat_provider_id"] == "legacy-provider"

    adapter.configure("")
    await adapter.generate(prompt="r", umo="room", system_prompt="", purpose="reply")
    assert calls[-2] == {"lookup": "room"}
    assert calls[-1]["chat_provider_id"] == "umo-provider"


def test_legacy_provider_warning_and_effective_resolution_are_exposed():
    plugin = _plugin({"provider": "legacy", "reply_provider": "reply", "vibe_provider": "vibe"})
    from astrbot_plugin_chat_dynamics.core.dashboard import snapshot_overview

    overview = snapshot_overview(plugin)
    assert any("legacy provider" in warning for warning in overview["config_warnings"])
    assert overview["provider_resolution"] == {"reply": "reply", "vibe": "vibe", "decision": "reply"}


def test_unknown_component_is_conservatively_treated_as_media():
    class UnknownComponent:
        pass

    event = MockEvent("", components=[UnknownComponent()])
    parsed = parse_group_event(event)
    assert parsed.has_media is True
    assert parsed.media_component_types == ["UnknownComponent"]


@pytest.mark.asyncio
async def test_media_modes_and_mixed_turn_behavior():
    filter_plugin = _plugin({"pipeline_mode": "filter"})
    pure = MockEvent("", group_id="media-filter", message_id="pure", components=[type("Image", (), {})()])
    await filter_plugin.on_group_message(pure)
    assert pure.call_llm is True
    assert pure.is_stopped is False
    assert filter_plugin.debounce.get_pending_count(pure.unified_msg_origin) == 0
    placeholder = MockEvent("[图片]", group_id="media-placeholder", message_id="placeholder", components=[type("Image", (), {})()])
    await filter_plugin.on_group_message(placeholder)
    assert placeholder.call_llm is True
    assert filter_plugin.debounce.get_pending_count(placeholder.unified_msg_origin) == 0

    exclusive = _plugin({"pipeline_mode": "exclusive"})
    pure_exclusive = MockEvent("", group_id="media-exclusive", message_id="pure", components=[type("Video", (), {})()])
    await exclusive.on_group_message(pure_exclusive)
    assert pure_exclusive.is_stopped is True

    shadow = _plugin({"pipeline_mode": "exclusive", "shadow_mode": True})
    pure_shadow = MockEvent("", group_id="media-shadow", message_id="pure", components=[type("File", (), {})()])
    await shadow.on_group_message(pure_shadow)
    assert pure_shadow.is_stopped is False
    assert pure_shadow.call_llm is False

    clock = VirtualClock(initial_time=10.0)
    mixed = _plugin({"debounce_base_cooldown": 1.0}, clock=clock)
    text_media = MockEvent("小助手看图", group_id="media-mixed", message_id="mixed", components=[type("Image", (), {})()])
    text_media.is_at_or_wake_command = True
    await mixed.on_group_message(text_media)
    assert text_media.call_llm is False
    assert mixed.debounce.get_pending_count(text_media.unified_msg_origin) == 0

    addressed_pure = MockEvent(
        "",
        group_id="media-at-image",
        message_id="at-img",
        components=[At(qq="bot_42"), type("Image", (), {})()],
        is_at_or_wake_command=True,
    )
    await filter_plugin.on_group_message(addressed_pure)
    assert addressed_pure.call_llm is False
    assert addressed_pure.is_stopped is False
    await clock.advance(2.0)
    await asyncio.sleep(0)
    node = mixed.dags[text_media.unified_msg_origin].get_node("mixed")
    assert node is not None
    metrics = mixed.vibe_analyzer.get_telemetrics(text_media.unified_msg_origin, current_time=clock.time())
    assert metrics.media_ratio > 0


def test_console_redacts_message_content_by_default():
    plugin = _plugin()
    key = "mock:GroupMessage:private"
    runtime = plugin._get_or_create_runtime(key, group_id="private", umo=key, bot_id="bot")
    runtime.dag.add_message("m1", "user-1234", "secret message", timestamp=1.0)
    detail = snapshot_session_or_none(plugin, key)
    assert detail is not None
    assert detail["content_redacted"] is True
    assert "nodes" not in detail
    assert detail["last_bot_text"] == ""


@pytest.mark.asyncio
async def test_web_api_presets_validate_confirmation_and_unknown_fields(monkeypatch, offline_web_responses):
    plugin = _plugin()
    catalog = await plugin._web.presets()
    assert catalog["ok"] is True
    assert set(catalog["data"]["presets"]) == {"observe", "balanced", "active"}

    async def unknown_body():
        return {"name": "active", "confirm": True, "unexpected": 1}

    monkeypatch.setattr(web_api, "_json_body", unknown_body)
    rejected = await plugin._web.apply_preset()
    assert rejected["status_code"] == 400

    async def valid_body():
        return {"name": "observe", "confirm": True}

    monkeypatch.setattr(web_api, "_json_body", valid_body)
    applied = await plugin._web.apply_preset()
    assert applied["ok"] is True
    assert applied["data"]["name"] == "observe"

    async def no_confirm():
        return {"name": "active", "confirm": False}

    monkeypatch.setattr(web_api, "_json_body", no_confirm)
    rejected = await plugin._web.apply_preset()
    assert rejected["status_code"] == 400


@pytest.mark.asyncio
async def test_web_api_rate_limit_uses_retry_after_header(monkeypatch):
    plugin = _plugin()

    def legacy_error(message, *, status_code=400, data=None):
        return {"status": "error", "ok": False, "error": message, "data": data, "status_code": status_code}

    monkeypatch.setattr(web_api, "error_response", legacy_error)

    async def body():
        return {"session_key": "missing"}

    monkeypatch.setattr(web_api, "_json_body", body)
    results = [await plugin._web.reset() for _ in range(21)]
    assert results[-1]["status_code"] == 429
    assert results[-1]["headers"]["Retry-After"] == "60"
    assert plugin._metrics.get("rate_limited") == 1


@pytest.mark.asyncio
async def test_web_api_rejects_non_scalar_identifier_and_boolean_minutes(monkeypatch, offline_web_responses):
    plugin = _plugin()

    async def bad_identifier():
        return {"session_key": ["room"]}

    monkeypatch.setattr(web_api, "_json_body", bad_identifier)
    response = await plugin._web.cool()
    assert response["status_code"] == 400

    key = "mock:GroupMessage:web-validation"
    plugin._get_or_create_runtime(key, group_id="web-validation", umo=key, bot_id="bot")

    async def bad_minutes():
        return {"session_key": key, "minutes": True}

    monkeypatch.setattr(web_api, "_json_body", bad_minutes)
    response = await plugin._web.cool()
    assert response["status_code"] == 400


@pytest.mark.asyncio
async def test_invalid_vibe_label_enters_short_failure_backoff():
    plugin = _plugin({"vibe_llm_enabled": True})
    key = "mock:GroupMessage:vibe-backoff"
    plugin._get_or_create_runtime(key, group_id="vibe-backoff", umo=key, bot_id="bot")
    plugin._vibe_msg_counts[key] = 12
    plugin._classify_vibe_with_llm = lambda *_args, **_kwargs: asyncio.sleep(0, result=None)
    plugin._schedule_vibe_llm(key, "turn", now=10.0)
    task = plugin._vibe_llm_tasks_by_session[key]
    await task
    assert plugin._vibe_llm_backoff_until[key] > 10.0
    plugin._schedule_vibe_llm(key, "turn2", now=11.0)
    assert plugin._vibe_llm_tasks_by_session.get(key) is None


@pytest.mark.asyncio
async def test_debounce_turn_is_bounded_to_recent_fragments_and_text_tail():
    clock = VirtualClock(initial_time=1.0)
    buffer = DebounceBuffer(time_service=clock, base_cooldown=30.0, max_cap=60.0, max_fragments=3, max_turn_chars=256)
    flushed = []

    async def callback(result):
        flushed.append(result)

    for index in range(4):
        await buffer.ingest("room", "user", f"fragment-{index}-" + ("abcdefgh" * 20), object(), callback)
        await clock.advance(0.1)
    results = await buffer.flush("room")
    assert results and len(results[0].messages) == 3
    assert len(results[0].consolidated_text) <= 256
    assert "内容已截断" in results[0].consolidated_text
    assert flushed
