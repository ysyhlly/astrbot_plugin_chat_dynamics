from collections import deque
from types import SimpleNamespace
import json

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
