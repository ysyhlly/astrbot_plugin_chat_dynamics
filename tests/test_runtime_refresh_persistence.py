"""Config refresh and idle persistence are explicit, bounded state transitions."""
import asyncio
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics import main
from astrbot_plugin_chat_dynamics.core import runtime_persistence
from .test_plugin_lifecycle import _plugin


def test_scope_predicate_never_parses_or_applies_configuration(monkeypatch):
    plugin = _plugin({"takeover_all": False, "takeover_groups": ["one"]})
    parse = Mock(side_effect=AssertionError("predicate must be pure"))
    monkeypatch.setattr(main, "parse_runtime_config", parse)
    for _ in range(20):
        assert plugin.is_group_takeover_enabled("one")
        assert not plugin.is_group_takeover_enabled("two")
    parse.assert_not_called()


def test_changed_mutable_config_refreshes_once(monkeypatch):
    plugin = _plugin({"takeover_all": False, "takeover_groups": ["one"]})
    parse = Mock(wraps=main.parse_runtime_config)
    configure = Mock(wraps=plugin.embeddings.configure)
    monkeypatch.setattr(main, "parse_runtime_config", parse)
    monkeypatch.setattr(plugin.embeddings, "configure", configure)
    plugin.refresh_config()
    parse.assert_not_called()
    plugin.config["takeover_groups"].append("two")
    assert not plugin.is_group_takeover_enabled("two")
    plugin.refresh_config()
    assert plugin.is_group_takeover_enabled("two")
    plugin.refresh_config()
    assert parse.call_count == configure.call_count == 1


@pytest.mark.asyncio
async def test_idle_periodic_save_skips_export_and_storage(monkeypatch):
    plugin = _plugin()
    try:
        await plugin._save_panel_runtime()
        export = Mock(wraps=runtime_persistence.export_runtime_state)
        writer = Mock(wraps=plugin.put_kv_data)
        monkeypatch.setattr(runtime_persistence, "export_runtime_state", export)
        monkeypatch.setattr(plugin, "put_kv_data", writer)
        await plugin._save_panel_runtime(force=False)
        await plugin._save_panel_runtime(force=False)
        export.assert_not_called()
        writer.assert_not_called()
        plugin._metric("message_received")
        await plugin._save_panel_runtime(force=False)
        assert export.call_count == writer.call_count == 1
        await plugin._save_panel_runtime(force=False)
        assert writer.call_count == 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_mutation_during_write_remains_dirty():
    plugin = _plugin()
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []
    async def write(_key, payload):
        writes.append(payload)
        if len(writes) == 1:
            entered.set()
            await release.wait()
    plugin.put_kv_data = write
    saving = asyncio.create_task(plugin._save_panel_runtime(force=False))
    try:
        await entered.wait()
        plugin._metric("message_received")
        release.set()
        await saving
        assert plugin._runtime_persist_saved_revision != plugin._runtime_persist_revision
        await plugin._save_panel_runtime(force=False)
        assert len(writes) == 2
        assert writes[-1]["metrics"]["message_received"] == 1
        await plugin._save_panel_runtime(force=False)
        assert len(writes) == 2
    finally:
        release.set()
        await asyncio.gather(saving, return_exceptions=True)
        await plugin.terminate()


@pytest.mark.asyncio
async def test_failed_periodic_write_retries_without_losing_dirty_state():
    plugin = _plugin()
    writer = Mock(side_effect=[False, True])
    plugin.put_kv_data = writer
    await plugin._save_panel_runtime(force=False)
    assert plugin._runtime_persist_saved_revision != plugin._runtime_persist_revision
    await plugin._save_panel_runtime(force=False)
    assert writer.call_count == 2
    await plugin._save_panel_runtime(force=False)
    assert writer.call_count == 2
    plugin.put_kv_data = Mock(return_value=True)
    await plugin.terminate()
