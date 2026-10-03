"""One model decision per turn, outside session locks, with bounded admission."""

from __future__ import annotations

import asyncio
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
from .message_semantics import describe_message
from .member_identity import quoted_identity
from .presence_policy import participation_policy
from .persona_trace import record_outcome, stage_trace
from . import outcome_recorder as outcomes
from .turn_decision import (
    MessageSnapshot, TurnContext, TurnDecision, reply_prompt,
)
from .jev_decision import (StateBudgetExceeded, bound_request_state, build_questions, build_state,
                           decision_from_answers, describe_answers)
from .topic_jev import align_topic_task, build_topic_task, apply_topic_answer, capture_topic_candidates
from .context_retrieval import clip_text, select_background
from .routing_contract import commit_topic_evidence

logger = logging.getLogger("astrbot_plugin_chat_dynamics.persona_engine")

_REQUEST_SUPPLEMENT_WINDOW = 120.0


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


def delivery_fragments(chains: Sequence[Any], text: str, pacer: Any, *, max_fragments: int = 3) -> list[Any]:
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
    parts = [] if (text or "").strip() in chain_texts else (
        pacer.persona_fragments(text or "") if pacer is not None else ([text] if text else []))
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
    return bool(ids and bot_id not in ids
                and float(routing.get("addressee_confidence", 0)) >= 0.72)


def turn_is_addressed(runtime: Any, turn: TurnContext, now: float) -> bool:
    if turn.mandatory_reply or turn.soft_wake:
        return True
    routing = _routing_for_turn(runtime, turn)
    if _routing_other(runtime, routing):
        return False
    return bool(turn.explicit or float(routing.get("bot_addressee_confidence", 0)) >= 0.72
                or is_request_supplement(runtime, turn.author, now))


def turn_is_continuation(runtime: Any, turn: TurnContext, now: float) -> bool:
    routing = _routing_for_turn(runtime, turn)
    if _routing_other(runtime, routing):
        return False

    turn_node = _dag_node(runtime, turn.messages[-1].message_id) if turn.messages else None
    dialogue = dialogue_for_node(runtime, turn_node, eligible_ids=turn.visible_node_ids, window=900)
    if dialogue is None or dialogue.closed:
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
        reply_to in bot_ids or (inferred_parent in bot_ids
                               and float(routing.get("parent_confidence", 0) or 0) >= .72)
    )

    coherent = same_topic or parent_continuity
    window = 120
    if same_topic and float(routing.get("topic_confidence", 0) or 0) >= .8:
        window = {"focused": 900, "supportive": 300}.get(dialogue.interaction_state, 120)
    return bool(
        coherent
        and dialogue.user_id == turn.author
        and 0 <= now - dialogue.updated_at < window
    )


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
    current = next((n for n in sorted(recent, key=lambda n: n.timestamp, reverse=True)
                    if str(n.user_id) == uid), None)
    current_routing = getattr(current, "metadata", {}).get("routing", {})
    if _routing_other(runtime, current_routing):
        return False
    for node in recent:
        if (confirmed_topic_id(current) and confirmed_topic_id(node)
                and confirmed_topic_id(current) != confirmed_topic_id(node)):
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
    selections = {selection.node.msg_id: selection for selection in
                  select_background(dag, nodes, bot_id=str(runtime.bot_id or ""), eligible_ids=eligible_ids)}
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
    budget = 6000
    for mid in sorted(proofs, key=priority):
        if budget <= 0 or len(background) >= 15:
            break
        if mid in original:
            text = clip_text(original[mid].text, min(1200, budget))
            background.append(replace(original[mid], text=text, context_evidence=proofs[mid]))
        else:
            n = selections[mid].node
            text = clip_text(n.text, min(1200, budget))
            background.append(MessageSnapshot(n.msg_id, n.user_id, text, n.reply_to_id or "",
                                              semantics=describe_message(n, dag, runtime.bot_id),
                                              source_text=n.text, timestamp=n.timestamp,
                                              mentioned_users=tuple(n.metadata.get("actual_mentions", n.mentioned_users)),
                                              author_name=str(n.metadata.get("sender_name") or ""),
                                              platform=str(n.metadata.get("sender_platform") or ""),
                                              quoted_author_name=_quoted_author_name(n, dag),
                                              context_evidence=proofs[mid]))
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
    code = next((code for code in reversed(latest.routing_evidence)
                 if code in {"topic_jev_new", "topic_jev_match"}), None)
    evidence = commit_topic_evidence({"evidence": original.routing_evidence,
                                     "topic_ambiguous": latest.topic_ambiguous}, code)
    # Jev updates topic membership, not the original sender/mention/quote facts.
    return replace(original, topic_id=latest.topic_id, topic_confidence=latest.topic_confidence,
                   topic_ambiguous=latest.topic_ambiguous,
                   routing_ambiguous=original.addressee_ambiguous or latest.topic_ambiguous,
                   routing_evidence=tuple(evidence))


def refresh_topic_context(runtime, item: ModelTurn) -> ModelTurn:
    """Refresh confirmed topic evidence under state_lock, using the original watermark."""
    turn = item.context
    if item.context_node_ids is None:
        return item
    nodes = tuple(_dag_node(runtime, message.message_id) for message in turn.messages)
    if any(node is None for node in nodes):
        return item
    if item.outcome_nodes and any(node is not original for node, original in zip(nodes, item.outcome_nodes)):
        return item
    messages = tuple(replace(message, semantics=_refresh_topic_semantics(runtime, message, node))
                     for node, message in zip(nodes, turn.messages))
    if messages == turn.messages:
        return item
    confirmed_topics = {message.semantics.topic_id for message in messages
                        if message.semantics and message.semantics.topic_id
                        and not message.semantics.topic_ambiguous
                        and message.semantics.topic_confidence >= .8}
    retained = tuple(message for message in turn.background
                     if any(proof.priority == 0 for proof in message.context_evidence)
                     or (message.semantics and message.semantics.topic_id in confirmed_topics
                         and any(proof.relation == "same_topic" for proof in message.context_evidence)))
    context = replace(turn, messages=messages,
                      background=_snapshot_background(runtime, nodes, eligible_ids=item.context_node_ids,
                                                      retained=retained))
    # Reclassifying the topic may select another old exchange, but only sources
    # visible to the original turn can enter its reply context.
    dialogue = capture_dialogue(runtime, nodes[-1], item.context_node_ids,
                                current_ids=tuple(m.message_id for m in messages))
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

    messages = tuple(MessageSnapshot(
        resolved.msg_id,
        str(getattr(resolved, "user_id", "") or getattr(parsed, "sender_id", "") or getattr(result, "user_id", "")),
        "",
        _existing_reply_id(runtime, resolved, parsed),
        tuple(getattr(parsed, "media_component_types", ()) or ()),
        describe_message(resolved, dag, runtime.bot_id),
        source_text=resolved.text, timestamp=resolved.timestamp,
        mentioned_users=tuple(resolved.metadata.get("actual_mentions", resolved.mentioned_users)),
        author_name=str(resolved.metadata.get("sender_name") or ""),
        platform=str(resolved.metadata.get("sender_platform") or ""),
        quoted_author_name=_quoted_author_name(resolved, dag),
    ) for parsed, resolved in zip(events, resolved_nodes))
    ordered = dag.get_recent_nodes(limit=0)
    current_ids = set(ids)
    boundary = max(i for i, n in enumerate(ordered) if n.msg_id in current_ids)
    cutoff = max(n.timestamp for n in resolved_nodes)
    context_node_ids = frozenset(n.msg_id for i, n in enumerate(ordered)
                                 if i <= boundary and n.timestamp <= cutoff)
    background = _snapshot_background(runtime, resolved_nodes, eligible_ids=context_node_ids)
    turn = TurnContext(runtime.session_key, result.user_id, result.consolidated_text[:8000], messages,
                       tuple(background), runtime.epoch,
                       getattr(result.last_event, "_chat_dynamics_user_revision", runtime.user_revisions.get(result.user_id, 0)),
                       result.start_time, explicit, bool(result.metadata.get("truncated")) or len(result.consolidated_text) > 8000,
                       source_text=result.consolidated_text,
                       source_truncated=bool(result.metadata.get("truncated")), wake_kind=wake_kind,
                       bot_id=str(runtime.bot_id or ""), visible_node_ids=context_node_ids)
    turn = replace(turn, snapshot_at=cutoff, dialogue=capture_dialogue(runtime, resolved_nodes[-1], context_node_ids,
                                                                    current_ids=ids))
    if getattr(runtime, "routing_state", None) is not None:
        turn = replace(turn, topic_candidates=capture_topic_candidates(runtime, turn))
    platform_ids = frozenset(
        str(getattr(parsed, "message_id", "") or "")
        for parsed, resolved in zip(events, resolved_nodes)
        if getattr(parsed, "message_id", "") and resolved.msg_id == getattr(parsed, "message_id", "")
    )
    observations = {**observations, "ambient_openings_used": sum(
        0 <= cutoff - timestamp < 60 for timestamp in getattr(runtime, "ambient_openings", ()))}
    return ModelTurn(turn, tuple(result.raw_events), observations, shadow, platform_message_ids=platform_ids,
                     outcome_nodes=tuple(_dag_node(runtime, message.message_id) for message in turn.messages),
                     debounce_result=result if result.metadata.get("semantic") else None)


class PersonaEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.bridge = AstrBotAgentBridge(plugin.context)
        self.slots = asyncio.Semaphore(4)
        self._projection_cache = OrderedDict()
        self._projection_tasks = {}

    def valid(self, runtime, item: ModelTurn) -> bool:
        p, turn = self.plugin, item.context
        return (not p._shutting_down and p._persona_mode() and p.shadow_mode == item.shadow
                and p.is_group_takeover_enabled(runtime.group_id)
                and runtime.epoch == turn.epoch
                and runtime.user_revisions.get(turn.author, 0) == turn.revision
                and (item.debounce_result is None or p.debounce.is_result_current(item.debounce_result))
                and (turn.mandatory_reply or turn.soft_wake
                     or not p.arbiter.is_in_deep_cooling(runtime.session_key, current_time=p.time_service.time())
                     or turn_is_continuation(runtime, turn, p.time_service.time())))

    async def submit(self, runtime, item: ModelTurn) -> None:
        if not self.valid(runtime, item):
            self.plugin.debounce.finish_result(item.debounce_result)
            return
        now = self.plugin.time_service.time()
        addressed = (turn_is_addressed(runtime, item.context, now)
                     or turn_is_continuation(runtime, item.context, now))
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
                if not self.valid(runtime, item):
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
            item is not None and item.context.author == user_id
            and item.context.explicit and self.valid(runtime, item)
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
        if (fingerprint in self._projection_tasks or len(self._projection_tasks) >= 4
                or not callable(getattr(p, "_create_background_task", None))
                or not callable(getattr(p, "get_kv_data", None))):
            return None
        async def warm():
            axes = None
            try:
                axes = await asyncio.wait_for(
                    persona_axes.load_cached(p, fingerprint),
                    timeout=p._runtime_config.decision_timeout)
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
        if turn.mandatory_reply:
            return TurnDecision.fallback(turn, "at_mandatory")
        if item.fallback and not turn.soft_wake and not (
                runtime is not None and turn_is_continuation(runtime, turn, p.time_service.time())):
            return TurnDecision.fallback(turn, "queue_overload")
        try:
            return await asyncio.wait_for(
                self._decide_layer(item, persona, state, runtime=runtime),
                timeout=p._runtime_config.decision_timeout)
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
        client = getattr(p, "jev", None)
        if client is None:
            return TurnDecision.fallback(turn, "jev_unavailable")
        timeout = p._runtime_config.jev_timeout
        floor = p._runtime_config.jev_min_confidence
        observations = dict(item.observations)
        if turn.dialogue is not None:
            state = turn.dialogue.interaction_state
        if item.debounce_result is not None:
            now = p.time_service.time()
            flushed_at = item.debounce_result.metadata.get("flushed_at", now)
            observations.update(utterance_pause_seconds=max(0.0, flushed_at - item.debounce_result.end_time),
                                utterance_age_seconds=max(0.0, flushed_at - turn.started_at),
                                queue_delay_seconds=max(0.0, now - flushed_at),
                                pending_input=bool(item.debounce_result.metadata.get("pending_input")),
                                completion_waited=item.debounce_result.was_extended)
        async with self.slots:
            topics, topic_questions, topic_mapping = {}, {}, None
            config_id = p._turn_config_identity()
            if runtime is not None and p._runtime_config.conversation_router_enabled:
                async with runtime.state_lock:
                    if self.valid(runtime, item) and p._sessions.get(runtime.session_key) is runtime:
                        runtime.routing_state.expire_topics(runtime.dag, p.time_service.time())
                        topics, topic_questions, topic_mapping = build_topic_task(runtime, turn, p.time_service.time())
            try:
                bounded_state = build_state(
                    turn,
                    previous_state=state,
                    observations=observations,
                    presence=p._runtime_config.presence_knob,
                    persona_prompt=getattr(persona, "prompt", ""),
                    decision_prompt=p._runtime_config.decision_prompt,
                    active_topics=topics,
                )
                topic_questions, topic_mapping = align_topic_task(bounded_state, topic_questions, topic_mapping)
                questions = {**build_questions(turn), **topic_questions}
                bounded_state = bound_request_state(bounded_state, questions)
                topic_questions, topic_mapping = align_topic_task(bounded_state, topic_questions, topic_mapping)
                questions = {**build_questions(turn), **topic_questions}
            except StateBudgetExceeded:
                p._metric(prefix + "state_budget_exceeded")
                return TurnDecision.abstain(prefix + "state_budget_exceeded")
            answers = await client.evaluate(
                state=bounded_state,
                questions=questions,
                timeout=timeout,
            )
        if not answers:
            p._metric(prefix + "unavailable")
            return TurnDecision.fallback(turn, prefix + "unavailable")
        if runtime is not None:
            runtime.jev_decision = describe_answers(answers)
        p._metric(prefix + "decision")
        decision = decision_from_answers(
            turn,
            answers,
            min_confidence=floor,
            join_floor=getattr(p._runtime_config, "reply_probability_threshold", 70.0) / 100.0,
            prefix=prefix,
        )
        if topic_mapping is not None and decision.reason_code != "jev_waiting_for_completion":
            async with runtime.state_lock:
                if (self.valid(runtime, item) and p._sessions.get(runtime.session_key) is runtime
                        and config_id == p._turn_config_identity()):
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

    def cancel_member(self, runtime, user_id: str) -> None:
        # Finish trace facts before the command advances the owner's revision.
        for item in (runtime.active_model_turn, *runtime.model_queue, *runtime.model_waiting.values()):
            if item is not None and item.context.author == user_id:
                record_outcome(runtime, item, outcomes.mark_suppressed, "member_stopped", stage="generation")
        runtime.cancel_owned_turns(user_id)

    async def generate(self, event, events, prompt, persona, provider, **kwargs):
        """All owned native agents share the adapter's per-provider admission budget."""
        timeout = self.plugin._runtime_config.reply_timeout
        if isinstance(self.bridge, AstrBotAgentBridge):
            kwargs["reply_timeout"] = timeout
            self.bridge.media_archive = self.plugin.media_archive
            self.plugin.media_archive.touch(event.unified_msg_origin, persona.conversation_id)
            self.plugin.media_archive.prune()
        call = self.plugin.llm.provider_budget.run(
            provider, "reply", lambda: self.bridge.generate(event, events, prompt, persona, provider, **kwargs))
        return await call if isinstance(self.bridge, AstrBotAgentBridge) else await asyncio.wait_for(call, timeout)

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
                try:
                    if self.valid(runtime, item):
                        # Final watchdog covers bridge/provider lookup, native
                        # generation and delivery, not only the decision model.
                        timeout = p._runtime_config.tool_agent_timeout
                        await self.run_owned(runtime, item.context.author,
                                             lambda: self.process(runtime, item), timeout=timeout)
                except asyncio.CancelledError:
                    item_cancelled = True
                    if task.cancelling():
                        raise
                except asyncio.TimeoutError:
                    if self.valid(runtime, item):
                        record_outcome(runtime, item, outcomes.mark_generation_failed, "reply_timeout")
                    self.diagnostic(runtime, "reply_timeout")
                    p._metric("llm_reply_unavailable")
                except PersonaChanged:
                    if self.valid(runtime, item):
                        record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                    self.diagnostic(runtime, "persona_changed")
                except Exception as exc:
                    if self.valid(runtime, item):
                        record_outcome(runtime, item, outcomes.mark_generation_failed, "agent_failed")
                    self.diagnostic(runtime, "agent_failed", error_type=type(exc).__name__)
                finally:
                    try:
                        if not item_cancelled and item.context.mandatory_reply and self.valid(runtime, item):
                            await asyncio.wait_for(self._ensure_mandatory_reply(runtime, item), timeout=10.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.diagnostic(runtime, "at_fallback_send_failed", error_type=type(exc).__name__)
                    finally:
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
        if item.shadow or not item.events or not self.valid(runtime, item):
            return
        nodes = item.outcome_nodes
        if not nodes or len(nodes) != len(turn.messages) or any(node is None or runtime.dag.get_node(message.message_id) is not node
                            for message, node in zip(turn.messages, nodes)):
            return
        if any(outcomes.read_outcome(node).get("delivered") for node in nodes):
            return
        failure = next((outcomes.read_outcome(node).get("suppression_reason") for node in nodes
                        if outcomes.read_outcome(node).get("suppression_reason")),
                       runtime.model_diagnostic.get("reason_code", "empty_reply"))
        text = "收到你的@，但这次暂时没能生成完整回复，请再试一次。"
        target = turn.messages[-1].message_id
        async with runtime.send_lock:
            if not self.valid(runtime, item):
                return
            sent = await p._send_owned(runtime, item.events[-1], text,
                                       reply_to_id=_platform_reply_id(runtime, item, target))
        if not sent.success:
            record_outcome(runtime, item, outcomes.mark_delivery_failed, "at_fallback_send_failed")
            self.diagnostic(runtime, "at_fallback_send_failed")
            p._metric("send_failed")
            return
        p._metric("send_succeeded")
        record_outcome(runtime, item, outcomes.mark_delivered, reason="at_reply_fallback")
        self.diagnostic(runtime, "at_reply_fallback", failure_reason=failure, action="acknowledge",
                        wake_kind="at", reply_required=True)
        if not self.valid(runtime, item):
            return
        real_id = str(sent.message_id) if sent.message_id else None
        bot = runtime.dag.add_message(real_id or p._next_outgoing_id(), runtime.bot_id, text,
                                      timestamp=p.time_service.time(), reply_to_id=target,
                                      metadata={"platform_message_id": real_id is not None,
                                                "dialogue_delivered": True, "trigger_user_id": turn.author,
                                                "dialogue_stop_revision": runtime.topic_stop_revisions.get(turn.author, 0)})
        p._observe_routed_bot(runtime, bot)
        runtime.last_bot_node = bot
        p._last_bot_nodes[runtime.session_key] = bot
        runtime.remember_sent(bot.msg_id)
        runtime.last_interlocutor = turn.author
        runtime.last_model_send = p.time_service.time()
        p.arbiter.record_bot_spoke(runtime.session_key, timestamp=runtime.last_model_send, user_id=turn.author)

    async def process(self, runtime, item: ModelTurn) -> None:
        p, turn = self.plugin, item.context
        decision_turn = turn
        event = item.events[-1]
        started = p.time_service.time()
        persona = await self.bridge.snapshot(event)
        interaction_state = runtime.interaction_state
        decision = await self.decide(item, persona, interaction_state, runtime=runtime)
        proposed = decision
        async with runtime.state_lock:
            if not self.valid(runtime, item):
                return
            if decision.reason_code != "jev_waiting_for_completion":
                item = refresh_topic_context(runtime, item)
                turn = item.context
        projection = await self._project_persona(persona, turn)
        if not self.valid(runtime, item):
            return
        if not await self.bridge.current(event, persona):
            record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
            return
        async with runtime.state_lock:
            # current() awaited host state; stop/reset may have invalidated this
            # turn while it was suspended. Validate again before any gate writes.
            if not self.valid(runtime, item) or any(
                _dag_node(runtime, message.message_id) is None for message in turn.messages
            ):
                return
            now = p.time_service.time()
            if decision.reason_code == "jev_waiting_for_completion":
                waiting_id = turn.messages[-1].message_id

                async def expired():
                    async with runtime.state_lock:
                        if (p._sessions.get(runtime.session_key) is runtime and runtime.epoch == turn.epoch
                                and runtime.user_revisions.get(turn.author, 0) == turn.revision
                                and runtime.model_diagnostic.get("waiting_message_id") == waiting_id
                                and runtime.model_diagnostic.get("waiting_user_id") == turn.author):
                            self.diagnostic(runtime, "jev_incomplete_expired", action="ignore",
                                            reason_zh="补充输入等待已结束")

                deferred = await p.debounce.defer_result(item.debounce_result, on_expire=expired)
                reason = decision.reason_code if deferred else "jev_incomplete_expired"
                self.diagnostic(runtime, reason, action="wait" if deferred else "ignore",
                                reason_zh="等待这句话的后续内容" if deferred else "未说完等待已结束",
                                waiting_user_id=turn.author, waiting_message_id=waiting_id,
                                shadow=item.shadow, jev=dict(runtime.jev_decision))
                if item.shadow:
                    p._record_shadow_decision(turn.session_key, action="ignore", reason=reason,
                        decision=replace(decision, reason_code=reason), timestamp=now)
                    p._metric("shadow_decision")
                else:
                    record_outcome(runtime, item, outcomes.mark_suppressed, reason, stage="completion")
                return
            p.debounce.finish_result(item.debounce_result)
            while runtime.ambient_openings and now - runtime.ambient_openings[0] >= 60:
                runtime.ambient_openings.popleft()
            continuation = turn_is_continuation(runtime, turn, now)
            addressed = (turn_is_addressed(runtime, turn, now)
                         or (turn.soft_wake and decision.reason_code == "jev_wake_addressed"))
            wake_admitted = turn.mandatory_reply or (turn.soft_wake and decision.reason_code == "jev_wake_addressed")
            opening = not addressed and not continuation
            opening_limit = participation_policy(p._runtime_config.presence_knob)["ambient_openings_per_minute"]
            if opening and opening_limit > 0 and len(runtime.ambient_openings) >= opening_limit:
                decision = replace(decision, action="ignore", reason_code="ambient_budget")
            # Social manners + occasion skin (persona path): quiet degrade, never raise.
            # Quotas are committed only after a successful send, never on observe/fail.
            # Civil wall time is only for hour/day gates; interval math above stays monotonic.
            gate = None
            runtime.request_media_understand = False
            gate_engine = getattr(p, "decision_gate", None)
            gate_now = p.time_service.wall_time()
            try:
                cfg = getattr(p, "_runtime_config", None)
                if gate_engine is not None and decision.action != "ignore":
                    dag = getattr(runtime, "dag", None)
                    recent = dag.get_recent_nodes(limit=12) if dag is not None and hasattr(dag, "get_recent_nodes") else []
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
                        public_topic=decision.reason_code == "jev_open_group_topic",
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
                        decision = replace(decision, action="acknowledge", length="brief",
                                           reason_code="at_required_gate_reply" if turn.mandatory_reply else "wake_addressed_gate_reply",
                                           response_goal="明确回应这次呼唤。暂不处理附件或延长对话，简短说明当前限制。")
                    elif not gate.should_speak:
                        decision = replace(decision, action="ignore", reason_code=gate.reason_code[:48] if gate.reason_code else "manners_silence")
                    else:
                        runtime.last_length_hint = gate.length_hint or "normal"
                        runtime.last_delay_scale = float(gate.delay_scale or 1.0)
                        runtime.last_rhythm_action = (
                            gate.rhythm.action if gate.rhythm is not None else ""
                        )
                        if gate.length_hint == "brief" and decision.length != "brief":
                            decision = replace(decision, length="brief")
                elif gate_engine is not None and decision.action == "ignore":
                    gate_engine.note_arbiter_silence(
                        runtime.session_key, decision.reason_code
                    )
            except Exception as exc:
                # Failing open here would send a reply the gate never authorised, and the
                # media/rhythm flags left over from the previous turn would ride along with
                # it (an image forwarded to the vision model on a turn the media gate never
                # saw). A gate that cannot be read denies the turn, and says so.
                gate = None
                runtime.request_media_understand = False
                decision = replace(decision, action="acknowledge" if wake_admitted else "ignore",
                                   reason_code=("at_required_gate_unavailable" if turn.mandatory_reply else
                                                "wake_addressed_gate_unavailable" if wake_admitted else "gate_unavailable"),
                                   response_goal="简短回应这次呼唤，不读取或处理附件。" if wake_admitted else decision.response_goal)
                p._metric("gate_unavailable")
                logger.warning(
                    "[ChatDynamics] Participation gate unavailable code=CD_GATE_UNAVAILABLE type=%s",
                    type(exc).__name__,
                )
            # Complete the trace only after the model decision and participation gates.
            # The projection is read once per turn -- it is a property of the persona,
            # not of any message -- and cached per fingerprint from there on.
            for message in (() if item.shadow else item.context.messages):
                node = _dag_node(runtime, message.message_id)
                trace = getattr(node, "metadata", {}).get("decision_trace")
                if isinstance(trace, dict) and isinstance(trace.get("participation"), dict):
                    from .routing_trace import finalize_decision_trace, compact_trace_inputs
                    trace = stage_trace(trace, persona=persona, interaction_state=interaction_state,
                                        presence=p._runtime_config.presence_knob, turn=decision_turn,
                                        proposed=proposed, final=decision, gate=gate,
                                        axes=projection)
                    node.metadata["decision_trace"] = finalize_decision_trace(
                        trace, should_reply=decision.action != "ignore", branch=decision.reason_code)
                    node.metadata["trace_inputs"] = compact_trace_inputs(node.metadata["decision_trace"])

            # The typed answers of a System One decision travel with the diagnostic so
            # the console can show why the decision layer chose what it chose; the
            # model path leaves that field empty rather than showing a stale one.
            evidence = dict(getattr(runtime, "jev_decision", {}) or {})
            self.diagnostic(runtime, decision.reason_code, action=decision.action, state=decision.state,
                            target_message_ids=list(decision.target_message_ids), length=decision.length,
                            latency_ms=round((now - started) * 1000), shadow=item.shadow,
                            backend=str(getattr(p._runtime_config, "decision_backend", "model") or "model"),
                            wake_kind=turn.wake_kind, reply_required=turn.mandatory_reply,
                            context_refreshed=turn is not decision_turn,
                            **({"jev": evidence} if evidence else {}))
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
                if proposed.reason_code in ("decision_timeout", "decision_invalid_or_failed", "queue_overload"):
                    record_outcome(runtime, item, outcomes.mark_generation_failed, proposed.reason_code)
                else:
                    record_outcome(runtime, item, outcomes.mark_suppressed, decision.reason_code,
                                   stage="persona" if proposed.action == "ignore" else "gate")
                runtime.interaction_state = decision.state
                return
        async with self.bridge.session_lock(turn.session_key):
            if not self.valid(runtime, item):
                return
            if not await self.bridge.current(event, persona):
                record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                return
            provider = await p.llm.resolve_provider_id(turn.session_key)
            async with runtime.state_lock:
                if not self.valid(runtime, item) or any(
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
            output = await self.generate(
                event,
                item.events,
                reply_prompt(turn, decision, delivery_constraints={
                    "length_hint": decision.length,
                    "rhythm_state": str(getattr(getattr(gate, "rhythm", None), "state", "")),
                    "rhythm_action": str(getattr(getattr(gate, "rhythm", None), "action", "")),
                }, reply_guidance=p._runtime_config.reply_prompt, group_reminders=reminder_context),
                persona,
                provider,
                execution_log=runtime.tool_executions,
                history_text=turn.history_text(),
                media_understand=understand,
            )
            # A first request may create the host conversation; keep that exact identity for sending.
            effective = await self.bridge.snapshot(event)
            if persona.conversation_id and effective.fingerprint != persona.fingerprint:
                record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                return
            if (effective.persona_id, effective.prompt) != (persona.persona_id, persona.prompt):
                record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                return
            if not self.valid(runtime, item):
                return
            rhythm_action = str(getattr(getattr(gate, "rhythm", None), "action", "")
                                or getattr(runtime, "last_rhythm_action", ""))
            max_fragments = 1 if is_rhythm_short_act(rhythm_action) else p._runtime_config.max_fragments
            fragments = delivery_fragments(output.chains, output.text, p.pacer,
                                           max_fragments=max_fragments)
            if not fragments:
                record_outcome(runtime, item, outcomes.mark_generation_failed, "empty_reply")
            delivered = []
            target_id = decision.target_message_ids[0]
            dag_parent_id = target_id if _dag_node(runtime, target_id) is not None else None
            platform_parent_id = _platform_reply_id(runtime, item, target_id)
            delay_scale = float(getattr(runtime, "last_delay_scale", 1.0) or 1.0)
            if gate is not None:
                delay_scale = float(gate.delay_scale or delay_scale)
            try:
                for index, fragment in enumerate(fragments):
                    delay = p.pacer.inter_burst_interval if index else 0.0
                    delay = scale_delay(delay, delay_scale)
                    if delay:
                        await p.time_service.sleep(delay)
                    if not await self.bridge.current(event, effective):
                        record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                        break
                    async with runtime.send_lock:
                        if not self.valid(runtime, item):
                            break
                        sent = await p._send_owned(runtime, event, fragment, reply_to_id=platform_parent_id)
                    if not sent.success:
                        record_outcome(runtime, item, outcomes.mark_delivery_failed, "send_failed")
                        p._metric("send_failed")
                        break
                    record_outcome(runtime, item, outcomes.mark_delivered)
                    delivered_text = fragment if isinstance(fragment, str) else "".join(
                        getattr(c, "text", "[已发送媒体]") for c in fragment.chain)
                    delivered.append(delivered_text)
                    p._metric("send_succeeded")
                    if runtime.epoch != turn.epoch:
                        break
                    real_message_id = str(sent.message_id) if sent.message_id else None
                    msg_id = real_message_id or p._next_outgoing_id()
                    bot = runtime.dag.add_message(msg_id=msg_id, user_id=runtime.bot_id, text=delivered_text,
                                                  timestamp=p.time_service.time(), reply_to_id=dag_parent_id,
                                                  metadata={"platform_message_id": real_message_id is not None,
                                                            "dialogue_delivered": True, "trigger_user_id": turn.author,
                                                            "dialogue_stop_revision": runtime.topic_stop_revisions.get(turn.author, 0),
                                                            "dialogue_state": decision.state,
                                                            "dialogue_action": decision.action})
                    bot.metadata["platform_message_id"] = real_message_id is not None
                    p._observe_routed_bot(runtime, bot)
                    p._schedule_neural_embed(runtime.session_key, bot)
                    runtime.last_bot_node = bot
                    p._last_bot_nodes[runtime.session_key] = bot
                    runtime.remember_sent(msg_id)
                    dag_parent_id = msg_id
                    platform_parent_id = real_message_id
                    if len(delivered) == 1:
                        runtime.interaction_state = decision.state
                        p.arbiter.record_bot_spoke(runtime.session_key, timestamp=p.time_service.time(), user_id=turn.author)
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
            finally:
                # Reset/cool may cancel a tail; persist only already delivered text, never a draft.
                if delivered:
                    committed = await self.bridge.commit(event, output, "\n\n".join(delivered))
                    if not committed:
                        self.diagnostic(runtime, "history_conflict")
