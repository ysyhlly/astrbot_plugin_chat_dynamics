import json
from collections import deque
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.group_context import (
    member_notice, remember_notice, select_group_events, export_group_events, restore_group_events,
)
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import build_state
from astrbot_plugin_chat_dynamics.core.persona_engine import refresh_topic_context
from astrbot_plugin_chat_dynamics.core.turn_decision import reply_prompt, TurnDecision
from .test_context_retrieval import route
from .test_persona_explicit_context import _runtime, _snapshot
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent, At


def notice(kind="group_increase", **changes):
    raw = dict(post_type="notice", notice_type=kind, sub_type="invite", group_id="room",
               user_id=12345, operator_id=67890, time=1000)
    raw.update(changes)
    event = NS(message_obj=NS(raw_message=raw))
    parsed = NS(group_id="room", sender_id="12345", sender_name="新成员", message_id="notice-id")
    return member_notice(event, parsed, now=100, wall_time=1000)


@pytest.mark.parametrize("changes", [
    {"post_type": "request"}, {"post_type": "message"}, {"group_id": "other"},
    {"user_id": 999}, {"time": 300}, {"time": float('nan')}, {"time": True},
])
def test_only_current_matching_platform_facts_are_accepted(changes):
    assert notice(**changes) is None


def test_dedup_ttl_watermark_and_restore():
    runtime = NS(scope_kind="group", group_events=deque(maxlen=32))
    row = notice()
    assert remember_notice(runtime, row)
    assert not remember_notice(runtime, row)
    assert select_group_events(runtime, 99) == ()
    assert select_group_events(runtime, 101)[0].member_id == '12345'
    assert select_group_events(runtime, 701) == ()
    saved = export_group_events(runtime, 101)
    restored = NS(scope_kind="group", group_events=deque(maxlen=32))
    restore_group_events(restored, saved, shift=900, now=1010)
    assert select_group_events(restored, 1010)[0].payload(1010)['age_seconds'] == 10
    private = NS(scope_kind="private", group_events=deque(maxlen=32))
    restore_group_events(private, saved, shift=900, now=1010)
    assert not private.group_events


def test_late_received_notice_does_not_enter_earlier_turn():
    row = replace(notice(), observed_at=80, received_at=120)
    runtime = NS(scope_kind="group", group_events=(row,))
    assert select_group_events(runtime, 100) == ()


def test_current_member_notice_is_kept_during_join_burst():
    rows = [replace(notice(), event_id=f'notice:{i:024}', member_id=str(10000 + i),
                    observed_at=100 + i, received_at=100 + i) for i in range(8)]
    runtime = NS(scope_kind='group', group_events=rows)
    selected = select_group_events(runtime, 110, member_ids={'10000'})
    assert len(selected) == 4 and selected[0].member_id == '10000'


def test_notice_survives_runtime_reload_but_expires_and_reset_clears_it(monkeypatch):
    from .test_runtime_persistence import plugin
    from astrbot_plugin_chat_dynamics.core import runtime_persistence as codec
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(101)
    runtime = source._registry.get_or_create('room')
    remember_notice(runtime, notice())
    saved = codec.export_runtime_state(source)
    target = plugin(1001)
    codec.restore_runtime_state(target, saved)
    restored = target._registry.get('room')
    assert select_group_events(restored, 1001)[0].member_id == '12345'
    assert not select_group_events(target._registry.get_or_create('other'), 1001)
    restored.reset_conversation_state()
    assert not restored.group_events
    monkeypatch.setattr(codec.time, 'time', lambda: 2000)
    expired = plugin(2001)
    codec.restore_runtime_state(expired, saved)
    assert not expired._registry.get('room').group_events


@pytest.mark.asyncio
async def test_real_notice_is_background_not_a_user_turn_and_reaches_reply_llm(jev_plugin):
    plugin, bridge = jev_plugin
    event = MockEvent('', sender_id='12345', message_id='join')
    event.message_obj.raw_message = {
        'post_type': 'notice', 'notice_type': 'group_increase', 'sub_type': 'invite',
        'group_id': event.group_id, 'user_id': 12345, 'operator_id': 67890,
        'time': plugin.time_service.wall_time(),
    }
    try:
        await plugin.on_group_message(event)
        runtime = plugin._sessions[event.unified_msg_origin]
        assert not bridge.requests and not plugin.jev.calls and not event.replies_sent
        assert runtime.dag.get_node('join') is None
        assert len(runtime.group_events) == 1
        followup = MockEvent('欢迎，新来的这位是谁呀？', sender_id='45678', message_id='welcome', components=[At('bot_42')])
        await plugin.on_group_message(followup)
        await flush(plugin, followup)
        await drain(plugin)
        prompt = bridge.requests[0][0]
        fact = prompt['conversation']['group_events'][0]
        assert fact['kind'] == 'member_joined' and fact['member_id'] == '12345'
        assert fact['operator_id'] == '67890'
        assert prompt['conversation']['author'] == '45678'
        assert 'join' not in [m['message_id'] for m in prompt['conversation']['messages']]
    finally:
        await plugin.terminate()


def test_confirmed_topic_retrieves_older_constraints_without_other_topic_or_future():
    dag = ConversationDAG('room')
    route(dag.add_message('old', 'alice', '需要离线运行，预算只有500元', timestamp=100), 'task')
    route(dag.add_message('wrong', 'bob', '另一个游戏的话题', timestamp=950), 'game')
    current = route(dag.add_message('current', 'carol', '这个方案要怎么改？', timestamp=1000), 'task')
    item = _snapshot(_runtime(dag), current)
    route(dag.add_message('future', 'alice', '后来改变了限制', timestamp=1001), 'task')
    payload = build_state(item.context)['conversation']
    assert [m['message_id'] for m in payload['background']] == ['old']
    assert payload['background'][0]['age_seconds'] == 900
    assert '500元' in json.loads(reply_prompt(item.context, TurnDecision.fallback(item.context, 'test')))['conversation']['background'][0]['text']


def test_stale_topic_and_same_author_do_not_expand_indiscriminately():
    dag = ConversationDAG('room')
    route(dag.add_message('stale', 'alice', '过期话题', timestamp=1), 'task')
    dag.add_message('author', 'carol', '同一个人以前的闲聊', timestamp=100)
    current = route(dag.add_message('current', 'carol', '继续', timestamp=2000), 'task')
    assert _snapshot(_runtime(dag), current).context.background == ()


def test_notice_member_boundary_filters_facts(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import member_boundary
    dag = ConversationDAG('room')
    current = dag.add_message('current', 'speaker', '欢迎', timestamp=101)
    runtime = _runtime(dag)
    runtime.group_events = (notice(),)
    monkeypatch.setattr(member_boundary, 'filter_frames', lambda host, event, frames, **kwargs:
                        ([f for f in frames if not f['frame_id'].startswith('notice:')], NS(available=True)))
    assert _snapshot(runtime, current).context.group_events == ()


def test_topic_refresh_cannot_restore_blocked_background(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import member_boundary
    dag = ConversationDAG('room')
    route(dag.add_message('hidden', 'alice', '不应传给模型的内容', timestamp=100), 'task')
    current = dag.add_message('current', 'bob', '继续这个问题', timestamp=1000)
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    route(current, 'task')
    monkeypatch.setattr(member_boundary, 'filter_frames', lambda host, event, frames, **kwargs:
                        ([f for f in frames if f['frame_id'] != 'hidden'], NS(available=True)))
    assert refresh_topic_context(runtime, item).context.background == ()
