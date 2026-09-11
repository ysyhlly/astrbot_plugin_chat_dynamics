"""Persistent host directory migration and collision-resistant session files."""
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import data_paths
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore
from astrbot_plugin_chat_dynamics.core.persist import legacy_safe_umo, safe_umo


@pytest.mark.parametrize('first,second', [
    ('platform:group:1', 'platform_group_1'),
    ('room', 'ROOM'),
    ('room', ':room'),
    ('x' * 140 + '1', 'x' * 140 + '2'),
])
def test_filename_and_cache_separate_colliding_sessions(tmp_path, first, second):
    assert safe_umo(first).casefold() != safe_umo(second).casefold()
    assert len(safe_umo(first)) <= 97
    notebook = GroupMemoryNotebook(tmp_path)
    notebook.add_reminder(first, text='private reminder', due_at=10)
    assert notebook.list_all(second)['reminders'] == []
    assert GroupMemoryNotebook(tmp_path).list_all(second)['reminders'] == []
    assert GroupMemoryNotebook(tmp_path).list_all(first)['reminders'][0]['text'] == 'private reminder'
    mood = MoodMemoryStore(tmp_path)
    mood.configure(enabled=True)
    mood.remember(first, 'peer', ['coding'], now=1)
    assert mood.recall(second, 'peer', now=1) == []
    reloaded = MoodMemoryStore(tmp_path)
    reloaded.configure(enabled=True)
    assert reloaded.recall(first, 'peer', now=1)[0]['tag'] == 'coding'
    assert reloaded.recall(second, 'peer', now=1) == []
    assert json.loads(mood._path(first).read_text(encoding='utf-8'))['umo'] == first


@pytest.mark.parametrize('owner', [None, 'other', 'room'])
@pytest.mark.parametrize('store_type,prefix', [(MoodMemoryStore, 'mood'), (GroupMemoryNotebook, 'notebook')])
def test_legacy_file_requires_verifiable_owner(tmp_path, owner, store_type, prefix):
    legacy = tmp_path / f'{prefix}_{legacy_safe_umo("room")}.json'
    data = {'mute_until': 123}
    if owner is not None:
        data['umo'] = owner
    legacy.write_text(json.dumps(data), encoding='utf-8')
    store = store_type(tmp_path)
    assert store._load('room')['mute_until'] == (123 if owner == 'room' else 0)
    store.mute_tonight('room', now=200)
    assert legacy.read_text(encoding='utf-8') == json.dumps(data)
    assert json.loads(store._path('room').read_text(encoding='utf-8'))['umo'] == 'room'


def test_wrong_owner_in_new_filename_is_never_loaded(tmp_path):
    store = GroupMemoryNotebook(tmp_path)
    store._path('room').write_text(json.dumps({'umo': 'other', 'mute_until': 999}), encoding='utf-8')
    assert store.list_all('room')['mute_until'] == 0


def test_host_directory_migration_keeps_source_and_existing_target(tmp_path, monkeypatch):
    import astrbot.api.star as star
    legacy, target = tmp_path / 'plugin' / 'data', tmp_path / 'host' / 'plugin_data'
    legacy.mkdir(parents=True)
    target.mkdir(parents=True)
    (legacy / 'mood_old.json').write_text('old mood', encoding='utf-8')
    (legacy / 'notebook_old.json').write_text('old notebook', encoding='utf-8')
    (legacy / 'unrelated.json').write_text('private', encoding='utf-8')
    (target / 'mood_old.json').write_text('new mood', encoding='utf-8')
    names = []

    def get_data_dir(name):
        names.append(name)
        return target

    monkeypatch.setattr(star, 'StarTools', SimpleNamespace(get_data_dir=get_data_dir), raising=False)
    assert data_paths.resolve_data_root(legacy) == target.resolve()
    assert names == ['astrbot_plugin_chat_dynamics']
    assert (target / 'mood_old.json').read_text() == 'new mood'
    assert (target / 'notebook_old.json').read_text() == 'old notebook'
    assert (legacy / 'notebook_old.json').read_text() == 'old notebook'
    assert not (target / 'unrelated.json').exists()
    assert {p.name for p in target.iterdir()} == {'mood_old.json', 'notebook_old.json'}
    data_paths.resolve_data_root(legacy)
    assert (target / 'mood_old.json').read_text() == 'new mood'


def test_migration_atomic_publish_does_not_clobber_racing_writer(tmp_path, monkeypatch):
    source, target = tmp_path / 'source.json', tmp_path / 'target.json'
    source.write_text('legacy')
    original = data_paths.os.link

    def racing_link(temporary, destination):
        destination.write_text('live writer')
        return original(temporary, destination)

    monkeypatch.setattr(data_paths.os, 'link', racing_link)
    data_paths._copy_missing(source, target)
    assert target.read_text() == 'live writer'
    assert source.read_text() == 'legacy'
    assert {p.name for p in tmp_path.iterdir()} == {'source.json', 'target.json'}


def test_host_directory_error_does_not_silently_fall_back(tmp_path, monkeypatch):
    import astrbot.api.star as star

    def denied(_name):
        raise RuntimeError('permission denied')

    monkeypatch.setattr(star, 'StarTools', SimpleNamespace(get_data_dir=denied), raising=False)
    with pytest.raises(RuntimeError, match='permission denied'):
        data_paths.resolve_data_root(tmp_path / 'legacy')
