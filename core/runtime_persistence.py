"""Versioned, bounded JSON snapshots. Executable and pending state is never saved."""
from __future__ import annotations

import math
import time
from collections import deque

from .graph import ConversationNode
from .session_runtime import TopicState
from .topic_archive import ArchivedTopic
from .telemetrics import _MessageRecord
from .arbiter import ArbitrationResult
from .vibe_analyzer import GroupChatMode

VERSION = 1
MAX_SESSIONS = 1000
MAX_NODES = 500
RUNTIME_FIELDS = ('last_activity', 'last_model_send', 'last_interlocutor',
                  'last_length_hint', 'last_delay_scale', 'last_rhythm_action',
                  'vibe_message_count', 'turn_sequence', 'model_diagnostic')
TOPIC_FIELDS = ('topic_id', 'message_ids', 'participants', 'updated_at', 'label',
                'generated_title', 'title_attempted', 'created_at', 'exemplar_messages',
                'centroid_vector', 'recent_message_ids', 'keywords', 'centroid_space',
                'summary_excerpts')
NODE_FIELDS = ('msg_id', 'user_id', 'text', 'timestamp', 'reply_to_id',
               'mentioned_users', 'parent_ids', 'thread_id', 'edge_kinds')
META_FIELDS = ('topic_id', 'routing', 'is_bot', 'source', 'addressivity', 'decision',
               'vibe_mode', 'sender_name', 'display_name', 'turn_id', 'topic_title',
               'edge_metadata', 'inferred_parent_id', 'is_wake')
SHADOW_FIELDS = ('session_key', 'timestamp', 'action', 'reason', 'willingness_score',
                 'threshold', 'topic_relevance', 'professionalism', 'question_value',
                 'participation', 'state', 'length', 'target_message_ids')
ARBITRATION_FIELDS = ('should_speak', 'willingness_score', 'threshold', 'reason',
                      'in_deep_cooling', 'is_energy_asymmetric', 'professionalism',
                      'topic_relevance', 'fatigue_penalty', 'question_value',
                      'participation', 'private_topic')


def _json(value, depth=0):
    if depth > 6:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:16000]
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k[:256]: _json(v, depth + 1) for k, v in list(value.items())[:128]
                if isinstance(k, str) and not k.startswith('_')}
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return [_json(v, depth + 1) for v in list(value)[-1024:]]
    return None


def _fields(obj, names):
    return {key: _json(getattr(obj, key)) for key in names}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('invalid number')
    return float(value)


def _strings(value):
    return [x[:16000] for x in value[-1024:] if isinstance(x, str)] if isinstance(value, list) else []


def _wall(plugin):
    return getattr(plugin.time_service, 'wall_time', time.time)()


def export_runtime_state(plugin) -> dict:
    sessions = []
    runtimes = sorted(plugin._registry.runtimes.values(), key=lambda r: r.last_activity)
    for runtime in runtimes[-MAX_SESSIONS:]:
        item = _fields(runtime, RUNTIME_FIELDS)
        item.update(session_key=runtime.session_key, group_id=runtime.group_id,
                    umo=runtime.umo, bot_id=runtime.bot_id)
        item['nodes'] = []
        if runtime.dag:
            for node in runtime.dag.get_recent_nodes(MAX_NODES):
                saved = _fields(node, NODE_FIELDS)
                saved['metadata'] = _json({k: v for k, v in node.metadata.items() if k in META_FIELDS})
                item['nodes'].append(saved)
        item['last_bot_id'] = runtime.last_bot_node.msg_id if runtime.last_bot_node else None
        item['topics'] = [_fields(topic, TOPIC_FIELDS) for topic in list(runtime.routing_state.topics.values())[-80:]]
        item['archive'] = [_fields(topic, ('topic_id', 'summary', 'exemplars', 'participants', 'updated_at', 'archived_at', 'title'))
                           for topic in list(runtime.routing_state.archive.entries.values())[-32:]]
        item['last_topic_id'] = runtime.routing_state.last_topic_id
        item['last_bot_topic_id'] = runtime.routing_state.last_bot_topic_id
        for name in ('last_occasion', 'last_manners'):
            item[name] = _json(getattr(runtime, name, {}))
        vibe = getattr(plugin, 'vibe_analyzer', None)
        if vibe is not None:
            mode = vibe._current_modes.get(runtime.session_key)
            item['vibe'] = {'mode': mode.value if isinstance(mode, GroupChatMode) else None,
                            'source': vibe._mode_sources.get(runtime.session_key, 'telemetrics'),
                            'snapshot_time': vibe._last_llm_snapshot_time.get(runtime.session_key),
                            'snapshot_count': vibe._llm_snapshot_counts.get(runtime.session_key, 0)}
        arbiter = getattr(plugin, 'arbiter', None)
        if arbiter is not None:
            decision = arbiter.last_decision(runtime.session_key)
            item['arbitration'] = _fields(decision, ARBITRATION_FIELDS) if decision else None
        item['telemetrics'] = [_fields(record, ('timestamp', 'char_count', 'emoji_count', 'media_count', 'has_formal_punc', 'text', 'user_id'))
                               for record in list(plugin.telemetrics._records.get(runtime.session_key, ()))[-200:]]
        sessions.append(item)
    return {'version': VERSION, 'saved_wall': _wall(plugin),
            'saved_clock': plugin.time_service.time(), 'sessions': sessions,
            'metrics': _json(plugin._metrics),
            'shadow_decisions': [_json({k: v for k, v in row.items() if k in SHADOW_FIELDS})
                                 for row in list(plugin._shadow_decisions)[-50:]]}


def restore_runtime_state(plugin, payload) -> None:
    if not isinstance(payload, dict) or payload.get('version') != VERSION:
        return
    try:
        shift = _number(plugin.time_service.time()) - _number(payload['saved_clock']) - max(0, _wall(plugin) - _number(payload['saved_wall']))
    except (KeyError, ValueError, TypeError):
        return
    rows = payload.get('sessions', [])
    if not isinstance(rows, list):
        return
    restored_keys = set()
    for item in rows[-min(MAX_SESSIONS, plugin._registry.max_sessions):]:
        if not isinstance(item, dict):
            continue
        key = item.get('session_key')
        if not isinstance(key, str) or not key.strip() or len(key) > 1024 or key in plugin._registry.runtimes:
            continue
        # A snapshot may not redirect one session into another UMO.
        if item.get('umo') != key:
            continue
        if plugin._registry.capacity_reached():
            break
        runtime = plugin._registry.get_or_create(key, group_id=str(item.get('group_id') or key), umo=key,
                                                 bot_id=str(item.get('bot_id') or ''))
        restored_keys.add(key)
        for name in RUNTIME_FIELDS:
            value = item.get(name)
            default = getattr(runtime, name)
            if isinstance(default, str) and isinstance(value, str):
                setattr(runtime, name, value[:16000])
            elif isinstance(default, dict) and isinstance(value, dict):
                setattr(runtime, name, _json(value))
            elif isinstance(default, (float, int)):
                try:
                    value = _number(value)
                    if name in ('last_activity', 'last_model_send'):
                        value = value + shift if value else 0.0
                    setattr(runtime, name, int(value) if isinstance(default, int) else value)
                except ValueError:
                    pass
        nodes = item.get('nodes', [])
        for row in nodes[-min(MAX_NODES, runtime.dag.max_nodes):] if isinstance(nodes, list) else []:
            try:
                if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('msg_id', 'user_id', 'text')) or not row['msg_id']:
                    continue
                node = ConversationNode(row['msg_id'][:1024], row['user_id'][:1024], row['text'][:16000], _number(row.get('timestamp')) + shift,
                                        reply_to_id=row.get('reply_to_id') if isinstance(row.get('reply_to_id'), str) else None,
                                        mentioned_users=_strings(row.get('mentioned_users')),
                                        thread_id=str(row.get('thread_id') or row['msg_id'])[:1024])
                meta = row.get('metadata', {})
                node.metadata = _json({k: v for k, v in meta.items() if k in META_FIELDS}) if isinstance(meta, dict) else {}
                if node.msg_id not in runtime.dag.nodes:
                    runtime.dag.nodes[node.msg_id] = node
                    runtime.dag.chronological_ids.append(node.msg_id)
            except (TypeError, ValueError):
                continue
        for row in nodes[-MAX_NODES:] if isinstance(nodes, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get('msg_id'), str):
                continue
            kinds = row.get('edge_kinds', {})
            for parent in _strings(row.get('parent_ids')):
                kind = kinds.get(parent, 'reply') if isinstance(kinds, dict) else 'reply'
                runtime.dag._link_parent(row['msg_id'], parent, kind=kind if kind in ('reply', 'mention', 'semantic') else 'reply')
        for node in runtime.dag.nodes.values():
            if node.reply_to_id and node.reply_to_id not in runtime.dag.nodes:
                runtime.dag._waiting_children.setdefault(node.reply_to_id, set()).add(node.msg_id)
        topics = item.get('topics', [])
        for row in topics[-80:] if isinstance(topics, list) else []:
            try:
                if not isinstance(row, dict) or not isinstance(row.get('topic_id'), str):
                    continue
                topic = TopicState(row['topic_id'][:1024])
                for name in TOPIC_FIELDS[1:]:
                    value = row.get(name)
                    if name in ('updated_at', 'created_at'):
                        setattr(topic, name, _number(value) + shift)
                    elif name in ('participants', 'keywords'):
                        setattr(topic, name, set(_strings(value)))
                    elif name == 'exemplar_messages':
                        topic.exemplar_messages = [(x[0][:16000], _number(x[1]) + shift) for x in value[-32:]
                                                   if isinstance(x, list) and len(x) == 2 and isinstance(x[0], str)] if isinstance(value, list) else []
                    elif name == 'centroid_vector':
                        topic.centroid_vector = [_number(x) for x in value[:1024]] if isinstance(value, list) else None
                    elif isinstance(getattr(topic, name), list):
                        setattr(topic, name, _strings(value))
                    elif isinstance(value, type(getattr(topic, name))):
                        setattr(topic, name, value[:16000] if isinstance(value, str) else value)
                runtime.routing_state.topics[topic.topic_id] = topic
            except (ValueError, TypeError):
                continue
        archive = item.get('archive', [])
        for row in archive[-32:] if isinstance(archive, list) else []:
            try:
                if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('topic_id', 'summary')):
                    continue
                topic = ArchivedTopic(row['topic_id'][:1024], row['summary'][:16000], tuple(_strings(row.get('exemplars'))),
                                      frozenset(_strings(row.get('participants'))), _number(row.get('updated_at')) + shift,
                                      _number(row.get('archived_at')) + shift, str(row.get('title') or '')[:16000])
                runtime.routing_state.archive.entries[topic.topic_id] = topic
            except (TypeError, ValueError):
                continue
        for name in ('last_topic_id', 'last_bot_topic_id'):
            value = item.get(name)
            if isinstance(value, str) and value in runtime.routing_state.topics:
                setattr(runtime.routing_state, name, value)
        last_bot = item.get('last_bot_id')
        runtime.last_bot_node = runtime.dag.nodes.get(last_bot) if isinstance(last_bot, str) else None
        records = item.get('telemetrics', [])
        restored = deque(maxlen=plugin.telemetrics.max_history)
        for row in records[-200:] if isinstance(records, list) else []:
            try:
                if not isinstance(row, dict) or not isinstance(row.get('text'), str):
                    continue
                restored.append(_MessageRecord(_number(row.get('timestamp')) + shift,
                    *[max(0, int(_number(row.get(k)))) for k in ('char_count', 'emoji_count', 'media_count')],
                    row.get('has_formal_punc') is True, row['text'][:1000], str(row.get('user_id') or '')[:1024]))
            except (TypeError, ValueError):
                continue
        plugin.telemetrics._records[key] = restored
        plugin._umo_by_session[key] = key
        plugin._vibe_msg_counts[key] = runtime.vibe_message_count
        if runtime.last_bot_node:
            plugin._last_bot_nodes[key] = runtime.last_bot_node
        for name in ('last_occasion', 'last_manners'):
            if isinstance(item.get(name), dict):
                setattr(runtime, name, _json(item[name]))
        vibe = getattr(plugin, 'vibe_analyzer', None)
        row = item.get('vibe')
        if vibe is not None and isinstance(row, dict):
            try:
                mode = GroupChatMode(row.get('mode'))
                source = row.get('source')
                vibe.set_mode(key, mode, source=source if isinstance(source, str) else 'telemetrics')
                if row.get('snapshot_time') is not None:
                    vibe._last_llm_snapshot_time[key] = _number(row['snapshot_time']) + shift
                vibe._llm_snapshot_counts[key] = max(0, int(_number(row.get('snapshot_count', 0))))
            except (ValueError, TypeError):
                pass
        arbiter = getattr(plugin, 'arbiter', None)
        row = item.get('arbitration')
        if arbiter is not None and isinstance(row, dict):
            try:
                decision = ArbitrationResult(False, 0.0, 0.0, '')
                for name in ARBITRATION_FIELDS:
                    value = row.get(name)
                    default = getattr(decision, name)
                    if isinstance(default, bool):
                        if not isinstance(value, bool):
                            raise ValueError('invalid flag')
                        setattr(decision, name, value)
                    elif isinstance(default, str):
                        if not isinstance(value, str):
                            raise ValueError('invalid reason')
                        setattr(decision, name, value[:16000])
                    else:
                        setattr(decision, name, _number(value))
                arbiter._last_decisions[key] = decision
            except (ValueError, TypeError):
                pass
    metrics = payload.get('metrics', {})
    if isinstance(metrics, dict):
        for key in plugin._metrics:
            value = metrics.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
                plugin._metrics[key] = value
    decisions = payload.get('shadow_decisions', [])
    for row in decisions[-50:] if isinstance(decisions, list) else []:
        try:
            if not isinstance(row, dict) or not isinstance(row.get('session_key'), str) or row['session_key'] not in restored_keys:
                continue
            saved = _json({k: v for k, v in row.items() if k in SHADOW_FIELDS})
            saved['timestamp'] = _number(row.get('timestamp')) + shift
            plugin._shadow_decisions.append(saved)
        except (TypeError, ValueError):
            continue
