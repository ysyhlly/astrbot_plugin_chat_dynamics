"""One model decision per turn, outside session locks, with bounded admission."""

from __future__ import annotations
import json

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Sequence

from .agent_bridge import AstrBotAgentBridge, PersonaChanged
from .pacer import PacingShaper, is_rhythm_short_act, scale_delay
from .topic_identity import confirmed_topic_id
from .active_dialogue import dialogue_for_node
from .dialogue_context import capture_dialogue
from .platform_bridge import chain_plain_text
from .bot_identity import BotIdentityMatcher
from .group_context import select_group_events
from .deferred_reply_media import DeferredReplyMedia
from .message_semantics import describe_message
from .member_identity import quoted_identity
from .presence_policy import participation_policy
from .persona_trace import record_outcome, record_terminal_outcome, stage_trace, annotate_model_decision
from . import outcome_recorder as outcomes
from .turn_decision import (
    MessageSnapshot,
    TurnContext,
    TurnDecision,
    reply_prompt,
)
from .jev_decision import (
    MAX_TOTAL_REQUEST_CHARS,
    StateBudgetExceeded,
    bound_request_with_optional,
    build_questions,
    build_state,
    decision_from_answers,
    describe_answers,
    request_size,
)
from .topic_jev import align_topic_task, build_topic_task, apply_topic_answer, capture_topic_candidates
from .conversation_map import build_addressee_task, conversation_map, resolve_addressees, rule_addressees
from .conversation_intent import INTENT_CRITERIA, build_intent_task, semantic_coverage, reply_interaction
from .pipeline_telemetry import PipelineTelemetry
from .context_retrieval import clip_text, select_background, select_peer_continuation_candidate
from .routing_contract import commit_topic_evidence
from .narrative import DECISION_GUIDANCE, REPLY_GUIDANCE
from .reply_failure import ModelReplyFailure, unavailable_reply
from . import member_boundary
from .context_completion import complete_context
from .early_reply import final_fragments

logger = logging.getLogger("astrbot_plugin_chat_dynamics.persona_engine")


def _turn_log_id(runtime, item: "ModelTurn") -> str:
    """Turn identity for logs only: the stored turn id, else the newest message id."""
    messages = getattr(getattr(item, "context", None), "messages", ()) or ()
    if not messages:
        return ""
    message_id = str(getattr(messages[-1], "message_id", "") or "")
    node = runtime.dag.get_node(message_id) if getattr(runtime, "dag", None) is not None else None
    metadata = getattr(node, "metadata", None)
    turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
    return str(turn_id or message_id)


_REQUEST_SUPPLEMENT_WINDOW = 120.0
_WAKE_ADMITTED_REASONS = frozenset({"jev_wake_addressed", "jev_wake_low_confidence_acknowledged"})


@dataclass(frozen=True)
class ModelTurn:
    context: TurnContext
    events: tuple[Any, ...]
    observations: dict
    shadow: bool
    fallback: bool = False
    platform_message_ids: frozenset[str] = frozenset()
    outcome_nodes: tuple[Any, ...] = ()
    debounce_result: Any = None
    narrative_bundle: Any = None
    private_direct: bool = False
    submitted_monotonic: float = 0.0

    @property
    def context_node_ids(self) -> frozenset[str] | None:
        return self.context.visible_node_ids


def _node_metadata(node: Any) -> dict:
    metadata = getattr(node, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _dag_node(runtime: Any, message_id: str) -> Any:
    dag = getattr(runtime, "dag", None)
    getter = getattr(dag, "get_node", None)
    if not callable(getter) or not message_id:
        return None
    try:
        return getter(message_id)
    except Exception:
        return None


def _existing_reply_id(runtime: Any, node: Any, parsed: Any) -> str:
    reply_id = str(getattr(node, "reply_to_id", "") or getattr(parsed, "reply_to_id", "") or "")
    return reply_id if _dag_node(runtime, reply_id) is not None else ""


def _event_platform_ids(events: tuple[Any, ...]) -> frozenset[str]:
    ids = set()
    for event in events:
        message_id = getattr(event, "message_id", None)
        if not message_id:
            message_obj = getattr(event, "message_obj", None)
            message_id = getattr(message_obj, "message_id", None) if message_obj is not None else None
        if message_id:
            ids.add(str(message_id))
    return frozenset(ids)


def _platform_reply_id(runtime: Any, item: ModelTurn, message_id: str) -> str | None:
    """Return a platform ID only when it is backed by a real platform message."""
    if not message_id:
        return None
    current_ids = {message.message_id for message in item.context.messages}
    if message_id in current_ids:
        platform_ids = item.platform_message_ids | _event_platform_ids(item.events)
        return message_id if message_id in platform_ids else None
    node = _dag_node(runtime, message_id)
    if node is None:
        return None
    if _node_metadata(node).get("platform_message_id") is True:
        return message_id
    return None


def delivery_fragments(chains: Sequence[Any], text: str, pacer: Any, *, max_fragments: int = 10) -> list[Any]:
    """Queue tool/media chains, then text fragments that are not the same payload.

    A tool-direct chain that already equals the model transcript must not be
    sent again as a prose fragment.
    """
    fragments: list[Any] = []
    chain_texts: set[str] = set()
    for chain in chains or ():
        fragments.append(chain)
        plain = chain_plain_text(chain).strip()
        if plain:
            chain_texts.add(plain)
    parts = (
        []
        if (text or "").strip() in chain_texts
        else (pacer.persona_fragments(text or "") if pacer is not None else ([text] if text else []))
    )
    prose = []
    for part in parts:
        if not part:
            continue
        plain = part.strip() if isinstance(part, str) else chain_plain_text(part).strip()
        if plain and plain in chain_texts:
            continue
        # Repeated prose paragraphs can be deliberate; deduplicate only media/tool captions.
        prose.append(part)
    fragments.extend(PacingShaper.limit_fragments(prose, max_fragments))
    return fragments


def _routing_for_turn(runtime: Any, turn: TurnContext) -> dict:
    if not turn.messages:
        return {}
    node = _dag_node(runtime, turn.messages[-1].message_id)
    return dict(getattr(node, "metadata", {}).get("routing", {}) or {})


def _routing_other(runtime: Any, routing: dict) -> bool:
    ids = routing.get("addressee_ids") or []
    bot_id = str(getattr(runtime, "bot_id", "") or "")
    if routing.get("subject_is_bot") and not routing.get("bot_is_addressee"):
        return True
    evidence = routing.get("evidence", []) or []
    if "inferred_reply" in evidence and "explicit_mention" not in evidence and "explicit_reply" not in evidence:
        return False
    return bool(ids and bot_id not in ids and float(routing.get("addressee_confidence", 0)) >= 0.72)


def turn_is_addressed(runtime: Any, turn: TurnContext, now: float) -> bool:
    if turn.mandatory_reply or turn.soft_wake:
        return True
    routing = _routing_for_turn(runtime, turn)
    if _routing_other(runtime, routing):
        return False
    return bool(
        turn.explicit
        or float(routing.get("bot_addressee_confidence", 0)) >= 0.72
        or is_request_supplement(runtime, turn.author, now)
    )


def turn_is_continuation(runtime: Any, turn: TurnContext, now: float) -> bool:
    """Admit a live owned candidate to judgement without forcing delivery.

    Args:
        runtime: Current session, delivery anchors and stop revisions.
        turn: Immutable incoming turn and optional dialogue evidence.
        now: Current monotonic time used to expire the candidate.

    Returns:
        Whether the turn qualifies for continuation queue admission.
    """
    routing = _routing_for_turn(runtime, turn)
    if _routing_other(runtime, routing):
        return False

    turn_node = _dag_node(runtime, turn.messages[-1].message_id) if turn.messages else None
    dialogue = dialogue_for_node(runtime, turn_node, eligible_ids=turn.visible_node_ids, window=900)
    if dialogue is None:
        return False
    snapshot = turn.dialogue
    if (
        snapshot is not None
        and snapshot.continuation_kind
        and snapshot.user_id == turn.author == dialogue.user_id
        and snapshot.last_bot_message_id == dialogue.last_bot_message_id
        and 0 <= now - dialogue.updated_at <= (900 if dialogue.interaction_state == "focused" else 600)
    ):
        return True
    if dialogue.closed:
        return False
    bot_topic = dialogue.topic_id
    current_topic = confirmed_topic_id(turn_node)
    same_topic = bool(current_topic and bot_topic and current_topic == bot_topic)

    reply_to = getattr(turn, "reply_to", None) or getattr(getattr(turn, "node", None), "reply_to_id", None)
    inferred_parent = routing.get("parent_message_id", "")
    bot_ids = set(dialogue.bot_message_ids)
    if not reply_to and turn_node:
        reply_to = getattr(turn_node, "reply_to_id", None)

    parent_continuity = bool(
        reply_to in bot_ids or (inferred_parent in bot_ids and float(routing.get("parent_confidence", 0) or 0) >= 0.72)
    )

    coherent = same_topic or parent_continuity
    window = 120
    if same_topic and float(routing.get("topic_confidence", 0) or 0) >= 0.8:
        window = {"focused": 900, "supportive": 300}.get(dialogue.interaction_state, 120)
    return bool(coherent and dialogue.user_id == turn.author and 0 <= now - dialogue.updated_at < window)


def is_request_supplement(
    runtime: Any,
    user_id: str,
    now: float,
    *,
    window: float = _REQUEST_SUPPLEMENT_WINDOW,
) -> bool:
    """True when the same user is adding to a recent explicit request, not opening ambient.

    A follow-up without @ after ``@bot 帮我写邮件`` is still that request. A newcomer's
    standalone chatter is not.
    """
    uid = str(user_id or "")
    if not uid:
        return False
    dag = getattr(runtime, "dag", None)
    getter = getattr(dag, "get_recent_nodes", None) if dag is not None else None
    if not callable(getter):
        return False
    try:
        recent = getter(limit=0)
    except Exception:
        return False
    bot_id = str(getattr(runtime, "bot_id", "") or "")
    current = next((n for n in sorted(recent, key=lambda n: n.timestamp, reverse=True) if str(n.user_id) == uid), None)
    current_routing = getattr(current, "metadata", {}).get("routing", {})
    if _routing_other(runtime, current_routing):
        return False
    for node in recent:
        if (
            confirmed_topic_id(current)
            and confirmed_topic_id(node)
            and confirmed_topic_id(current) != confirmed_topic_id(node)
        ):
            continue
        if str(getattr(node, "user_id", "") or "") != uid:
            continue
        if node.metadata.get("dialogue_stop_revision", 0) != getattr(runtime, "topic_stop_revisions", {}).get(uid, 0):
            continue
        try:
            ts = float(getattr(node, "timestamp", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and now - ts > window:
            continue
        metadata = getattr(node, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("is_wake"):
            return True
        mentions = list(getattr(node, "mentioned_users", None) or [])
        if bot_id and bot_id in mentions:
            return True
    return False


def _identity_failure(runtime: Any, detail: str, **extra: Any) -> None:
    diagnostic = {"reason_code": "turn_identity_invalid", "detail": detail, **extra}
    try:
        runtime.model_diagnostic = diagnostic
    except Exception:
        pass
    logger.warning("Persona turn identity mapping failed: %s", detail)


def _quoted_author_name(node, dag) -> str:
    return str((quoted_identity(node, dag) or {}).get("display_name") or "")


def _snapshot_background(runtime, nodes, *, eligible_ids=None, retained=()) -> tuple[MessageSnapshot, ...]:
    dag = runtime.dag
    original = {message.message_id: message for message in retained}
    selections = {
        selection.node.msg_id: selection
        for selection in select_background(
            dag,
            nodes,
            bot_id=str(runtime.bot_id or ""),
            eligible_ids=eligible_ids,
            delivered_ids=frozenset(getattr(runtime, "sent_id_set", ()) or ()),
        )
    }
    order = {n.msg_id: i for i, n in enumerate(dag.get_recent_nodes(limit=0))}
    for index, message in enumerate(retained):
        order.setdefault(message.message_id, index - len(retained))
    proofs = {}
    for mid in dict.fromkeys((*original, *selections)):
        evidence = original[mid].context_evidence if mid in original else ()
        if mid in selections:
            evidence = (*evidence, *selections[mid].context_evidence)
        proofs[mid] = tuple(sorted(dict.fromkeys(evidence), key=lambda p: (p.priority, -p.confidence)))[:3]

    def priority(mid):
        timestamp = original[mid].timestamp if mid in original else selections[mid].node.timestamp
        return min((p.priority for p in proofs[mid]), default=7), -(timestamp or 0), -order[mid]

    background = []
    supplemented = any(p.relation == "supplemental_context" for rows in proofs.values() for p in rows)
    budget = 9000 if supplemented else 6000
    for mid in sorted(proofs, key=priority):
        if budget <= 0 or len(background) >= (24 if supplemented else 15):
            break
        limit = 400 if any(p.relation in {"poke_sender", "poke_reply"} for p in proofs[mid]) else 1200
        if mid in original:
            text = clip_text(original[mid].text, min(limit, budget))
            background.append(replace(original[mid], text=text, context_evidence=proofs[mid]))
        else:
            n = selections[mid].node
            text = clip_text(n.text, min(limit, budget))
            background.append(
                MessageSnapshot(
                    n.msg_id,
                    n.user_id,
                    text,
                    n.reply_to_id or "",
                    semantics=describe_message(n, dag, runtime.bot_id),
                    source_text=n.text,
                    timestamp=n.timestamp,
                    mentioned_users=tuple(n.metadata.get("actual_mentions", n.mentioned_users)),
                    author_name=str(n.metadata.get("sender_name") or ""),
                    platform=str(n.metadata.get("sender_platform") or ""),
                    quoted_author_name=_quoted_author_name(n, dag),
                    context_evidence=proofs[mid],
                )
            )
        budget -= len(text)
    background.sort(key=lambda m: (m.timestamp, order.get(m.message_id, 0)))
    return tuple(background)


def _refresh_topic_semantics(runtime, message, node):
    if not _node_metadata(node).get("routing", {}).get("semantic_topic_decision"):
        return message.semantics
    latest = describe_message(node, runtime.dag, runtime.bot_id)
    original = message.semantics
    if original is None:
        return latest
    code = next(
        (code for code in reversed(latest.routing_evidence) if code in {"topic_jev_new", "topic_jev_match"}), None
    )
    evidence = commit_topic_evidence(
        {"evidence": original.routing_evidence, "topic_ambiguous": latest.topic_ambiguous}, code
    )
    # Jev updates topic membership, not the original sender/mention/quote facts.
    return replace(
        original,
        topic_id=latest.topic_id,
        topic_confidence=latest.topic_confidence,
        topic_ambiguous=latest.topic_ambiguous,
        routing_ambiguous=original.addressee_ambiguous or latest.topic_ambiguous,
        routing_evidence=tuple(evidence),
    )


def refresh_topic_context(runtime, item: ModelTurn, *, boundary_host=None) -> ModelTurn:
    """Refresh confirmed topic evidence under state_lock, using the original watermark."""
    turn = item.context
    if item.context_node_ids is None:
        return item
    nodes = tuple(_dag_node(runtime, message.message_id) for message in turn.messages)
    if any(node is None for node in nodes):
        return item
    if item.outcome_nodes and any(node is not original for node, original in zip(nodes, item.outcome_nodes)):
        return item
    messages = tuple(
        replace(message, semantics=_refresh_topic_semantics(runtime, message, node))
        for node, message in zip(nodes, turn.messages)
    )
    if messages == turn.messages:
        return item
    confirmed_topics = {
        message.semantics.topic_id
        for message in messages
        if message.semantics
        and message.semantics.topic_id
        and not message.semantics.topic_ambiguous
        and message.semantics.topic_confidence >= 0.8
    }
    retained = tuple(
        message
        for message in turn.background
        if any(proof.priority == 0 or proof.relation == "supplemental_context" for proof in message.context_evidence)
        or (
            message.semantics
            and message.semantics.topic_id in confirmed_topics
            and any(proof.relation == "same_topic" for proof in message.context_evidence)
        )
    )
    context = replace(
        turn,
        messages=messages,
        background=_snapshot_background(runtime, nodes, eligible_ids=item.context_node_ids, retained=retained),
    )
    # Newly retrieved topic sources must pass the same member boundary as the
    # initial snapshot; a routing refresh cannot resurrect shielded material.
    frames = [
        {
            "frame_id": message.message_id,
            "author_uid": message.author,
            "source_uids": [uid for uid in (message.author, getattr(message.semantics, "quoted_author_id", "")) if uid],
            "provenance_complete": bool(message.author),
            "kind": "event",
            "text": message.text,
        }
        for message in context.background
    ]
    filtered, state = member_boundary.filter_frames(
        boundary_host, item.events[-1] if item.events else None, frames, purpose="role_input"
    )
    if not state.available and (member_boundary.fail_closed(state) or state.service is not None):
        filtered = []
    allowed = {frame.get("frame_id") for frame in filtered if isinstance(frame, dict)}
    context = replace(
        context, background=tuple(message for message in context.background if message.message_id in allowed)
    )
    # Reclassifying the topic may select another old exchange, but only sources
    # visible to the original turn can enter its reply context.
    dialogue = capture_dialogue(
        runtime, nodes[-1], item.context_node_ids, current_ids=tuple(m.message_id for m in messages)
    )
    if turn.dialogue is not None:
        if dialogue is not None and dialogue.last_bot_message_id == turn.dialogue.last_bot_message_id:
            dialogue = replace(turn.dialogue, topic_id=dialogue.topic_id)
        elif dialogue is None and (not confirmed_topics or turn.dialogue.topic_id in confirmed_topics):
            dialogue = turn.dialogue
    context = replace(context, dialogue=dialogue)
    return replace(item, context=context)


def snapshot_turn(
    runtime: Any,
    result: Any,
    canonical_ids: Sequence[str],
    parsed_events: Sequence[Any],
    explicit: bool,
    observations: dict,
    shadow: bool,
    *,
    wake_kind: str = "legacy",
    boundary_host: Any = None,
) -> ModelTurn | None:
    """Build a model turn from the caller's ordered, canonical DAG IDs.

    The caller owns fragment ordering.  This function only dereferences the
    supplied IDs and refuses to construct a partial context when the mapping
    is incomplete or stale. History selection uses platform ancestry, accepted
    inference, committed topics and bounded recipient/speaker supplements.
    """
    dag = getattr(runtime, "dag", None)
    if dag is None:
        _identity_failure(runtime, "dag_missing")
        return None

    ids = tuple(str(message_id or "") for message_id in (canonical_ids or ()))
    events = tuple(parsed_events or ())
    raw_events = tuple(getattr(result, "raw_events", ()) or ())
    if not ids:
        _identity_failure(runtime, "mapping_missing")
        return None
    if len(ids) != len(events) or len(ids) != len(raw_events):
        _identity_failure(
            runtime,
            "mapping_length_mismatch",
            canonical_count=len(ids),
            parsed_count=len(events),
            raw_count=len(raw_events),
        )
        return None
    if len(set(ids)) != len(ids):
        _identity_failure(runtime, "mapping_duplicate")
        return None

    resolved_nodes = []
    for index, message_id in enumerate(ids):
        if not message_id:
            _identity_failure(runtime, "mapping_missing", index=index)
            return None
        resolved = _dag_node(runtime, message_id)
        if resolved is None:
            _identity_failure(runtime, "node_missing", index=index)
            return None
        resolved_nodes.append(resolved)

    messages = tuple(
        MessageSnapshot(
            resolved.msg_id,
            str(getattr(resolved, "user_id", "") or getattr(parsed, "sender_id", "") or getattr(result, "user_id", "")),
            "",
            _existing_reply_id(runtime, resolved, parsed),
            tuple(getattr(parsed, "media_component_types", ()) or ()),
            describe_message(resolved, dag, runtime.bot_id),
            source_text=resolved.text,
            timestamp=resolved.timestamp,
            mentioned_users=tuple(resolved.metadata.get("actual_mentions", resolved.mentioned_users)),
            author_name=str(resolved.metadata.get("sender_name") or ""),
            platform=str(resolved.metadata.get("sender_platform") or ""),
            quoted_author_name=_quoted_author_name(resolved, dag),
        )
        for parsed, resolved in zip(events, resolved_nodes)
    )
    ordered = dag.get_recent_nodes(limit=0)
    current_ids = set(ids)
    boundary = max(i for i, n in enumerate(ordered) if n.msg_id in current_ids)
    cutoff = max(n.timestamp for n in resolved_nodes)
    context_node_ids = frozenset(n.msg_id for i, n in enumerate(ordered) if i <= boundary and n.timestamp <= cutoff)
    background = _snapshot_background(runtime, resolved_nodes, eligible_ids=context_node_ids)
    dialogue = capture_dialogue(runtime, resolved_nodes[-1], context_node_ids, current_ids=ids)
    peer_continuation = select_peer_continuation_candidate(
        dag,
        resolved_nodes,
        bot_id=str(runtime.bot_id or ""),
        eligible_ids=context_node_ids,
        cutoff=cutoff,
        window=_REQUEST_SUPPLEMENT_WINDOW,
        explicit=explicit,
        wake_kind=wake_kind,
        dialogue=dialogue,
    )
    boundary_event = raw_events[-1] if raw_events else getattr(result, "last_event", None)
    notice_members = {
        uid
        for message in messages
        for uid in (message.author, *message.mentioned_users, getattr(message.semantics, "quoted_author_id", ""))
        if uid
    }
    group_events = select_group_events(runtime, cutoff, member_ids=notice_members)
    boundary_frames = []
    for notice in group_events:
        boundary_frames.append(
            {
                "frame_id": notice.event_id,
                "author_uid": notice.member_id,
                "source_uids": [uid for uid in (notice.member_id, notice.operator_id) if uid],
                "provenance_complete": True,
                "kind": "event",
                "text": notice.member_name,
            }
        )
    for message in (*messages, *background):
        boundary_frames.append(
            {
                "frame_id": message.message_id,
                "author_uid": message.author,
                "source_uids": [message.author] if message.author else [],
                "provenance_complete": bool(message.author),
                "kind": "event",
                "text": message.source_text if message.source_text is not None else message.text,
            }
        )
        quoted_uid = str(getattr(getattr(message, "semantics", None), "quoted_author_id", "") or "")
        if message.reply_to or quoted_uid:
            boundary_frames.append(
                {
                    "frame_id": message.message_id + ":quote",
                    "author_uid": quoted_uid,
                    "source_uids": [quoted_uid] if quoted_uid else [],
                    "provenance_complete": bool(quoted_uid),
                    "kind": "quoted_source",
                    "text": "",
                }
            )
    peer_frame_id = ""
    if peer_continuation is not None:
        peer_frame_id = "peer_continuation:" + peer_continuation.source_message_id
        boundary_frames.append(
            {
                "frame_id": peer_frame_id,
                "author_uid": peer_continuation.source_author,
                "source_uids": [peer_continuation.source_author, peer_continuation.candidate_recipient_id],
                "provenance_complete": True,
                "kind": "event",
                "text": peer_continuation.source_excerpt,
            }
        )
    filtered_frames, boundary_state = member_boundary.filter_frames(
        boundary_host, boundary_event, boundary_frames, purpose="role_input"
    )
    if not boundary_state.available and (
        member_boundary.fail_closed(boundary_state) or boundary_state.service is not None
    ):
        _identity_failure(runtime, "boundary_filter_unavailable")
        return None
    allowed_frames = {str(frame.get("frame_id")) for frame in filtered_frames if isinstance(frame, dict)}
    group_events = tuple(notice for notice in group_events if notice.event_id in allowed_frames)
    if peer_frame_id:
        allowed_peer_frame = next(
            (
                frame
                for frame in filtered_frames
                if isinstance(frame, dict) and str(frame.get("frame_id")) == peer_frame_id
            ),
            None,
        )
        if allowed_peer_frame is None:
            peer_continuation = None
        else:
            peer_continuation = replace(
                peer_continuation,
                source_excerpt=clip_text(str(allowed_peer_frame.get("text") or ""), 160),
            )
    denied_quotes = {
        message.message_id
        for message in (*messages, *background)
        if (message.reply_to or getattr(getattr(message, "semantics", None), "quoted_author_id", ""))
        and message.message_id + ":quote" not in allowed_frames
    }
    if denied_quotes:
        for parsed, raw in zip(events, raw_events):
            if str(getattr(parsed, "message_id", "") or "") in denied_quotes:
                try:
                    setattr(raw, "_chat_dynamics_strip_quote_media", True)
                except Exception:
                    pass
    messages = tuple(
        replace(message, reply_to="", attachments=(), semantics=None)
        if message.message_id in denied_quotes
        else message
        for message in messages
        if message.message_id in allowed_frames
    )
    background = tuple(
        replace(message, reply_to="", attachments=(), semantics=None)
        if message.message_id in denied_quotes
        else message
        for message in background
        if message.message_id in allowed_frames
    )
    turn = TurnContext(
        runtime.session_key,
        result.user_id,
        result.consolidated_text[:8000],
        messages,
        tuple(background),
        runtime.epoch,
        getattr(result.last_event, "_chat_dynamics_user_revision", runtime.user_revisions.get(result.user_id, 0)),
        result.start_time,
        explicit,
        bool(result.metadata.get("truncated")) or len(result.consolidated_text) > 8000,
        source_text=result.consolidated_text,
        source_truncated=bool(result.metadata.get("truncated")),
        wake_kind=wake_kind,
        bot_id=str(runtime.bot_id or ""),
        visible_node_ids=context_node_ids,
        peer_continuation=peer_continuation,
    )
    from .nearby_context import snapshot_nearby, filter_nearby

    nearby = snapshot_nearby(
        runtime, resolved_nodes, context_node_ids, excluded_ids={message.message_id for message in background}
    )
    turn = replace(
        turn,
        snapshot_at=cutoff,
        dialogue=dialogue,
        group_events=group_events,
        nearby_context=filter_nearby(nearby, boundary_host, boundary_event),
    )
    if getattr(runtime, "routing_state", None) is not None:
        turn = replace(turn, topic_candidates=capture_topic_candidates(runtime, turn))
    platform_ids = frozenset(
        str(getattr(parsed, "message_id", "") or "")
        for parsed, resolved in zip(events, resolved_nodes)
        if getattr(parsed, "message_id", "") and resolved.msg_id == getattr(parsed, "message_id", "")
    )
    observations = {
        **observations,
        "ambient_openings_used": sum(
            0 <= cutoff - timestamp < 60 for timestamp in getattr(runtime, "ambient_openings", ())
        ),
    }
    return ModelTurn(
        turn,
        tuple(result.raw_events),
        observations,
        shadow,
        platform_message_ids=platform_ids,
        outcome_nodes=tuple(_dag_node(runtime, message.message_id) for message in turn.messages),
        debounce_result=result if result.metadata.get("semantic") else None,
    )


class PersonaEngine:
    _decision_compaction_release = True
    _decision_nearby_context_release = True
    _recipient_guard_release = True

    def __init__(self, plugin):
        self.plugin = plugin
        self.bridge = AstrBotAgentBridge(plugin.context)
        self.slots = asyncio.Semaphore(4)
        self._projection_cache = OrderedDict()
        self._projection_tasks = {}
        self.telemetry = PipelineTelemetry()

    def _invalid(self, runtime, item: ModelTurn, reason: str) -> bool:
        """Refuse a turn and leave one WARNING with ids only (never message text)."""
        record_terminal_outcome(runtime, item, outcomes.mark_suppressed, reason, stage="generation")
        event = item.events[-1] if item.events else None
        if event is not None and not getattr(event, "_cd_invalid_warned_v1", False):
            try:
                setattr(event, "_cd_invalid_warned_v1", True)
            except Exception:
                pass
            logger.warning(
                "[ChatDynamics] turn dropped session=%s turn=%s reason=%s",
                runtime.session_key,
                _turn_log_id(runtime, item),
                reason,
            )
        return False

    def _valid_prefix(self, runtime, item: ModelTurn):
        """事件/边界前置检查。

        返回值 True/False 表示已可判定，None 表示需要继续做证据校验与尾部判定。
        """
        p, turn = self.plugin, item.context
        if item.events:
            event = item.events[-1]
            if getattr(event, "_chat_dynamics_boundary_failed_closed", False):
                return self._invalid(runtime, item, "boundary_failed_closed")
            if getattr(event, "_chat_dynamics_evidence_failed_closed", False):
                return self._invalid(runtime, item, "evidence_failed_closed")
            policy, token, state = member_boundary.peek(p, event, turn.author)
            if not state.available and member_boundary.fail_closed(state):
                return self._invalid(runtime, item, "boundary_service_unavailable")
            if member_boundary.blocked(policy):
                return self._invalid(runtime, item, "boundary_policy_blocked")
            captured = getattr(event, "_chat_dynamics_boundary_token", None)
            if captured is not None and token != captured:
                return self._invalid(runtime, item, "boundary_policy_changed")
        return None

    def _valid_suffix(self, runtime, item: ModelTurn) -> bool:
        """尾部判定（会话/epoch/冷却等纯本地状态检查）。"""
        p, turn = self.plugin, item.context
        scope_enabled = (
            p.is_runtime_scope_enabled(runtime)
            if callable(getattr(p, "is_runtime_scope_enabled", None))
            else p.is_group_takeover_enabled(runtime.group_id)
        )
        valid = (
            not p._shutting_down
            and p._persona_mode()
            and p.shadow_mode == item.shadow
            and scope_enabled
            and runtime.epoch == turn.epoch
            and runtime.user_revisions.get(turn.author, 0) == turn.revision
            and (item.debounce_result is None or p.debounce.is_result_current(item.debounce_result))
            and (
                item.private_direct
                or turn.mandatory_reply
                or turn.soft_wake
                or not p.arbiter.is_in_deep_cooling(runtime.session_key, current_time=p.time_service.time())
                or turn_is_continuation(runtime, turn, p.time_service.time())
            )
        )
        if not valid:
            reason = (
                "plugin_shutdown"
                if p._shutting_down
                else "session_reset"
                if runtime.epoch != turn.epoch
                else "member_revision_changed"
                if runtime.user_revisions.get(turn.author, 0) != turn.revision
                else "turn_superseded"
                if item.debounce_result is not None and not p.debounce.is_result_current(item.debounce_result)
                else "runtime_policy_changed"
            )
            return self._invalid(runtime, item, reason)
        return True

    def _evidence_view_current(self, item: ModelTurn) -> bool:
        """同步证据校验（valid() 原行为，逐字保留）。"""
        bundle = getattr(item, "narrative_bundle", None)
        if bundle is None:
            return True
        coordinator = getattr(self.plugin, "narrative", None)
        check = getattr(coordinator, "evidence_current", None)
        try:
            return bool(callable(check) and check(bundle.wire()))
        except Exception:
            return False

    async def _evidence_view_current_async(self, item: ModelTurn) -> bool:
        """异步证据校验：跨插件读库放到事件循环之外，判据与同步版一致。"""
        bundle = getattr(item, "narrative_bundle", None)
        if bundle is None:
            return True
        coordinator = getattr(self.plugin, "narrative", None)
        check = getattr(coordinator, "evidence_current_async", None)
        if not callable(check):
            # coordinator 无异步入口时退回同步实现，行为等价。
            return self._evidence_view_current(item)
        try:
            return bool(await check(bundle.wire()))
        except Exception:
            return False

    def valid(self, runtime, item: ModelTurn) -> bool:
        verdict = self._valid_prefix(runtime, item)
        if verdict is not None:
            return verdict
        if not self._evidence_view_current(item):
            return self._invalid(runtime, item, "evidence_revision_changed")
        return self._valid_suffix(runtime, item)

    async def valid_async(self, runtime, item: ModelTurn) -> bool:
        """与 valid() 完全同判据，但证据读库不阻塞事件循环。

        供 async 调用点使用；valid() 保持同步与逐字原行为，供少数同步调用点
        （_finish_generation_failure / has_pending_explicit_request）继续使用。
        """
        verdict = self._valid_prefix(runtime, item)
        if verdict is not None:
            return verdict
        if not await self._evidence_view_current_async(item):
            return self._invalid(runtime, item, "evidence_revision_changed")
        return self._valid_suffix(runtime, item)

    def _finish_generation_failure(
        self, runtime, item: ModelTurn, reason: str, *, error_type: str = "", suppressed: bool = False
    ) -> None:
        """Give a turn that had started generating a terminal fact and a log line.

        The admitted turn is a fact even when the lifecycle moved on: the normal
        `record_outcome` guard refuses invalidated turns, which is exactly how
        the audit found replies stuck `in_flight` forever with no WARNING.
        """
        valid = self.valid(runtime, item)
        if valid:
            if suppressed:
                record_outcome(runtime, item, outcomes.mark_suppressed, reason, stage="generation")
            else:
                record_outcome(runtime, item, outcomes.mark_generation_failed, reason)
        else:
            record_terminal_outcome(
                runtime, item, outcomes.mark_suppressed, "lifecycle_invalidated:" + reason, stage="generation"
            )
        logger.warning(
            "[ChatDynamics] reply generation failed session=%s turn=%s reason=%s valid=%s%s",
            runtime.session_key,
            _turn_log_id(runtime, item),
            reason,
            valid,
            (" error=" + error_type) if error_type else "",
        )

    def _sweep_in_flight(self, runtime, item: ModelTurn) -> None:
        """No admitted turn may leave its worker with an `in_flight` fact."""
        if item.shadow:
            return
        reason = "generation_unclosed"
        if record_terminal_outcome(runtime, item, outcomes.mark_generation_failed, reason, only_in_flight=True):
            logger.warning(
                "[ChatDynamics] reply generation left in flight session=%s turn=%s reason=%s",
                runtime.session_key,
                _turn_log_id(runtime, item),
                reason,
            )

    async def submit(self, runtime, item: ModelTurn) -> None:
        item = replace(item, submitted_monotonic=time.monotonic())
        if not await self.valid_async(runtime, item):
            self.plugin.debounce.finish_result(item.debounce_result)
            return
        now = self.plugin.time_service.time()
        addressed = (
            item.private_direct
            or turn_is_addressed(runtime, item.context, now)
            or turn_is_continuation(runtime, item.context, now)
        )
        if runtime.model_admission.locked() and not addressed:
            self.diagnostic(runtime, "overload_ambient")
            self.plugin.debounce.finish_result(item.debounce_result)
            return
        fallback = runtime.model_admission.locked()
        if fallback and item.context.explicit:
            # An already queued wake keeps its own request identity. Chatter
            # while it waits for capacity must not replace that request.
            self.plugin.debounce.finish_result(item.debounce_result)
        # Backpressure is outside state_lock. No dropped explicit request and no unbounded model tasks.
        token = object()
        runtime.model_waiting[token] = item
        admitted = False
        queued = False
        try:
            await runtime.model_admission.acquire()
            admitted = True
            async with runtime.state_lock:
                if not await self.valid_async(runtime, item):
                    return
                runtime.model_queue.append(replace(item, fallback=fallback))
                queued = True
                if runtime.generation_task is None or runtime.generation_task.done():
                    runtime.generation_task = self.plugin._create_background_task(self.run(runtime))
        finally:
            runtime.model_waiting.pop(token, None)
            if admitted and not queued:
                runtime.model_admission.release()
            if not queued:
                self.plugin.debounce.finish_result(item.debounce_result)

    def has_pending_explicit_request(self, runtime, user_id: str, *, mandatory_only: bool = False) -> bool:
        """Ordinary chatter must not cancel a pending, unanswered wake request.

        A pending @ also survives a newer wake. Stop/reset still advance the
        owner revision. This check grants no wake status to the incoming message.
        """
        pending = (runtime.active_model_turn, *runtime.model_queue, *runtime.model_waiting.values())
        return any(
            item is not None
            and item.context.author == user_id
            and item.context.explicit
            and self.valid(runtime, item)
            and (not mandatory_only or item.context.mandatory_reply)
            for item in pending
        )

    def diagnostic(self, runtime, code: str, **extra) -> None:
        runtime.model_diagnostic = {"reason_code": code, **extra}
        self.plugin._mark_panel_runtime_dirty()

    async def _project_persona(self, persona, turn):
        """Return only a completed projection; warm the cache without awaiting I/O."""
        from . import persona_axes

        p = self.plugin
        fingerprint = persona.fingerprint
        if not fingerprint or getattr(p, "_shutting_down", False):
            return None
        hit = self._projection_cache.get(fingerprint)
        if hit is not None:
            axes, expires = hit
            if time.monotonic() < expires:
                self._projection_cache.move_to_end(fingerprint)
                return axes
            self._projection_cache.pop(fingerprint, None)
        if (
            fingerprint in self._projection_tasks
            or len(self._projection_tasks) >= 4
            or not callable(getattr(p, "_create_background_task", None))
            or not callable(getattr(p, "get_kv_data", None))
        ):
            return None

        async def warm():
            axes = None
            try:
                axes = await asyncio.wait_for(
                    persona_axes.load_cached(p, fingerprint), timeout=p._runtime_config.decision_timeout
                )
            except Exception:
                pass
            else:
                if getattr(p, "_shutting_down", False):
                    return
            # Failed or missing projections retry after a short cooldown.
            self._projection_cache[fingerprint] = (axes, time.monotonic() + (3600 if axes else 60))
            while len(self._projection_cache) > persona_axes.MAX_CACHE_ENTRIES:
                self._projection_cache.popitem(last=False)

        task = p._create_background_task(warm())
        self._projection_tasks[fingerprint] = task

        def finished(done):
            if self._projection_tasks.get(fingerprint) is done:
                self._projection_tasks.pop(fingerprint, None)

        task.add_done_callback(finished)
        return None

    async def decide(self, item: ModelTurn, persona, state: str, *, runtime: Any = None) -> TurnDecision:
        p, turn = self.plugin, item.context
        if runtime is not None:
            runtime.jev_decision = {}
            runtime.jev_addressees = {}
        if (
            turn.mandatory_reply
            and item.narrative_bundle is None
            and not (getattr(p._runtime_config, "context_completion_enabled", False) and not item.shadow)
        ):
            return TurnDecision.fallback(turn, "at_mandatory")
        if (
            item.fallback
            and not turn.soft_wake
            and not (runtime is not None and turn_is_continuation(runtime, turn, p.time_service.time()))
        ):
            return TurnDecision.fallback(turn, "queue_overload")
        try:
            return await asyncio.wait_for(
                self.telemetry.run("decision", turn, self._decide_layer(item, persona, state, runtime=runtime)),
                timeout=p._runtime_config.decision_timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return TurnDecision.fallback(turn, "decision_timeout")
        except Exception:
            return TurnDecision.fallback(turn, "decision_invalid_or_failed")

    async def _decide_layer(
        self,
        item: ModelTurn,
        persona,
        state: str,
        *,
        runtime: Any = None,
    ) -> TurnDecision:
        """One bounded native Jev call, with the local conservative fallback."""
        p, turn = self.plugin, item.context
        prefix = "jev_"
        rule_routing = bool(
            getattr(p._runtime_config, "rule_topic_routing_enabled", False)
            and p._runtime_config.conversation_router_enabled
            and not item.private_direct
        )
        client = getattr(p, "jev", None)
        if client is None:
            return TurnDecision.fallback(turn, "jev_unavailable")
        timeout = p._runtime_config.jev_timeout
        floor = p._runtime_config.jev_min_confidence
        observations = dict(item.observations)
        context_options = {
            "context_completion": bool(
                getattr(p._runtime_config, "context_completion_enabled", False)
                and not item.shadow
                and not item.private_direct
                and not (item.narrative_bundle and item.narrative_bundle.wire().get("internal"))
            ),
            "context_completed": bool(observations.get("context_completion", {}).get("attempted")),
        }
        if turn.dialogue is not None:
            state = turn.dialogue.interaction_state
        if item.debounce_result is not None:
            now = p.time_service.time()
            flushed_at = item.debounce_result.metadata.get("flushed_at", now)
            observations.update(
                utterance_pause_seconds=max(0.0, flushed_at - item.debounce_result.end_time),
                utterance_age_seconds=max(0.0, flushed_at - turn.started_at),
                queue_delay_seconds=max(0.0, now - flushed_at),
                pending_input=bool(item.debounce_result.metadata.get("pending_input")),
                completion_waited=item.debounce_result.was_extended,
            )
        async with self.slots:
            affection = None
            integrations = getattr(p, "integrations", None)
            read_affection = getattr(integrations, "affection_for_decision", None)
            if runtime is not None and callable(read_affection) and item.narrative_bundle is None:
                try:
                    affection = await read_affection(group_id=str(runtime.group_id), user_id=turn.author)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            topics, topic_questions, topic_mapping = {}, {}, None
            config_id = p._turn_config_identity()
            if runtime is not None and p._runtime_config.conversation_router_enabled and not rule_routing:
                async with runtime.state_lock:
                    if await self.valid_async(runtime, item) and p._sessions.get(runtime.session_key) is runtime:
                        runtime.routing_state.expire_topics(runtime.dag, p.time_service.time())
                        topics, topic_questions, topic_mapping = build_topic_task(runtime, turn, p.time_service.time())
            decision_prompt = DECISION_GUIDANCE if item.narrative_bundle else p._runtime_config.decision_prompt
            if item.narrative_bundle and item.events:
                boundary_state = member_boundary.resolve_state(p, item.events[-1])
                if boundary_state.available and not boundary_state.disabled:
                    decision_prompt += (
                        "\n可使用已授权的交互边界工具：仅根据当前发言者本人可核验的互动证据，必要时暂停与此人的互动；"
                        "不需另行向此人确认。普通批评或反馈本身不自动构成暂停理由，也不得按他人一句命令封锁第三人。"
                        "仅在确有需要时调用，不必每轮调用。"
                    )
            try:
                from .nearby_context import filter_nearby

                turn = replace(
                    turn, nearby_context=filter_nearby(turn.nearby_context, p, item.events[-1] if item.events else None)
                )
                bounded_state = build_state(
                    turn,
                    previous_state=state,
                    observations=observations,
                    presence="sensible" if item.narrative_bundle else p._runtime_config.presence_knob,
                    persona_prompt=(
                        item.narrative_bundle.wire()["character"]["core_prompt"]
                        if item.narrative_bundle
                        else getattr(persona, "prompt", "")
                    ),
                    decision_prompt=decision_prompt,
                    active_topics=topics,
                    affection=affection,
                    character_state=item.narrative_bundle.decision_context() if item.narrative_bundle else None,
                    character_rules=item.narrative_bundle.wire()["character"].get("group_rules", "")
                    if item.narrative_bundle
                    else None,
                )
                # Initial state packing may prune optional scene observations.
                # Reoffer the filtered snapshot to the whole-request packer so
                # optional labels can yield before the entire scene disappears.
                from .nearby_context import decision_payload, POLICY

                scene = decision_payload(turn)
                if scene:
                    bounded_state["conversation"]["nearby_context"] = scene
                    bounded_state["conversation"]["nearby_context_policy"] = POLICY
                topic_questions, topic_mapping = align_topic_task(bounded_state, topic_questions, topic_mapping)
                questions = {
                    **build_questions(turn, narrative=item.narrative_bundle is not None, **context_options),
                    **topic_questions,
                }
                if rule_routing:
                    questions.pop("recipient", None)
                    current_questions = {}
                else:
                    current_questions, _ = build_addressee_task(turn, bounded_state, {}, limit=1, current_only=True)
                # The core recipient question already judges continuation/soft
                # wakes. Do not reserve a second estimate of the same recipient
                # at the expense of current evidence; add it later if it fits.
                recipient_reservation = {} if "recipient" in questions else current_questions
                intent_questions = build_intent_task({}, {}, compact=item.narrative_bundle is not None)
                bounded_state, intent_questions = bound_request_with_optional(
                    bounded_state, {**questions, **recipient_reservation}, intent_questions
                )
                topic_questions, topic_mapping = align_topic_task(bounded_state, topic_questions, topic_mapping)
                questions = {
                    **build_questions(turn, narrative=item.narrative_bundle is not None, **context_options),
                    **topic_questions,
                }
                # Current recipient first, current semantics second, historical
                # recipient estimates last. Keep the same single model call.
                if rule_routing:
                    questions.pop("recipient", None)
                    addressee_questions, addressee_mapping = {}, {}
                else:
                    addressee_questions, addressee_mapping = build_addressee_task(
                        turn, bounded_state, {**questions, **intent_questions}
                    )
                questions = {**questions, **addressee_questions, **intent_questions}
                request_budget = {
                    "limit_chars": MAX_TOTAL_REQUEST_CHARS,
                    "request_chars": request_size(bounded_state, questions),
                    "decision_persona_chars": len(bounded_state.get("persona", "")),
                    "decision_guidance_chars": len(bounded_state.get("decision_prompt", "")),
                    "decision_question_chars": len(json.dumps(questions, ensure_ascii=False)),
                    "nearby_context_messages": len(bounded_state["conversation"].get("nearby_context", ())),
                    "nearby_context_candidates": len(turn.nearby_context),
                    "nearby_context_chars": len(
                        json.dumps(bounded_state["conversation"].get("nearby_context", ()), ensure_ascii=False)
                    ),
                    "optional_questions_dropped": [key for key in INTENT_CRITERIA if key not in intent_questions],
                }
                if request_budget["optional_questions_dropped"]:
                    p._metric(prefix + "optional_questions_budget_reduced")
            except StateBudgetExceeded:
                p._metric(prefix + "state_budget_exceeded")
                if runtime is not None:
                    runtime.jev_decision = {
                        "semantic_coverage": {
                            key: {
                                "asked": False,
                                "returned": False,
                                "valid": False,
                                "accepted": False,
                                "reason": "required_context_budget",
                            }
                            for key in INTENT_CRITERIA
                        }
                    }
                # Internal context budgeting must not silence a named request: an
                # @ falls back to the conservative reply plan (the same plan the
                # no-Jev mandatory path uses), while ambient turns stay silent.
                if turn.mandatory_reply:
                    return TurnDecision.fallback(turn, prefix + "state_budget_exceeded")
                return TurnDecision.abstain(prefix + "state_budget_exceeded")
            answers = await client.evaluate(
                state=bounded_state,
                questions=questions,
                timeout=timeout,
            )
        coverage = semantic_coverage(intent_questions, answers, floor=floor)
        for key, status in coverage.items():
            for counter in ("asked", "returned", "accepted", "recognized"):
                if status[counter]:
                    p._metric("semantic_" + key + "_" + counter)
            if not status["accepted"]:
                p._metric("semantic_" + key + "_dropped_" + status["reason"])
        if runtime is not None:
            runtime.jev_decision = {"semantic_coverage": coverage, "request_budget": request_budget}
        if not answers:
            p._metric(prefix + "unavailable")
            return TurnDecision.fallback(turn, prefix + "unavailable")
        # Optional estimates must not poison traces or introduce unasked labels.
        answers = dict(answers)
        for key, criteria in INTENT_CRITERIA.items():
            raw = answers.get(key)
            confidence = raw.get("confidence") if isinstance(raw, dict) else None
            if (
                key not in intent_questions
                or not isinstance(raw, dict)
                or raw.get("type") != "choice"
                or not isinstance(raw.get("choice"), str)
                or raw["choice"] not in criteria
                or type(confidence) not in (int, float)
                or not 0 <= confidence <= 1
            ):
                answers.pop(key, None)
            else:
                answers[key] = {"type": "choice", "choice": raw["choice"], "confidence": float(confidence)}
        resolved_addressees = resolve_addressees(answers, addressee_mapping)
        if rule_routing:
            resolved_addressees = rule_addressees(turn)
            # Ignore unsolicited model attribution. Participation/style choices remain.
            answers.pop("topic", None)
            current = resolved_addressees.get(turn.messages[-1].message_id, {}) if turn.messages else {}
            kind = current.get("kind", "unclear")
            answers["recipient"] = {
                "type": "choice",
                "choice": "bot" if kind == "bot" else "other" if kind == "member" else "unclear",
                "confidence": current.get("confidence", 0.0),
            }
        if runtime is not None:
            runtime.jev_decision = describe_answers(answers)
            runtime.jev_decision["semantic_coverage"] = coverage
            runtime.jev_decision["request_budget"] = request_budget
            if rule_routing:
                runtime.jev_decision["recipient_source"] = "rules"
            runtime.jev_addressees = resolved_addressees
            runtime.jev_decision["interaction_frame"] = {
                "session_key": turn.session_key,
                "speaker_id": turn.author,
                "trigger_message_id": turn.messages[-1].message_id if turn.messages else "",
                "answers": {key: runtime.jev_decision[key] for key in intent_questions if key in runtime.jev_decision},
            }
        p._metric(prefix + "decision")
        decision = decision_from_answers(
            turn,
            answers,
            min_confidence=floor,
            join_floor=getattr(p._runtime_config, "reply_probability_threshold", 70.0) / 100.0,
            quote_recipient_floor=getattr(p._runtime_config, "quote_reply_threshold", 35.0) / 100.0,
            name_recipient_floor=getattr(p._runtime_config, "name_reply_threshold", 70.0) / 100.0,
            prefix=prefix,
            narrative=item.narrative_bundle is not None,
            addressees=resolved_addressees,
        )
        decision = annotate_model_decision(decision, turn, answers, resolved_addressees)
        if topic_mapping is not None and decision.reason_code != "jev_waiting_for_completion":
            async with runtime.state_lock:
                if (
                    await self.valid_async(runtime, item)
                    and p._sessions.get(runtime.session_key) is runtime
                    and config_id == p._turn_config_identity()
                ):
                    if apply_topic_answer(runtime, turn, answers.get("topic"), topic_mapping, p.time_service.time()):
                        p.topic_batches.enqueue(runtime, topic_mapping["nodes"])
                        p._mark_panel_runtime_dirty()
        return decision

    async def run_owned(self, runtime, user_id: str, call, *, timeout: float):
        """Cancel one member's call without tearing down the group's queue worker."""
        child = self.plugin._create_background_task(call())
        runtime.owned_turn_tasks[child] = user_id
        try:
            return await asyncio.wait_for(child, timeout=timeout)
        finally:
            runtime.owned_turn_tasks.pop(child, None)

    async def generate_ready(self, runtime, turn, *args, **kwargs):
        """Release a validated final output before optional cleanup/diagnostics."""
        started = time.monotonic()
        ready = asyncio.get_running_loop().create_future()
        early = getattr(self.plugin._runtime_config, "reply_first_sentence_enabled", True)

        def stage(name, seconds, outcome):
            if name.startswith("hook/"):
                self.telemetry.record_hook(name, turn, seconds, outcome)
            else:
                self.telemetry.record(name, turn, seconds, outcome)

        async def output_ready(output):
            self.telemetry.record("final_answer_ready", turn, time.monotonic() - started)
            if not ready.done():
                output.completion_task = child
                ready.set_result(output)

        async def partial_ready(output):
            self.telemetry.record("stream_sentence_ready", turn, time.monotonic() - started)
            if not ready.done():
                output.completion_task = child
                ready.set_result(output)

        stream_allowed = (
            isinstance(self.bridge, AstrBotAgentBridge)
            and early
            and getattr(self.plugin._runtime_config, "max_fragments", 10) > 1
            and not is_rhythm_short_act(str(getattr(runtime, "last_rhythm_action", "")))
        )
        operation = self.generate(
            *args,
            **kwargs,
            on_stage=stage,
            **({"on_output_ready": output_ready} if early else {}),
            **({"on_partial_text": partial_ready} if stream_allowed else {}),
        )
        child = self.plugin._create_background_task(self.telemetry.run("native_agent", turn, operation))
        runtime.owned_turn_tasks[child] = turn.author

        def finished(task):
            runtime.owned_turn_tasks.pop(task, None)
            if not task.cancelled():
                task.exception()

        child.add_done_callback(finished)
        try:
            completed, _ = await asyncio.wait({child, ready}, return_when=asyncio.FIRST_COMPLETED)
            if ready in completed and not ready.cancelled():
                return ready.result()
            return child.result()
        except BaseException:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            raise
        finally:
            if not ready.done():
                ready.cancel()

    def cancel_member(self, runtime, user_id: str) -> None:
        # Finish trace facts before the command advances the owner's revision.
        for item in (runtime.active_model_turn, *runtime.model_queue, *runtime.model_waiting.values()):
            if item is not None and item.context.author == user_id:
                record_outcome(runtime, item, outcomes.mark_suppressed, "member_stopped", stage="generation")
        runtime.cancel_owned_turns(user_id)

    async def generate(self, event, events, prompt, persona, provider, **kwargs):
        """All owned native agents share the adapter's per-provider admission budget."""
        timeout = self.plugin._runtime_config.reply_timeout
        started = time.monotonic()
        if isinstance(self.bridge, AstrBotAgentBridge):
            event._cd_memory_provider_started_v1 = False
            kwargs["reply_timeout"] = timeout
            self.bridge.media_archive = self.plugin.media_archive
            private_direct = bool(kwargs.pop("private_direct", False))
            kwargs["narrative_override"] = None if private_direct else getattr(self.plugin, "narrative", None)
            self.plugin.media_archive.touch(event.unified_msg_origin, persona.conversation_id)
            self.plugin.media_archive.prune()
        call = self.plugin.llm.provider_budget.run(
            provider,
            "reply",
            lambda: self.bridge.generate(event, events, prompt, persona, provider, boundary_host=self.plugin, **kwargs),
        )
        if not isinstance(self.bridge, AstrBotAgentBridge):
            return await asyncio.wait_for(call, timeout)
        try:
            return await call
        except Exception as exc:
            # A native-memory evidence revision can move while the provider
            # request is being prepared (background derivation finishes first),
            # and the fail-closed guard then aborts the whole generation. The
            # validated memory is no longer current, so retry once without any
            # memory projection instead of losing the turn: a reply without
            # recalled memory is still a reply.
            if (
                getattr(event, "_cd_native_memory_source_invalidated_v1", False)
                and not getattr(event, "_cd_memory_retry_done_v1", False)
                and not getattr(event, "_cd_memory_provider_started_v1", False)
                and not getattr(event, "_chat_dynamics_boundary_failed_closed", False)
                and (
                    getattr(exc, "reason_code", "") in {"provider_reply_failed", "native_memory_source_invalidated"}
                    or isinstance(exc, member_boundary.BoundaryCancelled)
                    and str(exc) == "native_memory_source_invalidated"
                )
            ):
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError from exc
                kwargs["reply_timeout"] = remaining
                try:
                    setattr(event, "_cd_memory_retry_done_v1", True)
                    setattr(event, "_cd_retry_without_memory_v1", True)
                    logger.warning(
                        "[ChatDynamics] retrying reply without memory session=%s reason=native_memory_source_invalidated upstream=%s",
                        getattr(event, "unified_msg_origin", ""),
                        str(getattr(exc, "reason_code", "") or type(exc).__name__)[:64],
                    )
                    retry = self.plugin.llm.provider_budget.run(
                        provider,
                        "reply",
                        lambda: self.bridge.generate(
                            event, events, prompt, persona, provider, boundary_host=self.plugin, **kwargs
                        ),
                    )
                    return await retry
                finally:
                    try:
                        delattr(event, "_cd_retry_without_memory_v1")
                    except AttributeError:
                        pass
            raise

    async def run(self, runtime) -> None:
        p = self.plugin
        task = asyncio.current_task()
        p._in_flight.add(runtime.session_key)
        cancelled = False
        try:
            while True:
                async with runtime.state_lock:
                    if not runtime.model_queue:
                        if runtime.generation_task is task:
                            runtime.generation_task = None
                            p._in_flight.discard(runtime.session_key)
                        break
                    item = runtime.model_queue.popleft()
                    runtime.active_model_turn = item
                item_cancelled = False
                deferred_media = DeferredReplyMedia(self, runtime, item)
                try:
                    if await self.valid_async(runtime, item):
                        # Final watchdog covers bridge/provider lookup, native
                        # generation and delivery, not only the decision model.
                        timeout = p._runtime_config.tool_agent_timeout
                        await self.run_owned(
                            runtime,
                            item.context.author,
                            lambda: self.process(runtime, item, deferred_media=deferred_media),
                            timeout=timeout,
                        )
                except asyncio.CancelledError:
                    item_cancelled = True
                    record_terminal_outcome(
                        runtime,
                        item,
                        outcomes.mark_suppressed,
                        "worker_cancelled" if task.cancelling() else "member_cancelled",
                        stage="generation",
                    )
                    if task.cancelling():
                        raise
                except asyncio.TimeoutError:
                    reason = "poke_reply_timeout" if item.context.wake_kind == "poke" else "reply_timeout"
                    self._finish_generation_failure(runtime, item, reason)
                    self.diagnostic(runtime, reason)
                    p._metric("llm_reply_unavailable")
                except PersonaChanged:
                    self._finish_generation_failure(runtime, item, "persona_changed", suppressed=True)
                    self.diagnostic(runtime, "persona_changed")
                except Exception as exc:
                    self._finish_generation_failure(runtime, item, "agent_failed", error_type=type(exc).__name__)
                    self.diagnostic(runtime, "agent_failed", error_type=type(exc).__name__)
                finally:
                    deferred_media.finish()
                    try:
                        if (
                            not item_cancelled
                            and (item.context.mandatory_reply or item.private_direct)
                            and await self.valid_async(runtime, item)
                        ):
                            await asyncio.wait_for(self._ensure_mandatory_reply(runtime, item), timeout=10.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.diagnostic(runtime, "at_fallback_send_failed", error_type=type(exc).__name__)
                    finally:
                        try:
                            self._sweep_in_flight(runtime, item)
                        except Exception as exc:  # never mask the turn's own ending
                            logger.warning(
                                "[ChatDynamics] in-flight sweep failed code=CD_SWEEP_FAILED type=%s",
                                type(exc).__name__,
                            )
                        p.debounce.finish_result(item.debounce_result)
                        p.debounce.release_result_media(item.debounce_result)
                        if item.debounce_result is None:
                            from .deferred_media import release_event_media

                            for event in item.events:
                                release_event_media(event)
                        if runtime.active_model_turn is item:
                            runtime.active_model_turn = None
                        runtime.model_admission.release()
        except asyncio.CancelledError:
            # The worker is being torn down (reset or unload). Every
            # turn still queued was queued with one admission permit held, and
            # nothing will ever pop it once this task is gone: releasing them
            # here keeps "queued turn == held permit" true by construction
            # instead of depending on each cancel site clearing first.
            cancelled = True
            raise
        finally:
            # This owner check and cleanup have no await: they are atomic on
            # the event loop and cannot wait on a cancelling caller's lock.
            if runtime.generation_task is task:
                runtime.generation_task = None
                p._in_flight.discard(runtime.session_key)
                if cancelled:
                    runtime.clear_model_queue()

    async def _ensure_mandatory_reply(self, runtime, item: ModelTurn) -> None:
        """Give a still-current @ one visible response even when generation fails."""
        p, turn = self.plugin, item.context
        # The fallback text below is static and asserts no memory fact, so an
        # invalidated native memory source must not cost a named @ its reply.
        if item.shadow or not item.events or not await self.valid_async(runtime, item):
            return
        nodes = item.outcome_nodes
        if (
            not nodes
            or len(nodes) != len(turn.messages)
            or any(
                node is None or runtime.dag.get_node(message.message_id) is not node
                for message, node in zip(turn.messages, nodes)
            )
        ):
            return
        if any(outcomes.read_outcome(node).get("delivered") for node in nodes):
            return
        failure = next(
            (
                outcomes.read_outcome(node).get("suppression_reason")
                for node in nodes
                if outcomes.read_outcome(node).get("suppression_reason")
            ),
            runtime.model_diagnostic.get("reason_code", "empty_reply"),
        )
        if item.private_direct and not turn.mandatory_reply:
            # Private turns have no @ to acknowledge; a generic receipt is the
            # honest visible ending when generation failed.
            text = "收到你的消息了，但这次暂时没能生成完整回复。"
        else:
            text = unavailable_reply(failure)
        target = turn.messages[-1].message_id
        async with runtime.send_lock:
            if not await self.valid_async(runtime, item):
                return
            fallback_event = item.events[-1]
            if not isinstance(getattr(fallback_event, "_extras", None), dict):
                fallback_event._extras = {}
            fallback_event._extras["_narrative_generation_failure_v1"] = failure
            try:
                sent = await p._send_owned(
                    runtime, fallback_event, text, reply_to_id=_platform_reply_id(runtime, item, target)
                )
            finally:
                fallback_event._extras.pop("_narrative_generation_failure_v1", None)
        if not sent.success:
            if sent.error == "boundary_policy_cancelled":
                record_outcome(runtime, item, outcomes.mark_suppressed, "boundary_policy_changed", stage="delivery")
                return
            record_outcome(runtime, item, outcomes.mark_delivery_failed, "at_fallback_send_failed")
            self.diagnostic(runtime, "at_fallback_send_failed")
            p._metric("send_failed")
            return
        p._metric("send_succeeded")
        fallback_reason = (
            "private_reply_fallback" if (item.private_direct and not turn.mandatory_reply) else "at_reply_fallback"
        )
        record_outcome(runtime, item, outcomes.mark_delivered, reason=fallback_reason)
        self.diagnostic(
            runtime,
            fallback_reason,
            failure_reason=failure,
            action="acknowledge",
            wake_kind="private" if item.private_direct and not turn.mandatory_reply else "at",
            reply_required=True,
        )
        if not await self.valid_async(runtime, item):
            return
        real_id = str(sent.message_id) if sent.message_id else None
        bot = runtime.dag.add_message(
            real_id or p._next_outgoing_id(),
            runtime.bot_id,
            text,
            timestamp=p.time_service.time(),
            reply_to_id=target,
            metadata={
                "platform_message_id": real_id is not None,
                "dialogue_delivered": True,
                "trigger_user_id": turn.author,
                "dialogue_stop_revision": runtime.topic_stop_revisions.get(turn.author, 0),
            },
        )
        p._observe_routed_bot(runtime, bot)
        runtime.last_bot_node = bot
        p._last_bot_nodes[runtime.session_key] = bot
        runtime.remember_sent(bot.msg_id)
        runtime.last_interlocutor = turn.author
        runtime.last_model_send = p.time_service.time()
        p.arbiter.record_bot_spoke(runtime.session_key, timestamp=runtime.last_model_send, user_id=turn.author)

    async def process(self, runtime, item: ModelTurn, *, deferred_media=None) -> None:
        await self.telemetry.run(
            "turn_total", item.context, self._process(runtime, item, deferred_media=deferred_media)
        )

    async def _process(self, runtime, item: ModelTurn, *, deferred_media=None) -> None:
        p, turn = self.plugin, item.context
        processing_started = time.monotonic()
        decision_turn = turn
        event = item.events[-1]
        started = p.time_service.time()
        if getattr(event, "_extras", None) is None:
            event._extras = {}
        event._extras["_cd_pipeline_turn_id_v1"] = self.telemetry.turn_id(turn)
        persona = await self.telemetry.run("persona_snapshot", turn, self.bridge.snapshot(event))
        narrative = None if item.private_direct else getattr(p, "narrative", None)
        bundle = (
            await self.telemetry.run(
                "prepare_decision", turn, narrative.prepare(runtime, item, persona, lightweight=True)
            )
            if narrative is not None
            else None
        )
        judgement_persona = persona
        if bundle is not None:
            material = bundle.wire()["character"]
            judgement_persona = replace(
                persona, prompt=material["core_prompt"] + "\n" + material.get("group_rules", "")
            )
            item = replace(item, narrative_bundle=bundle)
        interaction_state = runtime.interaction_state
        if item.private_direct:
            target = turn.messages[-1].message_id if turn.messages else ""
            decision = TurnDecision(
                action="reply",
                state=interaction_state,
                target_message_ids=(target,) if target else (),
                response_goal="直接回应这条私聊消息。",
                length="normal",
                reason_code="private_direct",
            )
        else:
            decision = await self.decide(item, judgement_persona, interaction_state, runtime=runtime)
        if bundle is not None:
            impact = runtime.jev_decision.get("impact", {})
            impact = (
                impact.get("choice", "none")
                if isinstance(impact, dict) and impact.get("confidence", 0) >= p._runtime_config.jev_min_confidence
                else "none"
            )
            await narrative.route_life_events_async(bundle, impact)
            revised = await narrative.advance_important(item, bundle, impact)
            if revised.payload != bundle.payload:
                bundle = revised
                item = replace(item, narrative_bundle=bundle)
                decision = await self.decide(item, judgement_persona, interaction_state, runtime=runtime)
        if (
            getattr(p._runtime_config, "context_completion_enabled", False)
            and not item.private_direct
            and not item.shadow
            and not (bundle and bundle.wire().get("internal"))
        ):
            need = runtime.jev_decision.get("context_need", {})
            confidence = need.get("confidence", 0)
            reason = runtime.jev_decision.get("reason", {}).get("choice")
            mode = (
                need.get("choice")
                if (
                    need.get("type") == "choice"
                    and type(confidence) in (int, float)
                    and p._runtime_config.jev_min_confidence <= confidence <= 1
                )
                else "ready"
            )
            if (
                mode in {"recent", "topic", "memory"}
                and decision.reason_code != "jev_waiting_for_completion"
                and reason not in {"other_recipient", "boundary_or_sensitive"}
                and not item.observations.get("context_completion", {}).get("attempted")
                and not any(set(m.mentioned_users) - {turn.bot_id} for m in turn.messages)
            ):
                try:
                    item = await complete_context(p, runtime, item, mode)
                except member_boundary.BoundaryCancelled:
                    record_outcome(
                        runtime, item, outcomes.mark_suppressed, "context_completion_invalidated", stage="generation"
                    )
                    return
                if not await self.valid_async(runtime, item):
                    return
                bundle, turn = item.narrative_bundle, item.context
                decision_turn = turn
                if bundle is not None:
                    await narrative.bind_async(item.events, bundle)
                info = item.observations["context_completion"]
                p._metric("context_completion_attempted")
                logger.info(
                    "Context completion status=%s mode=%s added=%s elapsed_ms=%s",
                    info["status"],
                    mode,
                    info["added_messages"],
                    info["elapsed_ms"],
                )
                decision = await self.decide(item, judgement_persona, interaction_state, runtime=runtime)
                need = runtime.jev_decision.get("context_need", {})
                confidence = need.get("confidence", 0)
                mode = (
                    need.get("choice")
                    if (
                        need.get("type") == "choice"
                        and type(confidence) in (int, float)
                        and p._runtime_config.jev_min_confidence <= confidence <= 1
                    )
                    else "clarify"
                )
                if decision.action != "ignore":
                    decision = replace(
                        decision,
                        response_goal=decision.response_goal
                        + " 本轮自动补查已结束；依据已提供来源回应，不为同一缺口重复检索。没有找到不等于从未发生；仍缺关键信息就澄清。",
                    )
            reason = runtime.jev_decision.get("reason", {}).get("choice")
            if (
                decision.action != "ignore"
                and reason not in {"other_recipient", "boundary_or_sensitive"}
                and (
                    mode == "clarify"
                    or (
                        item.observations.get("context_completion", {}).get("attempted")
                        and mode in {"recent", "topic", "memory"}
                    )
                )
            ):
                decision = replace(
                    decision,
                    action="clarify",
                    length="brief",
                    reason_code="jev_context_clarify",
                    response_goal="当前仍缺少作答所需的关键信息。只问一个具体、必要的澄清问题，不猜测缺失内容，不声称未检索到的事情从未发生。",
                )
        if bundle is not None and decision.action != "ignore" and decision.reason_code != "jev_waiting_for_completion":
            previous = bundle.wire()
            full_bundle = await self.telemetry.run("prepare_reply", turn, narrative.prepare(runtime, item, persona))
            if full_bundle is None:
                self._finish_generation_failure(runtime, item, "narrative_prepare_unavailable", suppressed=True)
                return
            bundle = full_bundle
            item = replace(item, narrative_bundle=bundle)
            material = bundle.wire()["character"]
            judgement_persona = replace(
                persona, prompt=material["core_prompt"] + "\n" + material.get("group_rules", "")
            )
            prior_view = previous.get("evidence_view")
            current_view = bundle.wire().get("evidence_view")
            if (
                previous["character"].get("fingerprint") != material.get("fingerprint")
                or previous["snapshot"].get("revision") != bundle.snapshot.get("revision")
                or previous["snapshot"].get("scope_revision") != bundle.snapshot.get("scope_revision")
                or bundle.wire().get("availability", {}).get("supplement_context_status") == "conflicting"
                or isinstance(prior_view, dict)
                and isinstance(current_view, dict)
                and prior_view.get("correction_revision") != current_view.get("correction_revision")
            ):
                decision = await self.decide(item, judgement_persona, interaction_state, runtime=runtime)
            if isinstance(prior_view, dict) and current_view is None and decision.action != "ignore":
                decision = replace(
                    decision,
                    action="clarify",
                    length="brief",
                    reason_code="narrative_context_unavailable",
                    response_goal="当前缺少能核实的旧话题依据。结合本轮原句，只问一个具体、必要的澄清问题；不猜测，也不声称未找到的事情从未发生。",
                )
        proposed = decision
        async with runtime.state_lock:
            if not await self.valid_async(runtime, item):
                return
            if not item.private_direct and decision.reason_code != "jev_waiting_for_completion":
                item = refresh_topic_context(runtime, item, boundary_host=p)
                turn = item.context
        projection = await self._project_persona(persona, turn)
        if not await self.valid_async(runtime, item):
            return
        if not await self.bridge.current(event, persona):
            record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
            return
        async with runtime.state_lock:
            # current() awaited host state; stop/reset may have invalidated this
            # turn while it was suspended. Validate again before any gate writes.
            if not await self.valid_async(runtime, item) or any(
                _dag_node(runtime, message.message_id) is None for message in turn.messages
            ):
                return
            now = p.time_service.time()
            if decision.reason_code == "jev_waiting_for_completion":
                waiting_id = turn.messages[-1].message_id

                async def expired():
                    async with runtime.state_lock:
                        if (
                            p._sessions.get(runtime.session_key) is runtime
                            and runtime.epoch == turn.epoch
                            and runtime.user_revisions.get(turn.author, 0) == turn.revision
                            and runtime.model_diagnostic.get("waiting_message_id") == waiting_id
                            and runtime.model_diagnostic.get("waiting_user_id") == turn.author
                        ):
                            self.diagnostic(
                                runtime, "jev_incomplete_expired", action="ignore", reason_zh="补充输入等待已结束"
                            )

                deferred = await p.debounce.defer_result(item.debounce_result, on_expire=expired)
                reason = decision.reason_code if deferred else "jev_incomplete_expired"
                self.diagnostic(
                    runtime,
                    reason,
                    action="wait" if deferred else "ignore",
                    reason_zh="等待这句话的后续内容" if deferred else "未说完等待已结束",
                    waiting_user_id=turn.author,
                    waiting_message_id=waiting_id,
                    shadow=item.shadow,
                    jev=dict(runtime.jev_decision),
                )
                if item.shadow:
                    p._record_shadow_decision(
                        turn.session_key,
                        action="ignore",
                        reason=reason,
                        decision=replace(decision, reason_code=reason),
                        timestamp=now,
                    )
                    p._metric("shadow_decision")
                else:
                    record_outcome(runtime, item, outcomes.mark_suppressed, reason, stage="completion")
                return
            p.debounce.finish_result(item.debounce_result)
            while runtime.ambient_openings and now - runtime.ambient_openings[0] >= 60:
                runtime.ambient_openings.popleft()
            continuation = item.private_direct or turn_is_continuation(runtime, turn, now)
            dialogue_reply = decision.reason_code in {
                "jev_dialogue_answer",
                "jev_dialogue_continue",
                "jev_dialogue_reopen",
            }
            continuation = continuation or dialogue_reply
            addressed = (
                item.private_direct
                or turn_is_addressed(runtime, turn, now)
                or (turn.soft_wake and decision.reason_code in _WAKE_ADMITTED_REASONS)
            )
            wake_admitted = (
                item.private_direct
                or turn.mandatory_reply
                or (turn.soft_wake and decision.reason_code in _WAKE_ADMITTED_REASONS)
            )
            sleep_wake_admitted = (
                item.private_direct
                or turn.mandatory_reply
                or (
                    decision.reason_code in _WAKE_ADMITTED_REASONS
                    and (
                        turn.wake_kind == "quote"
                        or (
                            turn.wake_kind == "name"
                            and any(
                                BotIdentityMatcher.wake_candidate(name, turn.text)
                                and BotIdentityMatcher.is_vocative(name, turn.text)
                                for name in p.bot_names
                            )
                        )
                    )
                )
            )
            opening = not addressed and not continuation
            opening_limit = participation_policy("sensible" if bundle is not None else p._runtime_config.presence_knob)[
                "ambient_openings_per_minute"
            ]
            if opening and opening_limit > 0 and len(runtime.ambient_openings) >= opening_limit:
                decision = replace(decision, action="ignore", reason_code="ambient_budget")
            # Social manners + occasion skin (persona path): quiet degrade, never raise.
            # Quotas are committed only after a successful send, never on observe/fail.
            # Civil wall time is only for hour/day gates; interval math above stays monotonic.
            gate = None
            if not item.private_direct:
                runtime.request_media_understand = False
            gate_engine = None if item.private_direct else getattr(p, "decision_gate", None)
            gate_now = p.time_service.wall_time()
            try:
                cfg = getattr(p, "_runtime_config", None)
                if bundle is not None and cfg is not None:
                    cfg = replace(
                        cfg,
                        daily_rhythm_enabled=False,
                        gap_fill_proactive_enabled=False,
                        cold_memory_nudge_enabled=False,
                        rhythm_morning_hi_enabled=False,
                        rhythm_insomnia_enabled=False,
                    )
                    availability = bundle.snapshot.get("availability", "unknown")
                    if availability == "asleep" and not sleep_wake_admitted:
                        decision = replace(decision, action="ignore", reason_code="narrative_asleep")
                    elif availability in {"asleep", "busy"} and sleep_wake_admitted:
                        decision = replace(
                            decision,
                            action="acknowledge",
                            length="brief",
                            response_goal="简短回应明确呼唤，结合既有忙碌或睡眠状态表达当前限制，不延长对话。",
                        )
                if gate_engine is not None and decision.action != "ignore":
                    dag = getattr(runtime, "dag", None)
                    recent = (
                        dag.get_recent_nodes(limit=12) if dag is not None and hasattr(dag, "get_recent_nodes") else []
                    )
                    tele = None
                    try:
                        tele = p.vibe_analyzer.get_telemetrics(runtime.session_key, current_time=now)
                    except Exception:
                        tele = None
                    vibe = None
                    try:
                        vibe = p.vibe_analyzer.peek_mode(runtime.session_key, current_time=now)
                    except Exception:
                        vibe = None
                    media_types = []
                    for msg in getattr(turn, "messages", ()) or ():
                        media_types.extend(list(getattr(msg, "attachments", ()) or ()))
                    turn_text = str(turn.text or "")
                    has_media_turn = bool(media_types) or any(
                        marker in turn_text for marker in ("[媒体", "[图片", "[语音", "[视频", "[文件")
                    )
                    gate = gate_engine.evaluate(
                        session_id=runtime.session_key,
                        user_id=str(turn.author or ""),
                        text=str(turn.text or ""),
                        vibe_mode=vibe,
                        telemetrics=tele,
                        recent_nodes=recent,
                        bot_id=str(getattr(runtime, "bot_id", "") or ""),
                        explicit=addressed,
                        willingness=1.0 if addressed else 0.55,
                        cfg=cfg,
                        now=gate_now,
                        node_now=now,
                        has_media=has_media_turn,
                        media_component_types=media_types,
                        quoted_bot=bool(turn.explicit),
                        group_memory=getattr(p, "group_memory", None),
                        committed_reply=decision.action != "ignore",
                        continuation=dialogue_reply,
                        public_topic=decision.reason_code == "jev_open_group_topic",
                        current_exchange=bool(
                            (
                                continuation
                                or addressed
                                and reply_interaction(
                                    turn,
                                    runtime.jev_decision.get("interaction_frame"),
                                    floor=p._runtime_config.jev_min_confidence,
                                )
                                .get("communication_intent", {})
                                .get("choice")
                                in {"task", "sharing", "venting", "support", "banter", "affection"}
                            )
                            and runtime.jev_decision.get("reason", {}).get("choice") != "boundary_or_sensitive"
                        ),
                    )
                    runtime.last_occasion = gate.skin.as_dict()
                    runtime.last_manners = gate.manners.as_dict()
                    runtime.last_media_gate = gate.media.as_dict() if gate.media is not None else {}
                    runtime.request_media_understand = bool(gate.request_understand)
                    runtime.last_proactive = gate.proactive.as_dict() if gate.proactive is not None else {}
                    runtime.last_rhythm = gate.rhythm.as_dict() if gate.rhythm is not None else {}
                    if not gate.should_speak and wake_admitted:
                        # Reply to the @ without treating blocked/private media
                        # as authorised for understanding or forwarding.
                        runtime.request_media_understand = False
                        decision = replace(
                            decision,
                            action="acknowledge",
                            length="brief",
                            reason_code="at_required_gate_reply"
                            if turn.mandatory_reply
                            else "wake_addressed_gate_reply",
                            response_goal="明确回应这次呼唤。暂不处理附件或延长对话，简短说明当前限制。",
                        )
                    elif not gate.should_speak:
                        decision = replace(
                            decision,
                            action="ignore",
                            reason_code=gate.reason_code[:48] if gate.reason_code else "manners_silence",
                        )
                    else:
                        runtime.last_length_hint = gate.length_hint or "normal"
                        runtime.last_delay_scale = float(gate.delay_scale or 1.0)
                        runtime.last_rhythm_action = gate.rhythm.action if gate.rhythm is not None else ""
                        if gate.length_hint == "brief" and decision.length != "brief":
                            decision = replace(decision, length="brief")
                elif gate_engine is not None and decision.action == "ignore":
                    gate_engine.note_arbiter_silence(runtime.session_key, decision.reason_code)
            except Exception as exc:
                # Failing open here would send a reply the gate never authorised, and the
                # media/rhythm flags left over from the previous turn would ride along with
                # it (an image forwarded to the vision model on a turn the media gate never
                # saw). A gate that cannot be read denies the turn, and says so.
                gate = None
                runtime.request_media_understand = False
                decision = replace(
                    decision,
                    action="acknowledge" if wake_admitted else "ignore",
                    reason_code=(
                        "at_required_gate_unavailable"
                        if turn.mandatory_reply
                        else "wake_addressed_gate_unavailable"
                        if wake_admitted
                        else "gate_unavailable"
                    ),
                    response_goal="简短回应这次呼唤，不读取或处理附件。" if wake_admitted else decision.response_goal,
                )
                p._metric("gate_unavailable")
                logger.warning(
                    "[ChatDynamics] Participation gate unavailable code=CD_GATE_UNAVAILABLE type=%s",
                    type(exc).__name__,
                )
            # Complete the trace only after the model decision and participation gates.
            # The projection is read once per turn -- it is a property of the persona,
            # not of any message -- and cached per fingerprint from there on.
            for message in () if item.shadow else item.context.messages:
                node = _dag_node(runtime, message.message_id)
                trace = getattr(node, "metadata", {}).get("decision_trace")
                if isinstance(trace, dict) and isinstance(trace.get("participation"), dict):
                    from .routing_trace import finalize_decision_trace, compact_trace_inputs

                    trace = stage_trace(
                        trace,
                        persona=persona,
                        interaction_state=interaction_state,
                        presence=p._runtime_config.presence_knob,
                        turn=decision_turn,
                        proposed=proposed,
                        final=decision,
                        gate=gate,
                        axes=projection,
                        model_answers={} if item.private_direct else runtime.jev_decision,
                        addressees={} if item.private_direct else runtime.jev_addressees,
                    )
                    node.metadata["decision_trace"] = finalize_decision_trace(
                        trace, should_reply=decision.action != "ignore", branch=decision.reason_code
                    )
                    node.metadata["trace_inputs"] = compact_trace_inputs(node.metadata["decision_trace"])

            # The typed answers of a System One decision travel with the diagnostic so
            # the console can show why the decision layer chose what it chose; the
            # model path leaves that field empty rather than showing a stale one.
            evidence = dict(getattr(runtime, "jev_decision", {}) or {})
            narrative_trace = (
                {
                    "bundle_id": bundle.bundle_id,
                    "turn_id": bundle.turn_id,
                    "revision": bundle.snapshot.get("revision"),
                    "scope_revision": bundle.snapshot.get("scope_revision"),
                    "character_fingerprint": bundle.wire()["character"].get("fingerprint"),
                }
                if bundle is not None
                else {}
            )
            self.diagnostic(
                runtime,
                decision.reason_code,
                action=decision.action,
                state=decision.state,
                target_message_ids=list(decision.target_message_ids),
                length=decision.length,
                latency_ms=round((now - started) * 1000),
                shadow=item.shadow,
                backend=str(getattr(p._runtime_config, "decision_backend", "model") or "model"),
                wake_kind=turn.wake_kind,
                reply_required=turn.mandatory_reply,
                context_refreshed=turn is not decision_turn,
                background_message_ids=[message.message_id for message in turn.background],
                group_event_ids=[notice.event_id for notice in turn.group_events],
                **({"narrative": narrative_trace} if narrative_trace else {}),
                **({"jev": evidence} if evidence else {}),
            )
            if turn.group_events or any(
                proof.relation == "topic_origin" for message in turn.background for proof in message.context_evidence
            ):
                logger.info(
                    "[ChatDynamics] context sources session=%s turn=%s background=%s group_events=%s",
                    runtime.session_key,
                    _turn_log_id(runtime, item),
                    ",".join(message.message_id for message in turn.background),
                    ",".join(notice.event_id for notice in turn.group_events),
                )
            if turn.soft_wake or any(_node_metadata(node).get("host_is_at_or_wake") for node in item.outcome_nodes):
                recipient = evidence.get("recipient", {})
                logger.info(
                    "[ChatDynamics] wake decision session=%s turn=%s messages=%s wake=%s "
                    "recipient=%s confidence=%s action=%s reason=%s sleep_wake=%s",
                    runtime.session_key,
                    _turn_log_id(runtime, item),
                    ",".join(message.message_id for message in turn.messages),
                    turn.wake_kind,
                    recipient.get("choice", "unknown"),
                    recipient.get("confidence", "unknown"),
                    decision.action,
                    decision.reason_code,
                    sleep_wake_admitted,
                )
            if item.shadow:
                p._record_shadow_decision(
                    turn.session_key,
                    action=decision.action,
                    reason=decision.reason_code,
                    decision=decision,
                    timestamp=now,
                )
                p._metric("shadow_decision")
                return
            if decision.action == "ignore":
                if not item.shadow:
                    p._metric("speech_withheld")
                if proposed.reason_code in ("decision_timeout", "decision_invalid_or_failed", "queue_overload"):
                    record_outcome(runtime, item, outcomes.mark_generation_failed, proposed.reason_code)
                else:
                    record_outcome(
                        runtime,
                        item,
                        outcomes.mark_suppressed,
                        decision.reason_code,
                        stage="persona" if proposed.action == "ignore" else "gate",
                    )
                runtime.interaction_state = decision.state
                return
            if bundle is not None:
                plan = {
                    "action": decision.action,
                    "state": decision.state,
                    "length": decision.length,
                    "reason_code": decision.reason_code,
                }
                goal = str(decision.response_goal or "")
                if runtime.jev_decision and decision.reason_code.startswith("jev_"):
                    # Native Jev's response goal comes from the plugin's fixed
                    # action/reason templates, not from conversation text.
                    plan.update(response_goal=goal[:600], response_goal_source="program")
                else:
                    plan.update(
                        response_goal_sha256=hashlib.sha256(goal.encode("utf-8")).hexdigest(),
                        response_goal_length=len(goal),
                        response_goal_source="unverified",
                    )
                extras = getattr(event, "_extras", None)
                if extras is None:
                    event._extras = {}
                    extras = event._extras
                extras["_narrative_plan_diagnostic_v1"] = plan
        async with self.bridge.session_lock(turn.session_key):
            if not await self.valid_async(runtime, item):
                return
            if not await self.bridge.current(event, persona):
                record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                return
            provider = await p.llm.resolve_provider_id(turn.session_key)
            async with runtime.state_lock:
                if not await self.valid_async(runtime, item) or any(
                    _dag_node(runtime, message.message_id) is None for message in turn.messages
                ):
                    return
            understand = bool(getattr(runtime, "request_media_understand", False))
            try:
                reminder_context = p.group_memory.reminder_context(turn.session_key, now=p.time_service.wall_time())
            except Exception as exc:
                logger.warning("notebook context unavailable type=%s", type(exc).__name__)
                reminder_context = []
            record_outcome(runtime, item, outcomes.mark_in_flight)
            if bundle is not None:
                await narrative.attach_reply_evidence_async(item, decision, runtime.jev_decision)
            try:
                p._metric("native_agent_started")
                generation_started = time.monotonic()
                output = await self.generate_ready(
                    runtime,
                    turn,
                    event,
                    item.events,
                    reply_prompt(
                        turn,
                        decision,
                        interaction_frame=runtime.jev_decision.get("interaction_frame"),
                        interaction_floor=p._runtime_config.jev_min_confidence,
                        conversation_map=conversation_map(
                            turn, getattr(runtime, "jev_addressees", None), floor=p._runtime_config.jev_min_confidence
                        ),
                        delivery_constraints={
                            "length_hint": decision.length,
                            "rhythm_state": str(getattr(getattr(gate, "rhythm", None), "state", "")),
                            "rhythm_action": str(getattr(getattr(gate, "rhythm", None), "action", "")),
                        },
                        reply_guidance=REPLY_GUIDANCE if bundle is not None else p._runtime_config.reply_prompt,
                        group_reminders=reminder_context,
                        character_context=bundle.decision_context() if bundle is not None else None,
                        include_reply_guidance=bundle is None,
                    ),
                    persona,
                    provider,
                    execution_log=runtime.tool_executions,
                    history_text=turn.history_text(),
                    media_understand=understand,
                    private_direct=item.private_direct,
                    deferred_reply_media=deferred_media,
                )
            except ModelReplyFailure as exc:
                p._metric("native_agent_failed")
                record_outcome(runtime, item, outcomes.mark_generation_failed, exc.reason_code)
                self.diagnostic(runtime, exc.reason_code, reason_zh="回复模型未提供可发送内容")
                p._metric("llm_reply_unavailable")
                logger.warning(
                    "[ChatDynamics] reply generation failed session=%s turn=%s reason=%s private=%s",
                    getattr(event, "unified_msg_origin", ""),
                    getattr(turn, "turn_id", "") or getattr(item, "turn_id", ""),
                    str(exc.reason_code)[:64],
                    bool(item.private_direct),
                )
                return
            except asyncio.CancelledError:
                p._metric("native_agent_cancelled")
                raise
            except (PersonaChanged, member_boundary.BoundaryCancelled):
                p._metric("native_agent_cancelled")
                raise
            except Exception:
                p._metric("native_agent_failed")
                p._metric("llm_reply_failed")
                raise
            if not getattr(output, "stream_prefix", ""):
                if output.text or output.chains:
                    p._metric("native_agent_succeeded")
                    p._metric("llm_reply_succeeded")
                else:
                    p._metric("native_agent_failed")
                    p._metric("llm_reply_failed")
            # A first request may create the host conversation; keep that exact identity for sending.
            effective = await self.bridge.snapshot(event)
            if persona.conversation_id and effective.fingerprint != persona.fingerprint:
                record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                return
            if (effective.persona_id, effective.prompt) != (persona.persona_id, persona.prompt):
                record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                return
            if not await self.valid_async(runtime, item):
                return
            rhythm_action = str(
                getattr(getattr(gate, "rhythm", None), "action", "") or getattr(runtime, "last_rhythm_action", "")
            )
            max_fragments = 1 if is_rhythm_short_act(rhythm_action) else p._runtime_config.max_fragments
            fragments = []
            from .stream_reply import output_fragments

            delivered = []
            target_id = decision.target_message_ids[0]
            dag_parent_id = target_id if _dag_node(runtime, target_id) is not None else None
            platform_parent_id = _platform_reply_id(runtime, item, target_id)
            delay_scale = float(getattr(runtime, "last_delay_scale", 1.0) or 1.0)
            if gate is not None:
                delay_scale = float(gate.delay_scale or delay_scale)
            try:
                async for index, fragment in output_fragments(
                    output,
                    p.pacer,
                    max_fragments,
                    delivery_fragments,
                    final_fragments
                    if getattr(p._runtime_config, "reply_first_sentence_enabled", True)
                    else (
                        lambda out, pacer, maximum, fragmenter: fragmenter(
                            out.chains, out.text, pacer, max_fragments=maximum
                        )
                    ),
                    fragments,
                ):
                    delay = p.pacer.inter_burst_interval if index else 0.0
                    delay = scale_delay(delay, delay_scale)
                    if delay:
                        await p.time_service.sleep(delay)
                    if not await self.bridge.current(event, effective):
                        record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                        break
                    if bundle is not None and not await narrative.current(bundle, event):
                        record_outcome(
                            runtime, item, outcomes.mark_suppressed, "narrative_character_changed", stage="generation"
                        )
                        break
                    async with runtime.send_lock:
                        if not await self.valid_async(runtime, item):
                            break
                        check_sources = getattr(output, "native_sources_current", None)
                        if callable(check_sources) and not await check_sources():
                            record_outcome(
                                runtime,
                                item,
                                outcomes.mark_suppressed,
                                "native_memory_source_invalidated",
                                stage="delivery",
                            )
                            break
                        send_source_validator = getattr(output, "native_memory_validator", None)
                        if send_source_validator is None:
                            sent = await p._send_owned(runtime, event, fragment, reply_to_id=platform_parent_id)
                        else:
                            sent = await p._send_owned(
                                runtime,
                                event,
                                fragment,
                                reply_to_id=platform_parent_id,
                                native_source_validator=output.native_sources_current,
                            )
                    if not sent.success:
                        if sent.error == "boundary_policy_cancelled":
                            record_outcome(
                                runtime, item, outcomes.mark_suppressed, "boundary_policy_changed", stage="delivery"
                            )
                            p._metric("boundary_send_cancelled")
                            break
                        record_outcome(runtime, item, outcomes.mark_delivery_failed, "send_failed")
                        p._metric("send_failed")
                        break
                    record_outcome(runtime, item, outcomes.mark_delivered)
                    if not delivered:
                        sent_at = time.monotonic()
                        timings = {
                            "generation_to_first_send": sent_at - generation_started,
                            "processing_to_first_send": sent_at - processing_started,
                        }
                        submitted = getattr(item, "submitted_monotonic", 0)
                        if submitted > 0:
                            timings["queue_wait_for_delivered"] = max(0.0, processing_started - submitted)
                        received = [getattr(raw, "_cd_received_monotonic_v1", None) for raw in item.events]
                        received = [stamp for stamp in received if type(stamp) in (float, int) and 0 < stamp <= sent_at]
                        if received:
                            timings["received_to_first_send"] = sent_at - max(received)
                            timings["oldest_input_to_first_send"] = sent_at - min(received)
                        self.telemetry.record_delivery(turn, timings)
                        completion = getattr(output, "completion_task", None)
                        if completion is not None and not completion.done():
                            p._metric("first_sentence_before_agent_finalize")
                    delivered_text = (
                        fragment
                        if isinstance(fragment, str)
                        else "".join(getattr(c, "text", "[已发送媒体]") for c in fragment.chain)
                    )
                    semantic = getattr(fragment, "_cd_delivery_text_v1", None)
                    if isinstance(semantic, str):
                        delivered_text = semantic
                    delivered.append(delivered_text)
                    p._metric("send_succeeded")
                    if runtime.epoch != turn.epoch:
                        break
                    real_message_id = str(sent.message_id) if sent.message_id else None
                    msg_id = real_message_id or p._next_outgoing_id()
                    bot = runtime.dag.add_message(
                        msg_id=msg_id,
                        user_id=runtime.bot_id,
                        text=delivered_text,
                        timestamp=p.time_service.time(),
                        reply_to_id=dag_parent_id,
                        metadata={
                            "platform_message_id": real_message_id is not None,
                            "dialogue_delivered": True,
                            "trigger_user_id": turn.author,
                            "dialogue_stop_revision": runtime.topic_stop_revisions.get(turn.author, 0),
                            "dialogue_state": decision.state,
                            "dialogue_action": decision.action,
                        },
                    )
                    bot.metadata["platform_message_id"] = real_message_id is not None
                    check_sources = getattr(output, "native_sources_current", None)
                    if callable(check_sources) and not await check_sources():
                        # Keep the delivered node/history truthful, but do not
                        # derive routing or embedding state from revoked memory.
                        runtime.last_bot_node = bot
                        p._last_bot_nodes[runtime.session_key] = bot
                        runtime.remember_sent(msg_id)
                        break
                    p._observe_routed_bot(runtime, bot)
                    native_source_validator = getattr(output, "native_memory_validator", None)
                    if native_source_validator is None:
                        p._schedule_neural_embed(runtime.session_key, bot)
                    else:
                        p._schedule_neural_embed(
                            runtime.session_key,
                            bot,
                            native_source_validator=native_source_validator,
                        )
                    runtime.last_bot_node = bot
                    p._last_bot_nodes[runtime.session_key] = bot
                    runtime.remember_sent(msg_id)
                    dag_parent_id = msg_id
                    platform_parent_id = real_message_id
                    if len(delivered) == 1:
                        if not item.private_direct:
                            runtime.interaction_state = decision.state
                            p.arbiter.record_bot_spoke(
                                runtime.session_key, timestamp=p.time_service.time(), user_id=turn.author
                            )
                        runtime.last_interlocutor = turn.author
                        runtime.last_model_send = p.time_service.time()
                        if opening:
                            runtime.ambient_openings.append(runtime.last_model_send)
                        if gate is not None and gate.should_speak and gate_engine is not None:
                            try:
                                gate_engine.note_spoke(
                                    runtime.session_key,
                                    skin=gate.skin,
                                    proactive=gate.proactive,
                                    rhythm=gate.rhythm,
                                )
                            except Exception:
                                pass
                if not fragments:
                    record_outcome(runtime, item, outcomes.mark_generation_failed, "empty_reply")
            finally:
                completion_cancelled = False
                completion = getattr(output, "completion_task", None)
                if completion is not None:
                    if getattr(output, "stream_prefix", "") and not delivered and not completion.done():
                        completion.cancel()
                    try:
                        await asyncio.shield(completion)
                        if getattr(output, "stream_prefix", ""):
                            p._metric("native_agent_succeeded")
                            p._metric("llm_reply_succeeded")
                    except asyncio.CancelledError:
                        completion_cancelled = True
                        if getattr(output, "stream_prefix", ""):
                            p._metric("native_agent_cancelled")
                    except Exception:
                        p._metric("agent_finalize_failed_after_output")
                        if getattr(output, "stream_prefix", ""):
                            p._metric("native_agent_failed")
                            p._metric("llm_reply_failed")
                # Reset/cool may cancel a tail; persist only already delivered text, never a draft.
                if delivered:
                    committed = await self.bridge.commit(event, output, "\n\n".join(delivered))
                    if not committed:
                        self.diagnostic(runtime, "history_conflict")
                if completion_cancelled:
                    raise asyncio.CancelledError
            if (
                deferred_media is not None
                and delivered
                and len(delivered) == len(fragments)
                and committed
                and not any(
                    type(component).__name__.lower() == "image"
                    for fragment in fragments
                    if not isinstance(fragment, str)
                    for component in getattr(fragment, "chain", ())
                )
            ):
                deferred_media.confirm(getattr(output, "native_sources_current", None), text="\n\n".join(delivered))
