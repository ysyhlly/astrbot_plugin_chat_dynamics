"""Durable reminder delivery, media fallback and hidden-label updates."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import flush, drain
from .test_plugin_lifecycle import At, MockEvent
from .test_topic_annotations import fixture_plugin, label
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook
from astrbot_plugin_chat_dynamics.core.reminder_delivery import ReminderDelivery
from astrbot_plugin_chat_dynamics.core import deferred_media, group_memory, web_api
from astrbot_plugin_chat_dynamics.core.platform_bridge import chain_plain_text
from astrbot_plugin_chat_dynamics.core.media_archive import MediaArchive
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.core.message_semantics import describe_message
from astrbot_plugin_chat_dynamics.core.runtime_persistence import export_runtime_state, restore_runtime_state

@pytest.mark.asyncio
async def test_media_copy_error_does_not_drop_explicit_wake(jev_plugin, tmp_path, monkeypatch):
    p, bridge = jev_plugin
    path = tmp_path / 'voice.wav'
    path.write_bytes(b'audio')
    class Record:
        def __init__(self):
            self.file = str(path)
    class SendingEvent(MockEvent):
        async def send(self, chain):
            self.sent_marker = True
            return await super().send(chain)
    event = SendingEvent('听一下这段音频', message_id='copy-failure', components=[At('bot_42'), Record()])
    def fail(*args, **kwargs):
        raise OSError('no space left on device')
    monkeypatch.setattr(deferred_media.shutil, 'copyfile', fail)
    failure = None
    try:
        try:
            await p.on_group_message(event)
        except OSError as exc:
            failure = exc
        await flush(p, event)
        await drain(p)
        assert failure is None, f'copy failure escaped after native suppression={event.call_llm}: {failure}'
        assert event.replies_sent or not event.call_llm, 'mandatory wake has neither owned nor native fallback'
        assert event.sent_marker
    finally:
        await p.terminate()

@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['shadow_mode', 'exclude_groups', 'cooling', 'mute', 'remove', 'group_memory_enabled'])
async def test_reminder_send_is_cancelled_when_policy_changes(jev_plugin, tmp_path, policy):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    umo = 'mock:GroupMessage:group_100'
    row = p.group_memory.add_reminder(umo, text='should not send after policy changes', due_at=time.time() - 1)
    entered, release, sent = asyncio.Event(), asyncio.Event(), []
    async def send(umo, chain):
        entered.set()
        await release.wait()
        sent.append(chain_plain_text(chain))
        return 'reminder-policy-id'
    p.context.send_message = send
    job = asyncio.create_task(p.reminders.dispatch())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if policy == 'cooling':
            assert await p._cool_session_async(umo, 15)
        elif policy == 'mute':
            p.notebook_mutate('mute_tonight', {'umo': umo, 'hours': 1})
        elif policy == 'remove':
            p.notebook_mutate('remove_reminder', {'umo': umo, 'id': row['id']})
        else:
            p.config[policy] = (True if policy == 'shadow_mode' else False if policy == 'group_memory_enabled'
                                else ['group_100'])
            p._sync_runtime_from_config()
            if policy == 'shadow_mode':
                assert p.shadow_mode
            elif policy == 'group_memory_enabled':
                assert not p.group_memory.enabled
            else:
                assert not p.is_group_takeover_enabled('group_100')
        release.set()
        await job
        assert not sent, f'{policy} changed before platform delivery but reminder still sent: {sent}'
    finally:
        release.set()
        await asyncio.gather(job, return_exceptions=True)
        await p.terminate()

@pytest.mark.asyncio
async def test_sent_reminder_is_not_repeated_after_failed_ack_and_restart(jev_plugin, tmp_path, monkeypatch):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    umo = 'mock:GroupMessage:group_100'
    p.group_memory.add_reminder(umo, text='one shot', due_at=time.time() - 1)
    sent = []
    async def send(umo, chain):
        sent.append(chain_plain_text(chain))
        return 'reminder-ack-id'
    p.context.send_message = send
    original = group_memory.atomic_write_json
    def fail(path, data):
        if any(row.get('nudged') for row in data.get('reminders', [])):
            raise OSError('disk full while acknowledging send')
        return original(path, data)
    monkeypatch.setattr(group_memory, 'atomic_write_json', fail)
    try:
        await p.reminders.dispatch()
        assert len(sent) == 1 and p.reminders.delivered, 'failed-ack control failed'
        monkeypatch.setattr(group_memory, 'atomic_write_json', original)
        p.group_memory = GroupMemoryNotebook(tmp_path)
        p.reminders = ReminderDelivery(p)
        await p.reminders.dispatch()
        assert len(sent) == 1, f'one-shot reminder was delivered twice across restart: {sent}'
        assert p.group_memory.list_all(umo)['reminders'][0]['delivery_state'] == 'sending'
        assert p.group_memory.due_reminders(umo) == []
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_cold_start_reminder_has_actual_bot_identity(jev_plugin, tmp_path):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    event = MockEvent('后续消息', message_id='real-user', components=[At('bot_42')])
    umo = event.unified_msg_origin
    p.group_memory.add_reminder(umo, text='cold start', due_at=time.time() - 1)
    async def send(umo, chain):
        return 'cold-reminder-id'
    p.context.send_message = send
    try:
        await p.reminders.dispatch()
        runtime = p._sessions[umo]
        note = runtime.dag.nodes['cold-reminder-id']
        assert describe_message(note, runtime.dag, runtime.bot_id).sender_is_bot
        await p.on_group_message(event)
        assert runtime.bot_id == 'bot_42', 'real-bot identity control failed'
        assert note.user_id == runtime.bot_id, f'reminder is attributed to {note.user_id!r}, actual bot is {runtime.bot_id!r}'
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_annotation_post_honors_hidden_identifiers(monkeypatch, offline_web_responses):
    p, _ = fixture_plugin()
    p.dags['a'].nodes['m'].metadata['routing'].update(
        parent_message_id='HIDDEN_PARENT_MESSAGE', addressee_ids=['HIDDEN_MEMBER_QQ'])
    api = web_api.ConsoleWebAPI(p)
    await api.topic_annotations.save(label())
    monkeypatch.setattr(web_api, '_query_param', lambda name: 'a')
    read = await api.annotations_get()
    assert 'HIDDEN_MEMBER_QQ' not in json.dumps(read), 'GET privacy control failed'
    async def body():
        return label(expected_topic='UNKNOWN', expected_revision=read['data']['revisions']['m'])
    monkeypatch.setattr(web_api, '_json_body', body)
    posted = await api.annotations_post()
    assert posted['ok'], posted
    assert 'HIDDEN_MEMBER_QQ' not in json.dumps(posted), f'POST reveals identifiers hidden by GET: {posted}'

@pytest.mark.asyncio
async def test_topic_edit_in_hidden_mode_preserves_existing_recipient_labels(monkeypatch, offline_web_responses):
    p, _ = fixture_plugin()
    api = web_api.ConsoleWebAPI(p)
    await api.topic_annotations.save(label(recipient_ids=['member-42'], subject_ids=['member-17']))
    monkeypatch.setattr(web_api, '_query_param', lambda name: 'a')
    read = await api.annotations_get()
    assert read['data']['records'][0]['recipient_ids'] == [], 'hidden-response control failed'
    # The current editor prefills returned [] as "[]", then includes those values in a topic-only save.
    async def body():
        return label(expected_topic='UNKNOWN', expected_revision=read['data']['revisions']['m'],
            recipient_ids=read['data']['records'][0]['recipient_ids'], subject_ids=read['data']['records'][0]['subject_ids'])
    monkeypatch.setattr(web_api, '_json_body', body)
    posted = await api.annotations_post()
    assert posted['ok'], posted
    saved = (await api.topic_annotations.read('a'))['records'][0]
    assert saved['recipient_ids'] == ['member-42'] and saved['subject_ids'] == ['member-17'], f'topic-only edit erased labels: {saved}'


@pytest.mark.asyncio
async def test_reminder_write_before_send_failure_has_no_external_effect(jev_plugin, tmp_path, monkeypatch):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    umo = 'mock:GroupMessage:group_100'
    row = p.group_memory.add_reminder(umo, text='one shot', due_at=0)
    sends = []
    async def send(umo, chain):
        sends.append(chain)
        return True
    p.context.send_message = send
    original = group_memory.atomic_write_json
    def fail(*args, **kwargs):
        raise OSError('disk full before send')
    monkeypatch.setattr(group_memory, 'atomic_write_json', fail)
    try:
        await p.reminders.dispatch()
        assert not sends
        assert p.group_memory.due_reminders(umo)[0]['id'] == row['id']
        monkeypatch.setattr(group_memory, 'atomic_write_json', original)
        p.group_memory = GroupMemoryNotebook(tmp_path)
        await p.reminders.dispatch()
        assert len(sends) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_pending_reminder_identity_survives_runtime_restart(jev_plugin, tmp_path):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    event = MockEvent('真实消息', message_id='after-restart')
    umo = event.unified_msg_origin
    p.group_memory.add_reminder(umo, text='reminder', due_at=0)
    async def send(umo, chain):
        return 'persisted-reminder'
    p.context.send_message = send
    try:
        await p.reminders.dispatch()
        saved = export_runtime_state(p)
        p._registry.clear()
        restore_runtime_state(p, saved)
        runtime = p._sessions[umo]
        assert runtime.dag.nodes['persisted-reminder'].metadata['bot_identity_pending']
        await p.on_group_message(event)
        assert runtime.dag.nodes['persisted-reminder'].user_id == 'bot_42'
    finally:
        await p.terminate()


def _media_event(path):
    component = type('Image', (), {})()
    component.file = str(path)
    return MockEvent('图片', components=[component])


def test_media_archive_keeps_committed_paths_through_release_and_restart(tmp_path):
    now = [100.0]
    archive = MediaArchive(tmp_path / 'archive', ttl=10, now=lambda: now[0])
    source = tmp_path / 'picture.png'
    source.write_bytes(b'image')
    owned = deferred_media.preserve_event_media(_media_event(source), archive=archive, session='room')
    path = owned.message_obj.message[0].file
    archive.retain(owned, 'room', 'conv')
    deferred_media.release_event_media(owned)
    assert Path(path).read_bytes() == b'image' and source.exists()
    restarted = MediaArchive(archive.root, ttl=10, now=lambda: now[0])
    now[0] = 105
    restarted.touch('different-room', 'conv')
    now[0] = 111
    restarted.prune()
    assert not Path(path).exists() and source.exists()


def test_media_archive_refresh_and_quota_do_not_delete_active_copies(tmp_path):
    now = [100.0]
    archive = MediaArchive(tmp_path / 'archive', ttl=10, max_bytes=5, now=lambda: now[0])
    source = tmp_path / 'picture.png'
    source.write_bytes(b'image')
    owned = deferred_media.preserve_event_media(_media_event(source), archive=archive, session='room')
    now[0] = 1000
    archive.prune()
    assert Path(owned.message_obj.message[0].file).exists()
    with pytest.raises(OSError):
        deferred_media.preserve_event_media(_media_event(source), archive=archive, session='other-room')
    deferred_media.release_event_media(owned)
    archive.max_bytes = 1024
    owned = deferred_media.preserve_event_media(_media_event(source), archive=archive, session='room')
    archive.retain(owned, 'room', 'conv')
    now[0] = 1005
    archive.touch('room', 'conv')
    now[0] = 1011
    archive.prune()
    assert Path(owned.message_obj.message[0].file).exists()


def test_media_archive_history_compaction_only_releases_unreferenced_conversation_files(tmp_path):
    archive = MediaArchive(tmp_path / '媒体目录')
    source = tmp_path / 'picture.png'
    source.write_bytes(b'image')
    events = [deferred_media.preserve_event_media(_media_event(source), archive=archive, session='room')
              for _ in range(3)]
    paths = [Path(event.message_obj.message[0].file) for event in events]
    for index, event in enumerate(events):
        archive.retain(event, 'room', 'other-conv' if index == 2 else 'conv')
    history = [{'role': 'assistant', 'tool_calls': [{'function': {
        'arguments': json.dumps({'path': str(paths[0])})}}]}]
    archive.touch('room', 'conv', history=history)
    assert paths[0].exists() and not paths[1].exists() and paths[2].exists()


@pytest.mark.asyncio
async def test_partial_annotation_updates_preserve_untouched_fields_and_allow_explicit_clear():
    p, _ = fixture_plugin()
    store = TopicAnnotations(p)
    await store.save(label(recipient_ids=['member'], expected_reply=False, bot_targeted=True))
    await store.save(label(expected_topic='UNKNOWN', expected_reply=True))
    saved = (await store.read('a'))['records'][0]
    assert saved['recipient_ids'] == ['member'] and saved['expected_reply'] and saved['bot_targeted']
    await store.save(label(clear_recipient_fields=['recipient_ids', 'expected_reply']))
    saved = (await store.read('a'))['records'][0]
    assert 'recipient_ids' not in saved and 'expected_reply' not in saved and saved['bot_targeted']


@pytest.mark.asyncio
@pytest.mark.parametrize('cleared', [['unknown'], ['expected_reply', 'expected_reply'], 'recipient_ids', [False]])
async def test_annotation_rejects_invalid_clear_fields(cleared):
    p, _ = fixture_plugin()
    with pytest.raises(ValueError, match='cleared recipient'):
        await TopicAnnotations(p).save(label(clear_recipient_fields=cleared))


@pytest.mark.asyncio
async def test_reminder_transport_error_is_uncertain_and_not_automatically_retried(jev_plugin, tmp_path):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    umo = 'mock:GroupMessage:group_100'
    p.group_memory.add_reminder(umo, text='one shot', due_at=0)
    sends = []
    async def send(umo, chain):
        sends.append(chain)
        raise ConnectionError('connection lost after possible acceptance')
    p.context.send_message = send
    try:
        await p.reminders.dispatch()
        p.group_memory = GroupMemoryNotebook(tmp_path)
        p.reminders = ReminderDelivery(p)
        await p.reminders.dispatch()
        assert len(sends) == 1
        assert p.group_memory.list_all(umo)['reminders'][0]['delivery_state'] == 'uncertain'
    finally:
        await p.terminate()
