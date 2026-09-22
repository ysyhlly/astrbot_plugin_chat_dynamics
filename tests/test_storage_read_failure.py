"""Unreadable storage must not turn into a writable empty session."""

import errno
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core import data_paths
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore


@pytest.mark.parametrize("store_type", [GroupMemoryNotebook, MoodMemoryStore])
@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError("temporary IO")])
def test_read_failure_never_caches_defaults_or_overwrites(tmp_path, monkeypatch, store_type, failure):
    original = store_type(tmp_path)
    original.mute_tonight("room", now=100)
    path = original._path("room")
    before = path.read_bytes()
    store = store_type(tmp_path)
    real_read = Path.read_text
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", Mock(side_effect=failure))
        with pytest.raises(type(failure)):
            store._load("room")
        with pytest.raises(type(failure)):
            store.mute_tonight("room", now=200)
    assert not store._cache
    assert path.read_bytes() == before
    assert store._load("room")["mute_until"] == json.loads(real_read(path, encoding="utf-8"))["mute_until"]


@pytest.mark.parametrize("store_type", [GroupMemoryNotebook, MoodMemoryStore])
def test_utf8_bom_is_read_without_losing_existing_state(tmp_path, store_type):
    store = store_type(tmp_path)
    store._path("room").write_text(json.dumps({"umo": "room", "mute_until": 42}), encoding="utf-8-sig")
    assert store._load("room")["mute_until"] == 42


def test_bad_reminder_rows_do_not_trigger_or_block_valid_reminder(tmp_path):
    book = GroupMemoryNotebook(tmp_path)
    malformed = [None, "bad", {}, {"due_at": "bad"}, {"due_at": None},
                 {"due_at": True}, {"due_at": "NaN"}, {"due_at": "-Infinity"},
                 {"due_at": []}, {"due_at": 10 ** 400}]
    valid = {"id": "good", "text": "meeting", "due_at": 10}
    book._save("room", {"reminders": malformed + [valid]})
    assert [row["id"] for row in book.pop_due_reminders("room", now=20)] == ["good"]
    assert book.list_all("room")["reminders"][:-1] == malformed
    assert book.pop_due_reminders("room", now=20) == []


def test_unsupported_hardlinks_fail_without_switching_storage(tmp_path, monkeypatch):
    import astrbot.api.star as star

    legacy, target = tmp_path / "legacy", tmp_path / "target"
    legacy.mkdir()
    source = legacy / "notebook_old.json"
    source.write_text("preserved", encoding="utf-8")
    monkeypatch.setattr(star, "StarTools", SimpleNamespace(get_data_dir=lambda _: target), raising=False)
    monkeypatch.setattr(data_paths.os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "unsupported")))
    with pytest.raises(OSError):
        data_paths.resolve_data_root(legacy)
    assert source.read_text(encoding="utf-8") == "preserved"
    assert list(target.iterdir()) == []
