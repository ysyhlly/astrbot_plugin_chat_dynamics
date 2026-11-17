"""Versioned Dynamics Learning boundary. No disk/network/model work on the reply path."""
from __future__ import annotations

import hashlib
import json
import math
import time
import weakref
from copy import deepcopy

from .catalog import find_companion

HOST_VERSION = "v1.18.3-reply-context.1"


def provider(host):
    try:
        current = find_companion(host.context, "dynamics_learning")
        caps = current.learning_capabilities() if current else {}
        return current if caps.get("schema_version") == 1 and caps.get("ready") is True else None
    except Exception:
        return None


def scope_for(host, turn, persona):
    # Stable across conversations but invalidated by any persona text change.
    fingerprint = hashlib.sha256((str(getattr(persona, "persona_id", "")) + "\0" +
                                  str(getattr(persona, "prompt", ""))).encode()).hexdigest()
    return dict(scope=turn.session_key, persona=fingerprint, member=turn.author,
                baseline=host._turn_config_identity(), host_version=HOST_VERSION)


def validate_selection(result,scope):
    """Shared production/preview wire validation; reject malformed or oversized material."""
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise ValueError("invalid contract")
    context = result.get("context", "")
    threshold = result.get("threshold")
    if not isinstance(context, str) or len(context) > 5000:
        raise ValueError("invalid context")
    if threshold is not None and (type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 100):
        raise ValueError("invalid threshold")
    versions = result.get("versions", [])
    if not isinstance(versions, list) or len(versions) > 100 or any(not isinstance(v, str) or len(v) > 128 for v in versions):
        raise ValueError("invalid versions")
    clean=dict(context=context, threshold=threshold, versions=list(versions))
    projection=result.get('reply_projection')
    if projection is not None:
        if (not isinstance(projection,dict) or type(projection.get('schema_version')) is not int or projection.get('schema_version')!=1 or
            any(projection.get(k)!=scope.get(k) for k in ('scope','persona','member')) or
            not isinstance(projection.get('items'),list) or len(projection['items'])>30 or
            len(json.dumps(projection,ensure_ascii=False,allow_nan=False))>8500):
            raise ValueError('invalid reply projection')
        clean['reply_projection']=deepcopy(projection)
    return clean


def read(current, scope, query='', character_scope=None):
    if current is None:
        return {"context": "", "threshold": None, "versions": []}
    try:
        capabilities=getattr(current,'learning_capabilities',lambda:{})().get('capabilities',[])
        extra={'character_scope':character_scope} if 'reply_projection_v1' in capabilities else {}
        result = current.get_learning_context(**scope, query=query, **extra)
        return validate_selection(result,scope)
    except Exception:
        return {"context": "", "threshold": None, "versions": []}


def selection(host, turn, persona):
    current = provider(host)
    return read(current, scope_for(host, turn, persona)) if current else read(None, {})


class TurnCapture:
    contract_version = 1

    def __init__(self, host, runtime, item, persona, current):
        self.host, self.runtime, self.source = weakref.ref(host), weakref.ref(runtime), weakref.ref(current)
        turn = item.context
        self.epoch = runtime.epoch
        self.scope = scope_for(host, turn, persona)
        self.query = turn.text
        bundle=getattr(item,'narrative_bundle',None)
        bundle_payload=bundle.wire() if bundle is not None else {}
        self.character_scope=bundle_payload.get('scope')
        self.selected = read(current, self.scope, self.query,self.character_scope)
        message = turn.messages[-1]
        self.base = dict(schema_version=1, **self.scope, turn_id=f"{runtime.epoch}:{message.message_id}",
                         message_id=message.message_id, text=turn.text[:4000],
                         baseline_threshold=float(host._runtime_config.reply_probability_threshold),
                         backend=host._runtime_config.decision_backend,
                         mandatory=bool(turn.mandatory_reply or item.private_direct), timestamp=time.time(),
                         policy={k:v for k,v in self.selected.items() if k!='reply_projection'}, context={"wake_kind": turn.wake_kind,
                            "message_ids": [m.message_id for m in turn.messages[-16:]],
                            "reply_to_ids": [
                                m.reply_to or getattr(getattr(m, "semantics", None), "quoted_message_id", "")
                                for m in turn.messages[-16:]
                            ]})
        self.final = None
        bundle = getattr(item,'narrative_bundle',None)
        if bundle is not None:
            payload = bundle_payload
            character_scope = payload.get('scope',{})
            if character_scope.get('umo') == turn.session_key:
                self.base['context']['character_scope'] = {k:character_scope.get(k,'') for k in ('platform','umo','persona_id','instance_id')}
                self.base['context']['persona_summary'] = str(payload.get('character',{}).get('core_prompt',''))[:2000]

    def emit(self, kind, node=None, reply=None):
        try:
            host, runtime, current = self.host(), self.runtime(), self.source()
            if host is None or runtime is None or current is None or getattr(host, "_shutting_down", False):
                return
            if provider(host) is not current or runtime.epoch != self.epoch:
                return
            event = {**self.base, "kind": kind}
            if self.final is not None:
                event["decision"] = self.final
            if node is not None:
                event["outcome"] = deepcopy(node.metadata.get("outcome", {}))
            if reply is not None:
                event["reply"] = reply
            current.ingest_dynamics_event(deepcopy(event))
        except Exception:
            # An optional learner cannot break admission or delivery.
            return

    def is_current(self):
        host, current = self.host(), self.source()
        expression = getattr(self, 'expression_packet', None)
        if expression and expression.get('items'):
            from .expression_style import current as expression_current
            if host is None or provider(host) is not current or not expression_current(current, expression):
                return False
        scene = getattr(self, 'scene_packet', None)
        if scene is not None:
            from .scene_learning import current as scene_current
            if host is None or provider(host) is not current or not scene_current(current, scene):
                return False
        # If no learning affected this turn, absence or a new learner cannot cancel it.
        if not self.selected["context"] and self.selected["threshold"] is None:
            return True
        if host is None or current is None or provider(host) is not current:
            return False
        latest = read(current,self.scope,self.query,self.character_scope)
        # New observations do not rewrite a frozen turn. Revoking any state that
        # this turn actually used still cancels it before delivery.
        return (all(version in latest['versions'] for version in self.selected['versions']) and
                (self.selected['threshold'] is None or latest['threshold'] == self.selected['threshold']))


def begin(host, runtime, item, persona):
    if item.shadow or not item.context.messages:
        return
    current = provider(host)
    if current is None:
        return
    try:
        capture = TurnCapture(host, runtime, item, persona, current)
        for message in item.context.messages:
            node = runtime.dag.get_node(message.message_id)
            if node is not None:
                # Transient attribute, deliberately outside persistent node.metadata.
                node._dynamics_learning_capture = capture
        capture.emit("observed")
    except Exception:
        return


def capture_for(runtime, item):
    if not item.context.messages:
        return None
    dag = getattr(runtime, "dag", None)
    getter = getattr(dag, "get_node", None)
    if not callable(getter):
        return None
    node = getter(item.context.messages[-1].message_id)
    return getattr(node, "_dynamics_learning_capture", None)


def current(runtime, item):
    capture = capture_for(runtime, item)
    return capture is None or capture.is_current()


def selected(runtime, item):
    capture = capture_for(runtime, item) if runtime is not None else None
    return deepcopy(capture.selected) if capture else read(None, {})


def augment_reply(prompt, runtime, item):
    selection=selected(runtime,item)
    context = selection["context"]
    projection=selection.get('reply_projection')
    if not context and projection is None:
        return prompt
    payload = json.loads(prompt)
    if projection is None:
        payload["reviewed_learning_context"] = context
    else:
        capture=capture_for(runtime,item)
        payload['dynamics_learning_reply']={**projection,'versions':selection['versions'],
            'baseline':capture.scope['baseline'],'host_version':capture.scope['host_version']}
        scene = getattr(capture, 'scene_packet', None)
        if scene is not None:
            payload['dynamics_learning_reply']['scene_selection'] = deepcopy(scene)
        expression = getattr(capture, 'expression_packet', None)
        if expression is not None:
            payload['dynamics_learning_reply']['expression_selection'] = deepcopy(expression)
    return json.dumps(payload, ensure_ascii=False)


def delivered(runtime, item, message_id, content):
    capture = capture_for(runtime, item)
    if capture is not None:
        capture.emit("outcome", reply={"message_id": str(message_id or ""), "text": content[:4000], "delivered": True})


def finalize(runtime, item, decision):
    capture = capture_for(runtime, item)
    if capture is None:
        return
    from ..conversation_intent import reply_interaction
    capture.base['context']['interaction'] = reply_interaction(item.context, runtime.jev_decision.get('interaction_frame'))
    # No provider prompts, response goals, keys, or unrelated historical messages.
    raw = getattr(runtime, "jev_decision", {}) if not item.private_direct else {}
    answers = {key: deepcopy(raw[key]) for key in ("join", "action", "state", "length", "completion", "recipient", "reason") if key in raw}
    capture.final = dict(action=decision.action, reason=decision.reason_code, length=decision.length,
                         state=decision.state, answers=answers)
    if len(json.dumps(capture.final, ensure_ascii=False)) <= 8000:
        capture.emit("decision")


def outcome(node):
    capture = getattr(node, "_dynamics_learning_capture", None)
    if capture is not None:
        capture.emit("outcome", node)


def affection(host, runtime, item):
    capture = capture_for(runtime,item)
    current = provider(host)
    getter = getattr(current,'get_affection_for_decision',None)
    if capture is None or not callable(getter):
        return None
    try:
        value = getter(**capture.scope)
        level = value.get('affection_level') if isinstance(value,dict) else None
        if (type(level) not in (int,float) or not math.isfinite(level) or level < 0 or
                value.get('scope') != capture.scope['scope'] or value.get('persona') != capture.scope['persona'] or
                value.get('user_id') != capture.scope['member']):
            return None
        return value
    except Exception:
        return None


def filter_legacy_social(host, scope, participants, rows):
    """Keep old style material; consume one authoritative slang/affection source."""
    current = provider(host)
    resolver = getattr(getattr(host, 'integrations', None), 'narrative_provider', None)
    if current is not None and callable(resolver) and resolver('self_learning') is current:
        return rows  # The native owner already produced these records.
    getter = getattr(current,'get_social_ownership',None)
    if not callable(getter):
        return rows
    try:
        ownership = getter(scope,participants)
        members = ownership.get('members',[])
        return [row for row in rows if not (
            isinstance(row,dict) and (
                str(row.get('source_id','')).startswith('affection:') and row.get('subject_id') in members or
                str(row.get('source_id','')).startswith('jargon:') and ownership.get('jargon') is True))]
    except Exception:
        return rows
