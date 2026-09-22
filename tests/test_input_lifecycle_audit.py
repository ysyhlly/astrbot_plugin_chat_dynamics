"""Retained input identities and API parsing across audit failure boundaries."""
import gc
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.turn_decisions import _decision_of
from astrbot_plugin_chat_dynamics.core import web_api as web


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [None, "u"])
async def test_old_stop_generation_survives_new_active_buffer(member):
    clock = VirtualClock()
    buffer = DebounceBuffer(time_service=clock, base_cooldown=10)
    callback = AsyncMock()
    try:
        await buffer.discard("s", member)
        await clock.advance(7201)
        await buffer.ingest("s", "u", "new turn", object(), callback)
        buffer.prune_idle_slots(max_idle_seconds=3600)
        results = await buffer.flush("s", "u")
        callback.assert_awaited_once()
        assert len(results) == 1 and buffer.is_result_current(results[0])
    finally:
        await buffer.close(flush=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [None, "u"])
async def test_prune_cannot_resurrect_extracted_generation_zero_result(member):
    clock = VirtualClock()
    buffer = DebounceBuffer(time_service=clock, base_cooldown=10)
    try:
        await buffer.ingest("s", "u", "old turn", object(), AsyncMock())
        slot = buffer._slots[("s", "u")]
        async with slot.lock:
            result = buffer._extract_result_locked(slot)
        await buffer.discard("s", member)
        await clock.advance(7201)
        buffer.prune_idle_slots(max_idle_seconds=3600)
        assert not buffer.is_result_current(result)
        key = "s" if member is None else ("s", "u")
        assert key in buffer._generation_touched
        del result
        gc.collect()
        buffer.prune_idle_slots(max_idle_seconds=3600)
        assert key not in buffer._generation_touched
    finally:
        await buffer.close(flush=False)


def test_repeated_receipts_do_not_evict_still_retained_ids():
    runtime = SessionRuntime("s", "g", "s")
    runtime.sent_message_ids = deque(maxlen=2)
    for mid in ("a", "a", "b"):
        runtime.remember_sent(mid)
    assert list(runtime.sent_message_ids) == ["a", "b"]
    assert runtime.sent_id_set == {"a", "b"}
    runtime.remember_sent("c")
    assert runtime.sent_id_set == set(runtime.sent_message_ids) == {"b", "c"}


def test_choice_missing_selected_probability_is_unknown_not_zero():
    answer = {"type": "choice", "choice": "yes", "confidence": 1.0, "probabilities": {"no": 1.0}}
    assert _decision_of(answer, "choice") is None
    answer["probabilities"] = {"yes": .8, "no": .2}
    assert _decision_of(answer, "choice").normalized() == .8


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"content-length": "1"}])
async def test_large_raw_body_rejected_before_json_parse(monkeypatch, headers):
    request = SimpleNamespace(headers=headers, body=AsyncMock(return_value=b" " * (web._MAX_BODY_BYTES + 1)))
    monkeypatch.setattr(web, "request", request)
    parser = Mock(side_effect=AssertionError("must not parse oversized JSON"))
    monkeypatch.setattr(web.json, "loads", parser)
    assert await web._json_body() == {"__invalid_body__": "body too large"}
    parser.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", ["body", "get_data"])
async def test_small_chunked_body_parses_with_supported_raw_api(monkeypatch, loader):
    request = SimpleNamespace(headers={})
    setattr(request, loader, AsyncMock(return_value=b'{"refresh":false}'))
    monkeypatch.setattr(web, "request", request)
    assert await web._json_body() == {"refresh": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["annotation_draft", "annotation_drafts_post", "annotations_post", "ui_preferences_save"])
async def test_invalid_body_is_rejected_before_annotation_operations(monkeypatch, offline_web_responses, endpoint):
    plugin = SimpleNamespace(_shutting_down=False, annotation_draft_payload=AsyncMock(),
                             annotation_drafts_apply=AsyncMock(), put_kv_data=AsyncMock())
    api = web.ConsoleWebAPI(plugin)
    api.topic_annotations.save = AsyncMock()
    monkeypatch.setattr(api, "_rate_limit", lambda *args: None)
    monkeypatch.setattr(api, "_ui_preference_key", lambda: "prefs")
    monkeypatch.setattr(web, "_json_body", AsyncMock(return_value={"__invalid_body__": "body too large"}))
    response = await getattr(api, endpoint)()
    assert response["status_code"] == 400 and response["error"] == "body too large"
    plugin.annotation_draft_payload.assert_not_awaited()
    plugin.annotation_drafts_apply.assert_not_awaited()
    plugin.put_kv_data.assert_not_awaited()
    api.topic_annotations.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["refresh", "regenerate_dismissed"])
@pytest.mark.parametrize("value", ["false", 0, None])
async def test_draft_flags_require_actual_booleans(monkeypatch, offline_web_responses, flag, value):
    plugin = SimpleNamespace(_shutting_down=False, annotation_draft_payload=AsyncMock())
    api = web.ConsoleWebAPI(plugin)
    monkeypatch.setattr(api, "_rate_limit", lambda *args: None)
    monkeypatch.setattr(web, "_json_body", AsyncMock(return_value={"session_key": "s", flag: value}))
    assert (await api.annotation_draft())["status_code"] == 400
    plugin.annotation_draft_payload.assert_not_awaited()
