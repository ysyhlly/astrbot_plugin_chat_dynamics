"""Optional scene companion: one bounded clarification, then a frozen selection."""
from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy

from ..conversation_intent import INTENT_CRITERIA

TIMEOUT_SECONDS = 5.0


async def prepare_all(prompt, runtime, item):
    prepared = await prepare(prompt, runtime, item)
    from .expression_style import prepare as prepare_expression
    return prepare_expression(prepared, runtime, item)


def clarification_input(host, capture, runtime, item, conversation):
    """Filter the current speaker's exchange before any extra model request."""
    from .. import member_boundary
    events = getattr(item, 'events', ())
    if not events:
        return None
    member = capture.scope['member']
    authors = {member, str(getattr(runtime, 'bot_id', ''))} - {''}
    frames = [{'author_uid': member, 'subject_id': member, 'source_uids': [member],
               'provenance_complete': True, '_scene_input': 'current', 'text': capture.query}]
    for index, row in enumerate(conversation.get('background', [])[-6:]):
        if not isinstance(row, dict) or row.get('author') not in authors or not isinstance(row.get('text'), str):
            continue
        frames.append({'author_uid': row['author'], 'subject_id': member, 'source_uids': [row['author']],
                       'provenance_complete': True, '_scene_input': str(index), 'text': row['text'][:800]})
    filtered, state = member_boundary.filter_material(host, events[-1], {'context_items': frames}, purpose='reply',
                                                     scope=getattr(capture, 'character_scope', None))
    if not state.available and (member_boundary.fail_closed(state) or state.service is not None):
        return None
    rows = filtered.get('context_items', []) if isinstance(filtered, dict) else []
    current_text = next((r.get('text') for r in rows if r.get('_scene_input') == 'current'), None)
    if not isinstance(current_text, str) or not current_text:
        return None
    return current_text, [{'author': r['author_uid'], 'text': r['text']} for r in rows
                          if r.get('_scene_input') != 'current' and r.get('author_uid') in authors and isinstance(r.get('text'), str)]


def labels(raw):
    result = {}
    for key, choices in INTENT_CRITERIA.items():
        if key == "speaker_stance":
            continue  # This turn's evaluation of a person is not reusable group style.
        item = raw.get(key, {}) if isinstance(raw, dict) else {}
        if isinstance(item, dict):
            confidence = item.get('confidence')
            item = item.get('choice') if type(confidence) in (int, float) and (.8 if key == 'expression_mode' else .7) <= confidence <= 1 else 'unclear'
        result[key] = item if isinstance(item, str) and item in choices else 'unclear'
    return result


def validate(packet, scope):
    if not isinstance(packet, dict) or packet.get('schema_version') != 1:
        raise ValueError('invalid scene packet')
    if any(packet.get(k) != scope.get(k) for k in ('scope', 'persona', 'member')) or ':GroupMessage:' not in scope['scope']:
        raise ValueError('scene scope mismatch')
    items = packet.get('items')
    if not isinstance(items, list) or len(items) > 1 or len(json.dumps(packet, ensure_ascii=False, allow_nan=False)) > 4000:
        raise ValueError('scene packet exceeds budget')
    for row in items:
        if (not isinstance(row, dict) or row.get('category') != 'scene_pattern' or row.get('member') != ''
                or row.get('source_umo') != scope['scope'] or not isinstance(row.get('source_id'), str)
                or type(row.get('revision')) is not int or row['revision'] < 1
                or not isinstance(row.get('value'), dict) or not isinstance(row.get('source_uids'), list)
                or row.get('provenance_complete') is not True):
            raise ValueError('invalid scene item')
    if packet.get('versions') != [r['source_id'] for r in items]:
        raise ValueError('scene versions mismatch')
    return deepcopy(packet)


def current(source, packet):
    if not isinstance(packet, dict):
        return True
    try:
        if 'scene_learning_v1' not in source.learning_capabilities().get('capabilities', []):
            return False
        scope = {k: packet[k] for k in ('scope', 'persona', 'member')}
        frozen = [{k: r[k] for k in ('source_id', 'revision')} for r in packet['items']]
        latest = validate(source.get_scene_context(**scope, frozen=frozen), scope)
        return latest['items'] == packet['items']
    except Exception:
        return False


def report(source, record):
    try:
        method = getattr(source, 'report_scene_event', None)
        if callable(method):
            method(record)
    except Exception:
        pass


async def prepare(prompt, runtime, item):
    from . import dynamics_learning as bridge
    capture = bridge.capture_for(runtime, item)
    if capture is None or getattr(item, 'private_direct', False):
        return bridge.augment_reply(prompt, runtime, item)
    host, source = capture.host(), capture.source()
    if host is None or source is None or bridge.provider(host) is not source:
        return bridge.augment_reply(prompt, runtime, item)
    try:
        caps = source.learning_capabilities().get('capabilities', [])
        if 'scene_learning_v1' not in caps:
            return bridge.augment_reply(prompt, runtime, item)
        payload = json.loads(prompt)
        plan = payload.get('response_plan', {})
        if plan.get('action') not in {'reply', 'acknowledge', 'close', 'clarify'}:
            return bridge.augment_reply(prompt, runtime, item)
        if not getattr(capture, 'scene_prepared', False):
            capture.scene_prepared = True  # Retries never make another disambiguation call.
            understanding = payload.get('interaction_understanding', {})
            scene = labels(understanding)
            trace = {k: capture.scope[k] for k in ('scope', 'persona', 'member')}
            trace['turn_id'] = capture.base['turn_id']
            tentative = understanding.get('tentative_understanding', {})
            intent_answer = understanding.get('communication_intent', {})
            intent_confidence = intent_answer.get('confidence', 0) if isinstance(intent_answer, dict) else 0
            difficult = bool(understanding) and (
                scene['communication_intent'] == 'unclear' and type(intent_confidence) in (int, float) and 0 < intent_confidence <= 1
                or scene['expression_mode'] == 'unclear' and tentative.get('expression_mode', {}).get('choice')
                in {'joke', 'meme', 'callback', 'sarcasm', 'hyperbole', 'mixed'})
            method = getattr(source, 'disambiguate_reply_scene', None)
            if difficult and 'scene_disambiguation_v1' in caps and callable(method):
                started = time.monotonic()
                status = 'failed'
                task = None
                try:
                    conversation = payload.get('conversation', {})
                    bounded = clarification_input(host, capture, runtime, item, conversation)
                    if bounded is None:
                        status = 'skipped'
                    else:
                        task = asyncio.create_task(method(**trace, text=bounded[0], context=bounded[1]))
                        done, _ = await asyncio.wait({task}, timeout=TIMEOUT_SECONDS)
                        if task in done:
                            result = task.result()
                            resolved = labels(result)
                            scene = {k: resolved[k] if v == 'unclear' else v for k, v in scene.items()}
                            status = 'succeeded' if any(v != 'unclear' for v in resolved.values()) else 'failed'
                        else:
                            status = 'timeout'
                except TimeoutError:
                    status = 'timeout'
                except Exception:
                    status = 'failed'
                finally:
                    if task is not None and not task.done():
                        task.cancel()
                        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
                    if status != 'skipped':
                        report(source, {**trace, 'stage': 'disambiguation', 'status': status,
                                        'elapsed_ms': round((time.monotonic() - started) * 1000)})
            if bridge.provider(host) is not source or not capture.is_current():
                return bridge.augment_reply(prompt, runtime, item)
            packet = validate(source.get_scene_context(**capture.scope, scene=scene, query=capture.query), capture.scope)
            if packet['items']:
                capture.scene_packet = {**packet, 'trace': trace, 'scene': scene}
            report(source, {**trace, 'stage': 'selected', 'refs': [{k: r[k] for k in ('source_id', 'revision')} for r in packet['items']]})
    except Exception:
        # Optional scene work must not block the original reply.
        pass
    return bridge.augment_reply(prompt, runtime, item)
