"""Memory consent races and durable notebook mutation regressions."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_chat_dynamics.core import group_memory as gm
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore


@pytest.mark.asyncio
async def test_cache_hook_never_loads_storage_and_evicted_policy_needs_refresh(tmp_path, monkeypatch):
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.remember("room", "peer", ["calm"])
    await store.refresh_async("room", "peer")
    monkeypatch.setattr(store, "_load", Mock(side_effect=AssertionError("request hook loaded storage")))
    assert store.cached_recall("room", "peer")
    assert store.recall_is_fresh("room", "peer")
    assert store.cached_recall("unknown", "peer") == []
    assert not store.recall_is_fresh("unknown", "peer")
    store._cache.clear()
    assert store.cached_recall("room", "peer") == []
    assert not store.recall_is_fresh("room", "peer")


def test_cache_hook_filters_existing_rows_using_memory_policy(tmp_path, monkeypatch):
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.forget("room", "peer", tag="coding")
    store._recall_cache[("room", "peer")] = (time.time(), [
        {"tag": "coding", "source": "local"},
        {"tag": "enjoys_coding", "source": "selflearning"},
        {"tag": "music", "source": "local"},
    ])
    monkeypatch.setattr(store, "_load", Mock(side_effect=AssertionError("request hook loaded storage")))
    assert store.cached_recall("room", "peer") == [{"tag": "music", "source": "local"}]


@pytest.mark.asyncio
async def test_group_forget_clears_warm_cache_without_touching_other_group(tmp_path):
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    for room in ("room", "other"):
        store.remember(room, "peer", ["calm"])
        assert await store.refresh_async(room, "peer")
    store.forget("room")
    assert store.cached_recall("room", "peer") == []
    assert not store.recall_is_fresh("room", "peer")
    assert store.cached_recall("other", "peer")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["tag", "peer", "group", "mute", "mute_clear"])
async def test_late_remote_recall_respects_policy_change(tmp_path, operation):
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(**kwargs):
        started.set()
        await release.wait()
        return [{"tag": "enjoys_coding"}]

    store = MoodMemoryStore(tmp_path, bridge=SimpleNamespace(fetch_approved_memories_async=fetch))
    store.configure(enabled=True)
    task = asyncio.create_task(store.refresh_async("room", "peer"))
    await started.wait()
    if operation == "tag":
        store.forget("room", "peer", tag="coding")
    elif operation == "peer":
        store.forget("room", "peer")
    elif operation == "group":
        store.forget("room")
    else:
        store.mute_tonight("room")
        if operation == "mute_clear":
            store.mute_tonight("room", hours=0)
    release.set()
    assert await task == []
    assert store.cached_recall("room", "peer") == []


@pytest.mark.asyncio
async def test_tag_forget_keeps_unrelated_local_notes(tmp_path):
    bridge = SimpleNamespace(fetch_approved_memories_async=AsyncMock(return_value=[{"tag": "enjoys_coding"}]))
    store = MoodMemoryStore(tmp_path, bridge=bridge)
    store.configure(enabled=True)
    store.remember("room", "peer", ["coding", "music"])
    store.forget("room", "peer", tag="coding")
    assert [r["tag"] for r in await store.refresh_async("room", "peer")] == ["music"]
    assert [r["tag"] for r in store.cached_recall("room", "peer")] == ["music"]
    bridge.fetch_approved_memories_async.assert_not_called()


@pytest.mark.parametrize("operation", ["add", "remove", "pop", "mute", "slang", "retract"])
def test_notebook_write_failure_preserves_cache_and_disk(tmp_path, monkeypatch, operation):
    book = gm.GroupMemoryNotebook(tmp_path)
    book.configure(slang_enabled=True)
    anniversary = book.add_anniversary("room", title="day", month=2, day=29)
    book.add_reminder("room", text="meeting", due_at=1)
    book.add_slang_trial("room", phrase="nice", approved=True)
    before = book.list_all("room")
    actions = {
        "add": lambda: book.add_anniversary("room", title="new", month=1, day=1),
        "remove": lambda: book.remove_anniversary("room", anniversary["id"]),
        "pop": lambda: book.pop_due_reminders("room", now=10),
        "mute": lambda: book.mute_tonight("room"),
        "slang": lambda: book.try_use_slang("room"),
        "retract": lambda: book.retract_cold_slang("room", "nice"),
    }
    monkeypatch.setattr(gm, "atomic_write_json", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        actions[operation]()
    assert book.list_all("room") == before
    assert gm.GroupMemoryNotebook(tmp_path).list_all("room") == before


def test_notebook_public_rows_do_not_mutate_committed_cache(tmp_path):
    book = gm.GroupMemoryNotebook(tmp_path)
    created = book.add_anniversary("room", title="day", month=1, day=1)
    created["title"] = "changed"
    listed = book.list_all("room")
    assert listed["anniversaries"][0]["title"] == "day"
    listed["anniversaries"][0]["title"] = "changed"
    assert book.list_all("room")["anniversaries"][0]["title"] == "day"


@pytest.mark.parametrize("month,day", [(2, 30), (2, 31), (4, 31), (6, 31), (9, 31), (11, 31)])
def test_anniversary_rejects_impossible_dates(tmp_path, month, day):
    book = gm.GroupMemoryNotebook(tmp_path)
    with pytest.raises(ValueError, match="invalid date"):
        book.add_anniversary("room", title="bad", month=month, day=day)
    assert book.list_all("room")["anniversaries"] == []
