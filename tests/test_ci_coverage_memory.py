"""Persistence, one-shot reminders and consent boundaries of memory stores."""
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock
import pytest
from astrbot_plugin_chat_dynamics.core import group_memory as gm, mood_memory as mm, persist


def test_notebook_persistence_and_removal(tmp_path):
    book = gm.GroupMemoryNotebook(tmp_path)
    anniversary = book.add_anniversary("room", title="birthday", month=9, day=9)
    reminder = book.add_reminder("room", text="meeting", due_at=10)
    loaded = gm.GroupMemoryNotebook(tmp_path)
    assert loaded.list_all("room")["anniversaries"] == [anniversary]
    assert loaded.list_all("other")["reminders"] == []
    assert loaded.remove_anniversary("room", anniversary["id"])
    assert not loaded.remove_anniversary("room", "absent")
    assert loaded.remove_reminder("room", reminder["id"])
    assert not loaded.remove_reminder("room", "absent")
    assert gm.GroupMemoryNotebook(tmp_path).list_all("room")["reminders"] == []


def test_notebook_invalid_storage_and_write_failure_keep_local_state(tmp_path, monkeypatch):
    book = gm.GroupMemoryNotebook(tmp_path)
    book._path("room").write_text("broken", encoding="utf-8")
    assert book.list_all("room")["reminders"] == []
    monkeypatch.setattr(gm, "atomic_write_json", Mock(side_effect=OSError))
    book.add_reminder("room", text="meeting", due_at=10)
    assert len(book.list_all("room")["reminders"]) == 1


def test_notebook_disabled_validation_and_mute(tmp_path):
    book = gm.GroupMemoryNotebook(tmp_path)
    for title, month, day in [("", 1, 1), ("bad", 13, 1)]:
        with pytest.raises(ValueError):
            book.add_anniversary("room", title=title, month=month, day=day)
    with pytest.raises(ValueError):
        book.add_reminder("room", text="", due_at=10)
    today = time.localtime(100)
    book.add_anniversary("room", title="today", month=today.tm_mon, day=today.tm_mday)
    assert len(book.due_anniversaries("room", now=100)) == 1
    book.add_reminder("room", text="meeting", due_at=10)
    book.mute_tonight("room", now=100)
    assert book.due_anniversaries("room", now=101) == []
    assert book.pop_due_reminders("room", now=101) == []
    book.configure(enabled=False)
    assert book.due_anniversaries("room") == book.pop_due_reminders("room") == []
    for call in (lambda: book.add_reminder("room", text="a", due_at=1),
                 lambda: book.add_anniversary("room", title="a", month=1, day=1)):
        with pytest.raises(RuntimeError):
            call()


@pytest.mark.asyncio
async def test_slang_bridge_approval_and_one_use(tmp_path):
    bridge = NS(fetch_slang_candidates=Mock(return_value=[{"text": "nice"}]),
                fetch_slang_candidates_async=AsyncMock(return_value=[{"tag": "great"}]))
    book = gm.GroupMemoryNotebook(tmp_path)
    with pytest.raises(RuntimeError):
        await book.add_slang_trial_async("room", phrase="nice")
    book.configure(slang_enabled=True, bridge=bridge)
    item = book.add_slang_trial("room", phrase="nice")
    second = await book.add_slang_trial_async("room", phrase="great")
    assert book.try_use_slang("room") == "nice"
    book.retract_cold_slang("room", "great")
    assert book.try_use_slang("room") is None
    assert book.remove_slang("room", item["id"])
    assert book.remove_slang("room", second["id"])
    assert not book.remove_slang("room", "absent")
    with pytest.raises(ValueError, match="unapproved"):
        await book.add_slang_trial_async("room", phrase="unknown")
    bridge.fetch_slang_candidates.side_effect = RuntimeError
    with pytest.raises(ValueError, match="unapproved"):
        book.add_slang_trial("room", phrase="unknown")
    book.mute_tonight("room", now=100)
    assert book.try_use_slang("room", now=101) is None


def test_mood_reload_retention_and_corrupt_file(tmp_path, monkeypatch):
    store = mm.MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.remember("room", "peer", ["coding"], now=100)
    loaded = mm.MoodMemoryStore(tmp_path)
    loaded.configure(enabled=True)
    assert loaded.recall("room", "peer", now=100)[0]["tag"] == "coding"
    loaded._path("corrupt").write_text("broken", encoding="utf-8")
    assert loaded.recall("corrupt", "peer") == []
    data = {"peers": {str(i): {"last_seen": i} for i in range(201)}}
    loaded._save("many", data)
    assert len(data["peers"]) == 200 and "0" not in data["peers"]
    monkeypatch.setattr(mm, "atomic_write_json", Mock(side_effect=OSError))
    loaded.remember("room", "peer", ["testing"], now=100)
    assert {r["tag"] for r in loaded.recall("room", "peer", now=100)} == {"coding", "testing"}


def test_mood_remote_sanitization_and_local_fallback(tmp_path):
    bridge = NS(fetch_approved_memories=Mock(return_value=[
        {"tag": "conflict"}, {"tag": "coding", "weight": "bad"},
        {"tag": "coding", "weight": float("nan")}, {"label": "music", "weight": 2}]))
    store = mm.MoodMemoryStore(tmp_path)
    store.configure(enabled=True, bridge=bridge)
    store.remember("room", "peer", ["coding"], now=100)
    assert store.recall("room", "peer", now=100) == [{"tag": "music", "weight": 1, "source": "selflearning"}]
    bridge.fetch_approved_memories.side_effect = RuntimeError
    assert store.recall("room", "peer", now=100)[0]["source"] == "local"
    store.forget("room", "peer", tag="coding")
    store.remember("room", "peer", ["coding"], now=200)
    assert store.recall("room", "peer", now=200) == []
    store.forget("room")
    assert not store.remote_context_allowed("room", "another")


@pytest.mark.asyncio
async def test_async_mood_disabled_and_mute_do_not_fetch(tmp_path):
    bridge = NS(fetch_approved_memories_async=AsyncMock())
    store = mm.MoodMemoryStore(tmp_path, bridge=bridge)
    assert await store.recall_async("room", "peer") == []
    store.configure(enabled=True)
    store.mute_tonight("room", now=100)
    assert await store.recall_async("room", "peer", now=101) == []
    bridge.fetch_approved_memories_async.assert_not_called()


def test_atomic_replace_failure_preserves_original_and_removes_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    persist.atomic_write_json(target, {"old": True})
    monkeypatch.setattr(persist.os, "replace", Mock(side_effect=OSError("disk unavailable")))
    with pytest.raises(OSError):
        persist.atomic_write_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}
    assert not target.with_name("state.json.tmp").exists()
