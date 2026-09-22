"""Operator cooldowns and successful delivery survive failure boundaries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import astrbot_plugin_chat_dynamics.main as main_module
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.core import group_memory, web_api
from .test_plugin_lifecycle import _plugin, MockEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["fail", "cancel", "success", "supersede"])
async def test_legacy_commits_first_send_once_even_if_tail_cannot_finish(monkeypatch, tail):
    p = _plugin(clock=VirtualClock(initial_time=100))
    event = MockEvent("help", message_id="trigger")
    key = event.unified_msg_origin
    runtime = p._get_or_create_runtime(key, group_id=event.group_id, umo=key, bot_id="bot")
    node = runtime.dag.add_message(msg_id="trigger", user_id="user", text="help", timestamp=100)
    monkeypatch.setattr(p, "_run_native_reply", AsyncMock(return_value="answer"))
    monkeypatch.setattr(p.pacer, "shape_and_fragment", lambda *args, **kwargs: ["first", "tail"])
    monkeypatch.setattr(p.time_service, "sleep", AsyncMock())
    commit = Mock()
    monkeypatch.setattr(p, "_commit_pending_gate_spoke", commit)
    attempts = []

    async def send(*args, **kwargs):
        attempts.append(args[2])
        if len(attempts) == 1:
            if tail == "supersede":
                runtime.revision += 1
            return SendResult(True, "sent-first")
        # The first successful fragment must already be committed here.
        assert p.arbiter._last_bot_speak_time[key] == 100
        assert len(p.arbiter._bot_speak_history[key]) == 1
        commit.assert_called_once()
        if tail == "cancel":
            raise asyncio.CancelledError
        return SendResult(tail == "success", "sent-tail")

    monkeypatch.setattr(p, "_send_owned", send)
    try:
        coro = p._dispatch_bot_response(runtime, node, GroupChatMode.FAST_BANTER, event, runtime.revision)
        if tail == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await coro
        else:
            await coro
        assert p.arbiter._last_bot_speak_time[key] == 100
        assert len(p.arbiter._bot_speak_history[key]) == 1
        assert p.arbiter._last_bot_msg_id[key] == ("sent-tail" if tail == "success" else "sent-first")
        commit.assert_called_once_with(runtime, key)
        assert node.metadata["outcome"]["final_outcome"] == "delivered"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_legacy_failed_first_send_does_not_spend_quota(monkeypatch):
    p = _plugin()
    event = MockEvent("help", message_id="trigger")
    key = event.unified_msg_origin
    runtime = p._get_or_create_runtime(key, group_id=event.group_id, umo=key, bot_id="bot")
    node = runtime.dag.add_message(msg_id="trigger", user_id="user", text="help", timestamp=1)
    monkeypatch.setattr(p, "_run_native_reply", AsyncMock(return_value="answer"))
    monkeypatch.setattr(p.time_service, "sleep", AsyncMock())
    monkeypatch.setattr(p, "_send_owned", AsyncMock(return_value=SendResult(False)))
    commit = Mock()
    monkeypatch.setattr(p, "_commit_pending_gate_spoke", commit)
    try:
        await p._dispatch_bot_response(runtime, node, GroupChatMode.FAST_BANTER, event, runtime.revision)
        assert key not in p.arbiter._bot_speak_history
        commit.assert_not_called()
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("eviction", ["capacity", "idle"])
async def test_eviction_retains_cooling_until_expiry_or_explicit_reset(eviction):
    clock = VirtualClock(initial_time=100)
    p = _plugin(clock=clock)
    key = "mock:GroupMessage:cool"
    p._get_or_create_runtime(key, group_id="cool", umo=key, bot_id="bot")
    p.arbiter.trigger_cooling(key, duration_seconds=7200)
    try:
        if eviction == "capacity":
            p._registry.max_sessions = 1
            assert p._ensure_runtime_capacity("mock:GroupMessage:new")
        else:
            await clock.advance(3601)
            p._prune_idle_sessions(clock.time())
        assert key not in p._sessions
        assert p.arbiter.is_in_deep_cooling(key)
        await p._persist_cooling()
        assert key in p._kv[main_module._KV_COOLING]
        p._get_or_create_runtime(key, group_id="cool", umo=key, bot_id="bot")
        assert p.arbiter.is_in_deep_cooling(key)
        await p._reset_session_state_async(key)
        assert not p.arbiter.is_in_deep_cooling(key)
        p.arbiter.trigger_cooling(key, duration_seconds=1)
        p._drop_session(key)
        await clock.advance(2)
        p._prune_idle_sessions(clock.time())
        assert key not in p.arbiter.cooling_export()
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, OSError("offline")])
async def test_periodic_persistence_retries_cooling_without_new_changes(monkeypatch, failure):
    p = _plugin()
    attempts = []
    saved = asyncio.Event()

    async def write(key, payload):
        if key == main_module._KV_COOLING:
            attempts.append(payload)
            if len(attempts) == 1:
                if isinstance(failure, Exception):
                    raise failure
                return failure
            saved.set()
        p._kv[key] = payload

    monkeypatch.setattr(p, "put_kv_data", write)
    monkeypatch.setattr(main_module, "_PERSIST_INTERVAL_SECONDS", .01)
    p.arbiter.trigger_cooling("room", duration_seconds=600)
    await p._cooling_persist_task
    assert len(attempts) == 1
    assert p._cooling_persist_dirty
    p._runtime_persist_task = asyncio.create_task(p._panel_persistence_loop())
    try:
        await asyncio.wait_for(saved.wait(), 1)
        await p._cooling_persist_task
        assert len(attempts) == 2
        assert "room" in p._kv[main_module._KV_COOLING]
        assert not p._cooling_persist_dirty
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_permanent_cooling_storage_failure_does_not_hang_shutdown(monkeypatch):
    p = _plugin()
    monkeypatch.setattr(p, "put_kv_data", AsyncMock(return_value=False))
    p.arbiter.trigger_cooling("room", duration_seconds=600)
    await p._cooling_persist_task
    assert p._cooling_persist_dirty
    await asyncio.wait_for(p.terminate(), 1)
    assert p._cooling_persist_dirty


@pytest.mark.asyncio
async def test_notebook_api_write_failure_keeps_committed_state(monkeypatch, tmp_path, offline_web_responses):
    p = _plugin()
    p.group_memory = group_memory.GroupMemoryNotebook(tmp_path)
    api = web_api.ConsoleWebAPI(p)
    body = {"action": "add_anniversary", "umo": "room", "title": "Birthday", "month": 2, "day": 29}
    monkeypatch.setattr(web_api, "request", SimpleNamespace(headers={}))
    monkeypatch.setattr(web_api, "request_json", AsyncMock(return_value=body))
    monkeypatch.setattr(api, "_rate_limit", lambda *args: None)
    monkeypatch.setattr(group_memory, "atomic_write_json", Mock(side_effect=OSError("private disk detail")))
    try:
        response = await api.notebook_post()
        assert response["status_code"] == 503
        assert response["ok"] is False
        group_memory.atomic_write_json.assert_called_once()
        assert "private disk detail" not in str(response)
        assert p.group_memory.list_all("room")["anniversaries"] == []
    finally:
        await p.terminate()
