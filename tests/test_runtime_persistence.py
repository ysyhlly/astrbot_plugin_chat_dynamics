from collections import deque
from types import SimpleNamespace
import json
import pytest

from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRegistry, TopicState
from astrbot_plugin_chat_dynamics.core.telemetrics import TelemetricsTracker
from astrbot_plugin_chat_dynamics.core.runtime_persistence import export_runtime_state, restore_runtime_state
from astrbot_plugin_chat_dynamics.core import runtime_persistence as codec
from astrbot_plugin_chat_dynamics.core.topic_archive import ArchivedTopic
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import VibeAnalyzer, GroupChatMode
from astrbot_plugin_chat_dynamics.core.arbiter import InterventionArbiter, ArbitrationResult


def plugin(now):
    clock = SimpleNamespace(time=lambda: now)
    return SimpleNamespace(time_service=clock, _registry=SessionRegistry(clock),
        telemetrics=TelemetricsTracker(time_service=clock), _metrics={'received': 0},
        _shadow_decisions=deque(maxlen=50), _umo_by_session={}, _vibe_msg_counts={}, _last_bot_nodes={})


@pytest.mark.parametrize('kind', ['reply', 'mention', 'fragment', 'inferred_reply', 'semantic'])
def test_edge_contract_roundtrip(kind, monkeypatch):
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    runtime = source._registry.get_or_create('room')
    runtime.dag.add_message('p', 'bot', 'parent', timestamp=90)
    child = runtime.dag.add_message('c', 'u', 'child', timestamp=95,
                                   reply_to_id='p' if kind == 'reply' else None)
    runtime.dag._link_parent('c', 'p', kind=kind)
    child.metadata.update(quoted_author_id='bot', topic_source_text='完整语义')
    target = plugin(100)
    restore_runtime_state(target, export_runtime_state(source))
    dag = target._registry.get('room').dag
    assert dag.nodes['c'].edge_kinds == {'p': kind}
    assert dag.nodes['c'].metadata['quoted_author_id'] == 'bot'
    assert dag.nodes['c'].metadata['topic_source_text'] == '完整语义'
    assert dag.unlink_inferred_reply('c') is (kind in {'inferred_reply', 'semantic'})


def test_platform_message_identity_survives_runtime_snapshot():
    source = plugin(100)
    node = source._registry.get_or_create('room').dag.add_message(
        'platform-123', 'u', '原消息', timestamp=95,
        metadata={'platform_message_id': True})
    assert node.metadata['platform_message_id'] is True
    target = plugin(100)
    restore_runtime_state(target, export_runtime_state(source))
    restored = target._registry.get('room').dag.nodes['platform-123']
    assert restored.metadata['platform_message_id'] is True


def test_unknown_saved_edge_is_not_platform_fact(monkeypatch, caplog):
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    dag = source._registry.get_or_create('room').dag
    dag.add_message('p', 'u', 'p', timestamp=90)
    dag.add_message('c', 'u', 'c', timestamp=95)
    snapshot = export_runtime_state(source)
    row = snapshot['sessions'][0]['nodes'][1]
    row.update(parent_ids=['p'], edge_kinds={'p': 'unknown'})
    target = plugin(100)
    restore_runtime_state(target, snapshot)
    assert not target._registry.get('room').dag.nodes['c'].parent_ids
    assert 'Skipped snapshot edge with unknown or missing kind' in caplog.text


def test_first_large_session_obeys_complete_utf8_budget(monkeypatch):
    monkeypatch.setattr(codec, 'MAX_TOTAL_BYTES', 12000)
    source = plugin(100)
    dag = source._registry.get_or_create('room').dag
    for index in range(10):
        dag.add_message(str(index), 'u', '中' * 1000, timestamp=90 + index)
    snapshot = export_runtime_state(source)
    assert len(json.dumps(snapshot, ensure_ascii=False).encode('utf-8')) <= 12000
    assert snapshot['truncated'] is True
    assert snapshot['dropped_nodes'] > 0
    assert snapshot['sessions'][0]['nodes'][-1]['msg_id'] == '9'


def test_restored_inference_can_be_replaced_but_reply_is_protected(monkeypatch):
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    dag = source._registry.get_or_create('room').dag
    dag.add_message('p', 'bot', 'p', timestamp=90)
    dag.add_message('q', 'bot', 'q', timestamp=91)
    dag.add_message('c', 'u', 'c', timestamp=95)
    dag.add_message('explicit', 'u', 'reply', timestamp=96, reply_to_id='p')
    assert dag.link_inferred_reply('c', 'p', confidence=0.8, reason='test')
    target = plugin(100)
    restore_runtime_state(target, export_runtime_state(source))
    restored = target._registry.get('room').dag
    assert restored.link_inferred_reply('c', 'q', confidence=0.9, reason='replacement')
    assert restored.nodes['c'].parent_ids == {'q'}
    assert not restored.link_inferred_reply('explicit', 'q', confidence=0.9, reason='test')
    assert not restored.link_related('explicit', 'p')
    assert restored.nodes['explicit'].edge_kinds == {'p': 'reply'}


@pytest.mark.parametrize('explicit', [False, True])
def test_legacy_missing_edge_kind_requires_platform_reply(explicit, monkeypatch):
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    dag = source._registry.get_or_create('room').dag
    dag.add_message('c', 'u', 'child first', timestamp=95,
                    reply_to_id='p' if explicit else None)
    dag.add_message('p', 'u', 'parent later', timestamp=90)
    snapshot = export_runtime_state(source)
    child = next(row for row in snapshot['sessions'][0]['nodes'] if row['msg_id'] == 'c')
    child.update(parent_ids=['p'], edge_kinds={})
    target = plugin(100)
    restore_runtime_state(target, snapshot)
    restored = target._registry.get('room').dag
    assert restored.nodes['c'].edge_kinds == ({'p': 'reply'} if explicit else {})
    # Clock restoration retains natural expiration; no fresh TTL is granted.
    assert restored.prune(ttl_seconds=2, current_time=100) == 2


def test_snapshot_budget_includes_global_diagnostics(monkeypatch):
    monkeypatch.setattr(codec, 'MAX_TOTAL_BYTES', 1000)
    source = plugin(100)
    source._shadow_decisions.append({'reason': '中' * 16000})
    source._registry.get_or_create('room').dag.add_message('m', 'u', 'text', timestamp=99)
    snapshot = export_runtime_state(source)
    assert codec._encoded_size(snapshot) <= 1000
    assert snapshot['truncated']
    assert snapshot['dropped_sessions'] == 1


def test_roundtrip_rebases_clocks_and_preserves_isolation(monkeypatch):
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    original = plugin(100)
    for key in ('a:Group:1', 'b:Group:1'):
        runtime = original._registry.get_or_create(key, group_id='1', umo=key)
        first = runtime.dag.add_message('one', 'user', key, timestamp=90)
        runtime.last_bot_node = runtime.dag.add_message('two', 'bot', 'reply', timestamp=95, reply_to_id='one')
        first.metadata['raw_event'] = object()
        runtime.routing_state.topics['t'] = TopicState('t', message_ids=['one'], updated_at=90, created_at=80)
        runtime.pending_followup_fragments = ['must not send']
        original.telemetrics.record_message(key, 'hello', timestamp=90)
    original._metrics['received'] = 7
    snapshot = json.loads(json.dumps(export_runtime_state(original), allow_nan=False))
    assert 'raw_event' not in str(snapshot)
    # The retention rule travels with the snapshot: a reader (the learning
    # plugin, the replay page) can then say how long a message stays labelable
    # instead of assuming the cap and the TTL.
    assert snapshot['graph'] == {'max_nodes': 500, 'ttl_seconds': 3600.0}
    monkeypatch.setattr(codec.time, 'time', lambda: 1010)
    restored = plugin(30)
    restore_runtime_state(restored, snapshot)
    assert restored._metrics['received'] == 7
    for key, runtime in restored._registry.runtimes.items():
        assert runtime.dag.nodes['one'].text == key
        assert runtime.dag.nodes['one'].timestamp == 10
        assert runtime.last_bot_node.parent_ids == {'one'}
        assert runtime.routing_state.topics['t'].updated_at == 10
        assert restored.telemetrics._records[key][0].timestamp == 10
        assert runtime.pending_followup_fragments == []
        assert runtime.generation_task is None
    existing = restored._registry.runtimes['a:Group:1']
    restore_runtime_state(restored, snapshot)
    assert restored._registry.runtimes['a:Group:1'] is existing
    assert restored._metrics['received'] == 7


def test_replay_keeps_500_messages_after_restart(monkeypatch):
    from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks

    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(600)
    runtime = source._registry.get_or_create('room', group_id='room', umo='room')
    for index in range(500):
        runtime.dag.add_message(str(index), 'user', 'retained', timestamp=100 + index,
                               metadata={'routing': {'topic_id': 'topic'}})
    snapshot = json.loads(json.dumps(export_runtime_state(source)))
    target = plugin(700)
    restore_runtime_state(target, snapshot)
    restored = target._registry.get('room')
    # Same wall-clock instant, new monotonic epoch.
    display = SimpleNamespace(dags={'room': restored.dag}, console_show_message_content=False)
    blocks = replay_topic_blocks(display, [], 'room')
    assert blocks[0]['message_count'] == 500
    assert blocks[0]['messages'][0]['msg_id'] == '0'
    assert blocks[0]['messages'][-1]['msg_id'] == '499'


def test_corruption_unknown_versions_and_cross_umo_are_ignored(monkeypatch):
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    target = plugin(10)
    restore_runtime_state(target, {'version': 99})
    snapshot = {'version': 1, 'saved_wall': 1000, 'saved_clock': 10,
        'sessions': [None, {'session_key': 'bad', 'umo': 'other'},
                     {'session_key': 'ok', 'umo': 'ok', 'nodes': [None, {'msg_id': 'x'}],
                      'topics': [None], 'telemetrics': ['bad']}],
        'metrics': {'received': -1}, 'shadow_decisions': [None]}
    restore_runtime_state(target, snapshot)
    assert set(target._registry.runtimes) == {'ok'}
    assert not target._registry.runtimes['ok'].dag.nodes
    assert target._metrics['received'] == 0


def test_archive_diagnostics_and_late_parent(monkeypatch):
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    source.vibe_analyzer = VibeAnalyzer(source.telemetrics)
    source.arbiter = InterventionArbiter()
    source.vibe_analyzer.set_mode('room', GroupChatMode.SERIOUS_INQUIRY, source='llm')
    source.arbiter._last_decisions['room'] = ArbitrationResult(False, 0.2, 0.6, 'wait')
    runtime = source._registry.get_or_create('room')
    runtime.last_occasion = {'reason': 'quiet'}
    runtime.dag.add_message('child', 'u', 'reply', timestamp=95, reply_to_id='parent')
    runtime.routing_state.archive.entries['old'] = ArchivedTopic('old', 'summary', ('text',), frozenset({'u'}), 80, 90)
    source._shadow_decisions.append({'session_key': 'room', 'timestamp': 90, 'reason': 'quiet'})
    snapshot = export_runtime_state(source)
    monkeypatch.setattr(codec.time, 'time', lambda: 1010)
    target = plugin(50)
    target.vibe_analyzer = VibeAnalyzer(target.telemetrics)
    target.arbiter = InterventionArbiter()
    restore_runtime_state(target, snapshot)
    restored = target._registry.get('room')
    assert restored.routing_state.archive.entries['old'].updated_at == 20
    assert restored.last_occasion == {'reason': 'quiet'}
    assert target.vibe_analyzer._current_modes['room'] == GroupChatMode.SERIOUS_INQUIRY
    assert target.arbiter.last_decision('room').reason == 'wait'
    restored.dag.add_message('parent', 'u', 'earlier', timestamp=30)
    assert restored.dag.nodes['child'].parent_ids == {'parent'}
    restore_runtime_state(target, snapshot)
    assert len(target._shadow_decisions) == 1


def test_an_absurd_integer_does_not_abort_the_whole_restore(monkeypatch):
    """一个 400 位整数曾经让整次恢复抛 OverflowError，连正常的会话一起丢掉。"""
    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    target = plugin(10)
    snapshot = {'version': 1, 'saved_wall': 1000, 'saved_clock': 10, 'sessions': [
        {'session_key': 'bad', 'umo': 'bad', 'nodes': [
            {'msg_id': 'x', 'user_id': 'u', 'text': 't', 'timestamp': 10 ** 400}]},
        {'session_key': 'ok', 'umo': 'ok', 'nodes': [
            {'msg_id': 'y', 'user_id': 'u', 'text': 't', 'timestamp': 90}]},
    ]}

    restore_runtime_state(target, snapshot)

    assert 'ok' in target._registry.runtimes
    assert 'y' in target._registry.runtimes['ok'].dag.nodes


def test_metrics_recorded_outside_the_fixed_list_are_still_restored(monkeypatch):
    """恢复循环只更新已存在的键，所以这些名字必须出现在 _METRIC_NAMES 里。"""
    from astrbot_plugin_chat_dynamics.main import _METRIC_NAMES

    for name in ("config_saved", "config_applied", "shadow_telemetry_persist_failed"):
        assert name in _METRIC_NAMES

    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    source._metrics = {name: 0 for name in _METRIC_NAMES}
    source._metrics["config_saved"] = 3
    snapshot = export_runtime_state(source)

    target = plugin(30)
    target._metrics = {name: 0 for name in _METRIC_NAMES}
    restore_runtime_state(target, snapshot)

    assert target._metrics["config_saved"] == 3


def test_a_quiet_window_survives_a_restart(monkeypatch):
    """“今天别闹”是六小时指令：重启不能把它连同内存会话一起丢掉。"""
    from astrbot_plugin_chat_dynamics.core.occasion_skin import OccasionClassifier

    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    source.decision_gate = SimpleNamespace(occasion=OccasionClassifier())
    source.decision_gate.occasion.note_cool_command('room', duration=6 * 3600, now=1000)
    snapshot = export_runtime_state(source)
    assert snapshot['occasion_cool'] == {'room': 1000 + 6 * 3600}

    monkeypatch.setattr(codec.time, 'time', lambda: 1010)
    target = plugin(30)
    target.decision_gate = SimpleNamespace(occasion=OccasionClassifier())
    restore_runtime_state(target, snapshot)
    assert target.decision_gate.occasion.cool_remaining('room', now=1010) == 6 * 3600 - 10

    # 已经过期的窗口不能因为重启而复活。
    expired = 1000 + 6 * 3600 + 1
    monkeypatch.setattr(codec.time, 'time', lambda: expired)
    late = plugin(30)
    late.decision_gate = SimpleNamespace(occasion=OccasionClassifier())
    restore_runtime_state(late, snapshot)
    assert late.decision_gate.occasion.cool_remaining('room', now=expired) == 0


def test_the_compact_trace_inputs_survive_a_restart(monkeypatch):
    """重启后重建的 trace 仍要带上 participation —— 那是学习层回放阈值的唯一依据。"""
    from astrbot_plugin_chat_dynamics.core.routing_trace import (
        build_routing_trace, compact_trace_inputs,
    )

    monkeypatch.setattr(codec.time, 'time', lambda: 1000)
    source = plugin(100)
    runtime = source._registry.get_or_create('room', group_id='room', umo='room')
    trace = build_routing_trace(
        routing={'topic_id': 't', 'topic_confidence': 0.8, 'addressee_ids': ['bot']},
        identity={'bot_reference': 'vocative', 'vocative': True,
                  'mention': False, 'subject': False},
        participation={'score': 0.81, 'level': 'strong', 'should_reply': True,
                       'evidence': [{'code': 'vocative', 'family': 'recipient',
                                     'source': 'identity_matcher', 'strength': 1.0,
                                     'raw_value': 1.0}],
                       'family_contributions': {'recipient': 0.31},
                       'contribution_total': 0.81},
        state={'pending_hover': False, 'intervening_users': 0},
        mode='legacy', weights_version='v3')
    runtime.dag.add_message('m1', 'user', 'hello', timestamp=90,
                            metadata={'routing': {'topic_id': 't'},
                                      'decision_trace': trace,
                                      'trace_inputs': compact_trace_inputs(trace)})
    snapshot = json.loads(json.dumps(export_runtime_state(source), allow_nan=False))

    target = plugin(110)
    restore_runtime_state(target, snapshot)

    restored = target._registry.get('room').dag.nodes['m1']
    assert 'decision_trace' not in restored.metadata, '完整快照只留在内存里'
    inputs = restored.metadata['trace_inputs']
    assert inputs['participation']['contribution_total'] == 0.81
    assert inputs['participation']['evidence'][0]['code'] == 'vocative'
    assert inputs['identity']['bot_reference'] == 'vocative'
    assert inputs['mode'] == 'legacy'

    # 用持久化下来的输入重建，得到的是同一个 trace，而不是一个全是 null 的骨架。
    rebuilt = build_routing_trace(
        routing=restored.metadata['routing'], identity=inputs['identity'],
        participation=inputs['participation'], state=inputs['state'],
        mode=inputs['mode'], weights_version=inputs['weights_version'])
    assert rebuilt['participation']['score'] == 0.81
    assert rebuilt['participation']['contribution_total'] == 0.81
    assert rebuilt['participation']['evidence'][0]['code'] == 'vocative'
