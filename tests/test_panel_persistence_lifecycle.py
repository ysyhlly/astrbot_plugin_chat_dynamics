"""Panel storage survives a real plugin object replacement."""
import asyncio
import copy

import pytest

from .test_plugin_lifecycle import _plugin


@pytest.mark.asyncio
async def test_unload_reload_and_reset_preserve_storage_contract():
    first = _plugin()
    runtime = first._get_or_create_runtime("adapter:GroupMessage:42", group_id="42", bot_id="bot")
    runtime.dag.add_message("one", "user", "reload evidence", timestamp=first.time_service.time())
    runtime.touch(first.time_service.time())
    first._metric("message_received", 3)
    await first.terminate()
    stored = copy.deepcopy(first._kv)
    assert not first._sessions
    second = _plugin()
    second._kv = stored
    await second.initialize()
    assert second._sessions[runtime.session_key].dag.get_node("one").text == "reload evidence"
    assert second._metrics["message_received"] == 3
    await second._reset_session_state_async(runtime.session_key)
    third = _plugin()
    third._kv = copy.deepcopy(second._kv)
    await third.initialize()
    assert not third._sessions[runtime.session_key].dag.nodes
    await second.terminate()
    await third.terminate()


@pytest.mark.asyncio
async def test_storage_failure_does_not_prevent_shutdown():
    plugin = _plugin()

    async def fail(*args):
        raise OSError("disk unavailable")

    plugin.put_kv_data = fail
    await plugin.terminate()
    assert plugin._metrics["panel_persist_failed"] == 1
    assert not plugin._background_tasks



@pytest.mark.asyncio
async def test_overlapping_saves_cannot_overwrite_newer_state():
    plugin = _plugin()
    started, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def delayed_write(key, payload):
        if not writes:
            writes.append(payload)
            started.set()
            await release.wait()
        else:
            writes.append(payload)
        plugin._kv[key] = payload

    plugin.put_kv_data = delayed_write
    first = asyncio.create_task(plugin._save_panel_runtime())
    await started.wait()
    plugin._metric("message_received")
    second = asyncio.create_task(plugin._save_panel_runtime())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert plugin._kv["panel_runtime_v1"]["metrics"]["message_received"] == 1
    await plugin.terminate()
