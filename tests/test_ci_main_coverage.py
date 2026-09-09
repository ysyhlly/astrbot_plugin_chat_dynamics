"""Behavior contracts for the console configuration and notebook adapters."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore
from .test_plugin_lifecycle import MockContext


@pytest.fixture
def plugin(tmp_path):
    instance = ChatDynamicsPlugin(MockContext(), {"decision_mode": "legacy"})
    instance.group_memory = GroupMemoryNotebook(tmp_path)
    instance.group_memory.configure(slang_enabled=True)
    instance.mood_memory = MoodMemoryStore(tmp_path)
    return instance


@pytest.mark.parametrize("kind,value,expected", [
    ("bool", True, True), ("bool", 0, False), ("bool", "YES", True),
    ("bool", " false ", False), ("int", "3.0", 3), ("int", 4, 4),
    ("float", "1.25", 1.25), ("list", None, []), ("list", ["a"], ["a"]),
    ("list", "a, b\nc", ["a", "b", "c"]), ("string", None, ""),
    ("string", 42, "42"), ("string", "a", "a"),
])
def test_config_input_normalization(plugin, kind, value, expected):
    assert plugin._normalize_config_update_value("field", value, {"type": kind}) == expected


@pytest.mark.parametrize("kind,value", [
    ("bool", "maybe"), ("bool", 2), ("int", True), ("int", None),
    ("int", " "), ("int", "no"), ("int", 1.2), ("int", "inf"),
    ("float", False), ("float", None), ("float", " "),
    ("float", "no"), ("float", "nan"), ("list", 1),
])
def test_config_invalid_inputs_are_rejected(plugin, kind, value):
    with pytest.raises(ValueError, match="field must be"):
        plugin._normalize_config_update_value("field", value, {"type": kind})


@pytest.mark.asyncio
async def test_config_save_and_apply_refresh_live_runtime(plugin):
    plugin.save_config = AsyncMock(return_value=True)
    panel = await plugin.save_config_values({"chars_per_second": "42", "bot_names": "Alice,Bob"})
    assert panel["stored"]["chars_per_second"] == 42
    assert panel["effective"]["chars_per_second"] == 42
    assert plugin.pacer.chars_per_second == 42
    assert plugin.addressivity_router.bot_names == {"Alice", "Bob"}
    plugin.save_config.assert_awaited_once()
    plugin.config["chars_per_second"] = 50
    panel = await plugin.apply_stored_config()
    assert panel["effective"]["chars_per_second"] == 50
    assert plugin._metrics["config_saved"] == 1
    assert plugin._metrics["config_applied"] == 1
    assert "enable" in panel["mismatches"]


@pytest.mark.asyncio
async def test_config_validation_happens_before_mutation(plugin):
    before = dict(plugin.config)
    for updates, match in [(None, "object"), ({"not_a_field": 1}, "unknown fields"),
                           ({"enable": False, "chars_per_second": "bad"}, "number")]:
        with pytest.raises(ValueError, match=match):
            await plugin.save_config_values(updates)
        assert plugin.config == before
    plugin.save_config = Mock(return_value=False)
    with pytest.raises(RuntimeError, match="returned false"):
        await plugin.save_config_values({"enable": False})
    assert plugin._metrics.get("config_saved", 0) == 0


@pytest.mark.asyncio
async def test_config_write_failure_is_reported(plugin):
    class ReadOnly(dict):
        def __setitem__(self, key, value):
            raise TypeError("read only")
    plugin.config = ReadOnly(plugin.config)
    with pytest.raises(RuntimeError, match="failed to set enable: TypeError"):
        await plugin.save_config_values({"enable": False})


def test_config_schema_and_stored_value_fallbacks(plugin, monkeypatch):
    class Config(dict):
        schema = {"enable": {"type": "bool"}}
        def get(self, *args):
            raise RuntimeError("unavailable")
    plugin.config = Config(enable=True)
    assert plugin._config_schema() == Config.schema
    assert plugin._config_stored_values() == {"enable": None}
    plugin.config = {}
    monkeypatch.setattr("pathlib.Path.read_text", Mock(side_effect=OSError))
    assert plugin._config_schema() == {}
    plugin._runtime_config = None
    assert plugin.get_effective_config()["enable"] is True


def test_provider_catalog_preserves_metadata_and_orders_ids(plugin):
    rich = SimpleNamespace(meta=lambda: SimpleNamespace(id="Z", model="gpt", type="chat", provider_type="llm"))
    fallback = SimpleNamespace(meta=Mock(side_effect=ValueError), provider_config={"id": "a", "model": "local", "type": "custom", "provider_type": "llm"})
    embedding = SimpleNamespace(provider_config={"id": "embed"}, model_name="vector")
    plugin.context = SimpleNamespace(get_all_providers=lambda: [rich, fallback, object()],
                                     get_all_embedding_providers=lambda: [embedding])
    catalog = plugin.list_available_providers()
    assert [p["id"] for p in catalog["chat"]] == ["a", "Z"]
    assert catalog["chat"][1]["label"] == "Z · gpt · chat"
    assert catalog["embedding"][0]["model"] == "vector"
    assert plugin._serialize_provider(SimpleNamespace(provider_config="invalid"))["label"] == "(unnamed)"
    plugin.context = SimpleNamespace(get_all_providers=Mock(side_effect=RuntimeError),
                                     get_all_embedding_providers=Mock(side_effect=RuntimeError))
    assert plugin.list_available_providers() == {"chat": [], "embedding": []}
    plugin.context = None
    assert plugin.list_available_providers() == {"chat": [], "embedding": []}


@pytest.mark.asyncio
async def test_notebook_crud_is_session_isolated(plugin):
    umo = "platform:GroupMessage:one"
    def mutate(action, **fields):
        return plugin.notebook_mutate(action, {"umo": umo, **fields})
    anniversary = mutate("add_anniversary", title="Birthday", month=5, day=6, note="cake")["item"]
    reminder = mutate("add_reminder", text="meeting", due_at=9999999999, created_by="owner")["item"]
    slang = mutate("add_slang", phrase="great", approved=True)["item"]
    assert len(mutate("list")["anniversaries"]) == 1
    assert plugin.notebook_list("platform:GroupMessage:two")["anniversaries"] == []
    assert mutate("due_reminders") == {"items": []}
    assert isinstance(mutate("due_anniversaries")["items"], list)
    assert mutate("remove_anniversary", id=anniversary["id"])["removed"]
    assert mutate("remove_reminder", id=reminder["id"])["removed"]
    assert mutate("remove_slang", id=slang["id"])["removed"]
    for action in ("mark_done", "done_reminder"):
        item = mutate("add_reminder", text="todo", due_at=9999999999)["item"]
        assert mutate(action, id=item["id"])["removed"]
    assert mutate("mute_tonight", hours=1)["mute_until"] > 0
    assert mutate("forget", peer_id="nobody", tag="test") == {"forgotten": True}
    assert (await plugin.notebook_mutate_async("list", {"session_key": umo}))["reminders"] == []
    item = await plugin.notebook_mutate_async("add_slang", {"umo": umo, "phrase": "nice", "approved": True})
    assert item["item"]["phrase"] == "nice"


@pytest.mark.asyncio
async def test_notebook_errors_are_explicit(plugin):
    with pytest.raises(ValueError, match="umo required"):
        await plugin.notebook_mutate_async("add_slang", {})
    with pytest.raises(ValueError, match="umo required"):
        plugin.notebook_mutate("list", {})
    with pytest.raises(ValueError, match="due_at required"):
        plugin.notebook_mutate("add_reminder", {"umo": "one"})
    with pytest.raises(ValueError, match="unknown notebook action"):
        plugin.notebook_mutate("unknown", {"umo": "one"})
    plugin.group_memory = None
    assert plugin.notebook_list("one")["reminders"] == []
    with pytest.raises(RuntimeError, match="group memory unavailable"):
        plugin.notebook_mutate("list", {"umo": "one"})


@pytest.mark.asyncio
async def test_preset_failed_persistence_restores_previous_and_absent_keys(plugin):
    plugin.config["shadow_mode"] = True
    before = dict(plugin.config)
    plugin.save_config = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="returned false"):
        await plugin.apply_preset("balanced")
    assert plugin.config == before
    assert plugin.shadow_mode is True
    assert plugin._metrics["preset_apply_failed"] == 1
    plugin.save_config = AsyncMock(return_value=True)
    result = await plugin.apply_preset("balanced")
    assert result["saved"] is True
    assert result["changed"]
    assert plugin._metrics["preset_applied"] == 1


@pytest.mark.asyncio
async def test_cooling_persistence_failure_clears_dirty_state(plugin):
    plugin.put_kv_data = AsyncMock(side_effect=OSError("disk unavailable"))
    plugin._cooling_persist_dirty = True
    plugin._cooling_persist_event.set()
    await plugin._persist_cooling()
    assert plugin._metrics["cooling_persist_failed"] == 1
    assert not plugin._cooling_persist_dirty
    assert not plugin._cooling_persist_event.is_set()
    plugin.put_kv_data = None
    plugin._cooling_persist_dirty = True
    plugin._cooling_persist_event.set()
    await plugin._persist_cooling()
    assert not plugin._cooling_persist_dirty
    assert not plugin._cooling_persist_event.is_set()


def test_delayed_owned_send_markers_are_consumed_once(plugin):
    event = SimpleNamespace()
    marker = ("one", id(event))
    event._chat_dynamics_owned_send_marker = [marker, marker]
    assert not plugin._is_owned_send(event, "two")
    assert plugin._is_owned_send(event, "one")
    assert plugin._is_owned_send(event, "one")
    assert not plugin._is_owned_send(event, "one")
    assert not hasattr(event, "_chat_dynamics_owned_send_marker")
