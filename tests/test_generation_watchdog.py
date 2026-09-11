"""Generation watchdogs release runtime ownership and permit the next request."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from .test_persona_model import model_plugin as model_plugin, flush, drain
from .test_plugin_lifecycle import At, MockEvent, _plugin


def request(message_id):
    return MockEvent("帮我回答这个问题", message_id=message_id, is_at_or_wake_command=True,
                     components=[At(qq="bot_42")])


@pytest.mark.asyncio
async def test_persona_timeout_releases_admission_and_next_request_replies(model_plugin):
    plugin, bridge = model_plugin
    plugin._runtime_config = replace(plugin._runtime_config, tool_agent_timeout=0.03)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hang():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bridge.before_reply = hang
    first = request("timeout-persona")
    try:
        await plugin.on_group_message(first)
        await flush(plugin, first)
        await asyncio.wait_for(entered.wait(), 1)
        runtime = plugin._sessions[first.unified_msg_origin]
        task = runtime.generation_task
        await asyncio.wait_for(asyncio.shield(task), 1)
        assert cancelled.is_set()
        assert runtime.generation_task is None
        assert runtime.active_model_turn is None
        assert runtime.session_key not in plugin._in_flight
        assert runtime.model_diagnostic["reason_code"] == "reply_timeout"
        assert not first.replies_sent
        assert not bridge.commits
        # Admission permits are all available again, including the one consumed
        # by the cancelled request (a non-locked semaphore alone cannot prove it).
        assert runtime.model_admission._value == 9
        bridge.before_reply = None
        plugin._runtime_config = replace(plugin._runtime_config, tool_agent_timeout=5)
        second = request("after-persona-timeout")
        await plugin.on_group_message(second)
        await flush(plugin, second)
        await drain(plugin)
        assert second.replies_sent
        assert bridge.commits == [bridge.response]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_legacy_generation_timeout_releases_owner_and_next_request_replies():
    plugin = _plugin({"pipeline_mode": "exclusive", "base_thinking_delay": 0,
                      "debounce_base_cooldown": 10, "daily_rhythm_enabled": False})
    plugin.llm.tool_agent_timeout = 0.03
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    plugin.context.tool_loop_agent = hang
    first = request("timeout-legacy")
    try:
        await plugin.on_group_message(first)
        await flush(plugin, first)
        await asyncio.wait_for(entered.wait(), 1)
        runtime = plugin._sessions[first.unified_msg_origin]
        await asyncio.wait_for(asyncio.shield(runtime.generation_task), 1)
        assert cancelled.is_set()
        assert runtime.generation_task is None
        assert runtime.session_key not in plugin._in_flight
        assert not first.replies_sent
        assert runtime.last_bot_node is None

        async def good(**kwargs):
            return SimpleNamespace(completion_text="这是答案。")

        plugin.context.tool_loop_agent = good
        plugin.llm.tool_agent_timeout = 5
        second = request("after-legacy-timeout")
        await plugin.on_group_message(second)
        await flush(plugin, second)
        await drain(plugin)
        assert second.replies_sent
        assert runtime.last_bot_node is not None
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", [False, True])
async def test_cancel_completes_while_state_lock_held_without_clearing_new_owner(model_plugin, persona):
    plugin, bridge = model_plugin
    if not persona:
        await plugin.terminate()
        plugin = _plugin({"pipeline_mode": "exclusive", "debounce_base_cooldown": 10,
                          "daily_rhythm_enabled": False})
    entered = asyncio.Event()

    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if persona:
        bridge.before_reply = hang
    else:
        plugin.context.tool_loop_agent = hang
    event = request("cancel-under-lock")
    replacement = None
    try:
        await plugin.on_group_message(event)
        await flush(plugin, event)
        await asyncio.wait_for(entered.wait(), 1)
        runtime = plugin._sessions[event.unified_msg_origin]
        old = runtime.generation_task
        replacement = asyncio.create_task(asyncio.Event().wait())
        async with runtime.state_lock:
            runtime.generation_task = replacement
            replacement_turn = object()
            runtime.active_model_turn = replacement_turn
            plugin._in_flight.add(runtime.session_key)
            old.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(old), 1)
            assert runtime.generation_task is replacement
            assert runtime.active_model_turn is replacement_turn
            assert runtime.session_key in plugin._in_flight
        assert not event.replies_sent
    finally:
        if replacement is not None:
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)
        await plugin.terminate()
