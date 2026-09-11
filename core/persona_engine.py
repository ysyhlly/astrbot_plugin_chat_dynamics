"""One model decision per turn, outside session locks, with bounded admission."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Sequence

from .agent_bridge import AstrBotAgentBridge, PersonaChanged
from .llm_adapter import completion_text
from .pacer import is_rhythm_short_act, scale_delay
from .topic_identity import node_topic_id
from .platform_bridge import chain_plain_text
from .message_semantics import describe_message
from .turn_decision import (
    DECISION_INSTRUCTIONS, MessageSnapshot, TurnContext, TurnDecision, decision_prompt, reply_prompt,
)

logger = logging.getLogger("astrbot_plugin_chat_dynamics.persona_engine")

_EXPLICIT_PARENT_EDGE_KINDS = frozenset(("reply", "mention"))
_MAX_EXPLICIT_PARENT_HOPS = 8
_REQUEST_SUPPLEMENT_WINDOW = 120.0


@dataclass(frozen=True)
class ModelTurn:
    context: TurnContext
    events: tuple[Any, ...]
    observations: dict
    shadow: bool
    fallback: bool = False
    platform_message_ids: frozenset[str] = frozenset()


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


def delivery_fragments(chains: Sequence[Any], text: str, pacer: Any) -> list[Any]:
    """Queue tool/media chains, then text fragments that are not the same payload.

    A tool-direct chain that already equals the model transcript must not be
    sent again as a prose fragment.
    """
    fragments: list[Any] = []
    seen: set[str] = set()
    for chain in chains or ():
        fragments.append(chain)
        plain = chain_plain_text(chain).strip()
        if plain:
            seen.add(plain)
    parts = pacer.persona_fragments(text or "") if pacer is not None else ([text] if text else [])
    for part in parts:
        if not part:
            continue
        plain = part.strip() if isinstance(part, str) else chain_plain_text(part).strip()
        if plain and plain in seen:
            continue
        fragments.append(part)
        if plain:
            seen.add(plain)
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
    routing = _routing_for_turn(runtime, turn)
    if _routing_other(runtime, routing):
        return False
    return bool(turn.explicit or float(routing.get("bot_addressee_confidence", 0)) >= 0.72
                or is_request_supplement(runtime, turn.author, now))


def turn_is_continuation(runtime: Any, turn: TurnContext, now: float) -> bool:
    routing = _routing_for_turn(runtime, turn)
    if _routing_other(runtime, routing):
        return False

    last_bot = getattr(runtime, "last_bot_node", None)
    if last_bot is None:
        return False

    bot_topic = node_topic_id(last_bot)
    turn_node = _dag_node(runtime, turn.messages[-1].message_id) if turn.messages else None
    current_topic = node_topic_id(turn_node)
    same_topic = bool(current_topic and bot_topic and current_topic == bot_topic)

    reply_to = getattr(turn, "reply_to", None) or getattr(getattr(turn, "node", None), "reply_to_id", None)
    inferred_parent = routing.get("parent_message_id", "")
    last_bot_id = getattr(last_bot, "msg_id", "")
    if not reply_to and turn_node:
        reply_to = getattr(turn_node, "reply_to_id", None)

    parent_continuity = bool(
        last_bot_id and (
            reply_to == last_bot_id
            or inferred_parent == last_bot_id
            or (turn_node and last_bot_id in getattr(turn_node, "parent_ids", ()))
        )
    )

    coherent = same_topic or parent_continuity
    last_interlocutor = getattr(runtime, "last_interlocutor", "")
    last_model_send = getattr(runtime, "last_model_send", 0.0)

    return bool(
        coherent
        and last_interlocutor == turn.author
        and 0 <= now - last_model_send < 120
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
        recent = getter(limit=12)
    except Exception:
        return False
    bot_id = str(getattr(runtime, "bot_id", "") or "")
    current = next((n for n in sorted(recent, key=lambda n: n.timestamp, reverse=True)
                    if str(n.user_id) == uid), None)
    current_routing = getattr(current, "metadata", {}).get("routing", {})
    if _routing_other(runtime, current_routing):
        return False
    for node in recent:
        candidate_routing = getattr(node, "metadata", {}).get("routing", {})
        if (current_routing.get("topic_id") and candidate_routing.get("topic_id")
                and current_routing["topic_id"] != candidate_routing["topic_id"]):
            continue
        if str(getattr(node, "user_id", "") or "") != uid:
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


def _explicit_parent_ids(node: Any) -> tuple[str, ...]:
    """Return deterministic parent IDs for reply/mention edges only."""
    parent_ids = getattr(node, "parent_ids", ()) or ()
    edge_kinds = getattr(node, "edge_kinds", {}) or {}
    kind_for = getattr(edge_kinds, "get", None)
    if not callable(kind_for):
        return ()

    explicit_ids = []
    for raw_parent_id in sorted(parent_ids, key=str):
        parent_id = str(raw_parent_id or "")
        if parent_id and kind_for(raw_parent_id) in _EXPLICIT_PARENT_EDGE_KINDS:
            explicit_ids.append(parent_id)
    return tuple(dict.fromkeys(explicit_ids))


def _is_background_related(
    node: Any,
    *,
    current_ids: frozenset[str],
    selected_ids: frozenset[str],
    author_id: str,
    bot_id: str,
) -> bool:
    """Apply the bounded background relevance rules independently of identity mapping."""
    message_id = str(getattr(node, "msg_id", "") or "")
    if not message_id or message_id in current_ids:
        return False
    if message_id in selected_ids:
        return True
    if str(getattr(node, "user_id", "") or "") == author_id:
        return True
    reply_to_id = str(getattr(node, "reply_to_id", "") or "")
    return bool(bot_id and str(getattr(node, "user_id", "") or "") == bot_id
                and reply_to_id in selected_ids)


def snapshot_turn(
    runtime: Any,
    result: Any,
    canonical_ids: Sequence[str],
    parsed_events: Sequence[Any],
    explicit: bool,
    observations: dict,
    shadow: bool,
) -> ModelTurn | None:
    """Build a model turn from the caller's ordered, canonical DAG IDs.

    The caller owns fragment ordering.  This function only dereferences the
    supplied IDs and refuses to construct a partial context when the mapping
    is incomplete or stale. Background relevance is selected separately using
    explicit parent ancestry plus bounded same-user and bot-reply rules.
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
    ) for parsed, resolved in zip(events, resolved_nodes))
    current_ids = {m.message_id for m in messages}
    # Follow only explicit DAG parent edges. In particular, mention parents
    # are authoritative even when the platform event has no reply_to field;
    # semantic edges and room-wide time heuristics do not belong in the parent
    # chain. Background relevance below is a separate, bounded supplement.
    selected = set(current_ids)
    frontier = list(ids)
    for _ in range(_MAX_EXPLICIT_PARENT_HOPS):
        next_frontier = []
        for child_id in frontier:
            child = _dag_node(runtime, child_id)
            if child is None:
                continue
            for parent_id in _explicit_parent_ids(child):
                parent = _dag_node(runtime, parent_id)
                if parent is None:
                    continue
                parent_message_id = str(getattr(parent, "msg_id", "") or "")
                if not parent_message_id or parent_message_id in selected:
                    continue
                selected.add(parent_message_id)
                next_frontier.append(parent_message_id)
        if not next_frontier:
            break
        frontier = next_frontier
    candidates = runtime.dag.get_recent_nodes(limit=60)
    background = []
    budget = 6000
    current_ids = frozenset(current_ids)
    selected_ids = frozenset(selected)
    for n in reversed(candidates):
        if _is_background_related(
                n,
                current_ids=current_ids,
                selected_ids=selected_ids,
                author_id=str(getattr(result, "user_id", "") or ""),
                bot_id=str(getattr(runtime, "bot_id", "") or ""),
        ) and budget > 0:
            text = n.text[:min(1200, budget)]
            background.append(MessageSnapshot(n.msg_id, n.user_id, text, n.reply_to_id or "",
                                              semantics=describe_message(n, dag, runtime.bot_id)))
            budget -= len(text)
            if len(background) >= 15:
                break
    turn = TurnContext(runtime.session_key, result.user_id, result.consolidated_text[:8000], messages,
                       tuple(reversed(background)), runtime.epoch,
                       getattr(result.last_event, "_chat_dynamics_user_revision", runtime.user_revisions.get(result.user_id, 0)),
                       result.start_time, explicit, bool(result.metadata.get("truncated")) or len(result.consolidated_text) > 8000)
    platform_ids = frozenset(
        str(getattr(parsed, "message_id", "") or "")
        for parsed, resolved in zip(events, resolved_nodes)
        if getattr(parsed, "message_id", "") and resolved.msg_id == getattr(parsed, "message_id", "")
    )
    return ModelTurn(turn, tuple(result.raw_events), observations, shadow, platform_message_ids=platform_ids)


class PersonaEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.bridge = AstrBotAgentBridge(plugin.context)
        self.slots = asyncio.Semaphore(4)

    def valid(self, runtime, item: ModelTurn) -> bool:
        p, turn = self.plugin, item.context
        return (not p._shutting_down and p._persona_mode() and p.shadow_mode == item.shadow
                and runtime.epoch == turn.epoch
                and runtime.user_revisions.get(turn.author, 0) == turn.revision
                and not p.arbiter.is_in_deep_cooling(runtime.session_key, current_time=p.time_service.time()))

    async def submit(self, runtime, item: ModelTurn) -> None:
        if not self.valid(runtime, item):
            return
        now = self.plugin.time_service.time()
        addressed = turn_is_addressed(runtime, item.context, now)
        if runtime.model_admission.locked() and not addressed:
            self.diagnostic(runtime, "overload_ambient")
            return
        fallback = runtime.model_admission.locked()
        # Backpressure is outside state_lock. No dropped explicit request and no unbounded model tasks.
        await runtime.model_admission.acquire()
        queued = False
        try:
            async with runtime.state_lock:
                if not self.valid(runtime, item):
                    return
                runtime.model_queue.append(replace(item, fallback=fallback))
                queued = True
                if runtime.generation_task is None or runtime.generation_task.done():
                    runtime.generation_task = self.plugin._create_background_task(self.run(runtime))
        finally:
            if not queued:
                runtime.model_admission.release()

    def diagnostic(self, runtime, code: str, **extra) -> None:
        runtime.model_diagnostic = {"reason_code": code, **extra}
        self.plugin._mark_panel_runtime_dirty()

    async def decide(self, item: ModelTurn, persona, state: str) -> TurnDecision:
        p, turn = self.plugin, item.context
        if item.fallback:
            return TurnDecision.fallback(turn, "queue_overload")
        async def request():
            async with self.slots:
                provider_id = p._runtime_config.decision_provider_id or await p.llm.resolve_provider_id(turn.session_key)
                response = await p.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=DECISION_INSTRUCTIONS + "\nEffective persona:\n" + persona.prompt,
                    prompt=decision_prompt(turn, state, item.observations),
                )
                return TurnDecision.parse(completion_text(response), turn)
        try:
            # Timeout covers global admission and the single request, with no automatic retry.
            return await asyncio.wait_for(request(), timeout=p._runtime_config.decision_timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return TurnDecision.fallback(turn, "decision_timeout")
        except Exception:
            return TurnDecision.fallback(turn, "decision_invalid_or_failed")

    async def run(self, runtime) -> None:
        p = self.plugin
        task = asyncio.current_task()
        p._in_flight.add(runtime.session_key)
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
                try:
                    if self.valid(runtime, item):
                        # Final watchdog covers bridge/provider lookup, native
                        # generation and delivery, not only the decision model.
                        await asyncio.wait_for(self.process(runtime, item), timeout=p._runtime_config.tool_agent_timeout)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    self.diagnostic(runtime, "reply_timeout")
                    p._metric("llm_reply_unavailable")
                except PersonaChanged:
                    self.diagnostic(runtime, "persona_changed")
                except Exception as exc:
                    self.diagnostic(runtime, "agent_failed", error_type=type(exc).__name__)
                finally:
                    if runtime.active_model_turn is item:
                        runtime.active_model_turn = None
                    runtime.model_admission.release()
        finally:
            # This owner check and cleanup have no await: they are atomic on
            # the event loop and cannot wait on a cancelling caller's lock.
            if runtime.generation_task is task:
                runtime.generation_task = None
                p._in_flight.discard(runtime.session_key)

    async def process(self, runtime, item: ModelTurn) -> None:
        p, turn = self.plugin, item.context
        event = item.events[-1]
        started = p.time_service.time()
        persona = await self.bridge.snapshot(event)
        decision = await self.decide(item, persona, runtime.interaction_state)
        if not self.valid(runtime, item) or not await self.bridge.current(event, persona):
            return
        now = p.time_service.time()
        while runtime.ambient_openings and now - runtime.ambient_openings[0] >= 60:
            runtime.ambient_openings.popleft()
        continuation = turn_is_continuation(runtime, turn, now)
        addressed = turn_is_addressed(runtime, turn, now)
        opening = not addressed and not continuation
        if opening and len(runtime.ambient_openings) >= 2:
            decision = replace(decision, action="ignore", reason_code="ambient_budget")
        # Social manners + occasion skin (persona path): quiet degrade, never raise.
        # Quotas are committed only after a successful send, never on observe/fail.
        # Civil wall time is only for hour/day gates; interval math above stays monotonic.
        gate = None
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
                    has_media=has_media_turn,
                    media_component_types=media_types,
                    quoted_bot=bool(turn.explicit),
                    group_memory=getattr(p, "group_memory", None),
                    committed_reply=decision.action != "ignore",
                )
                runtime.last_occasion = gate.skin.as_dict()
                runtime.last_manners = gate.manners.as_dict()
                runtime.last_media_gate = gate.media.as_dict() if gate.media is not None else {}
                runtime.request_media_understand = bool(gate.request_understand)
                runtime.last_proactive = gate.proactive.as_dict() if gate.proactive is not None else {}
                runtime.last_rhythm = gate.rhythm.as_dict() if gate.rhythm is not None else {}
                if not gate.should_speak:
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
                    runtime.session_key, decision.reason_code, now=gate_now
                )
        except Exception:
            gate = None
        # Complete the trace only after the model decision and participation gates.
        for message in item.context.messages:
            node = _dag_node(runtime, message.message_id)
            trace = getattr(node, "metadata", {}).get("decision_trace")
            if isinstance(trace, dict) and isinstance(trace.get("participation"), dict):
                trace["participation"]["should_reply"] = decision.action != "ignore"

        self.diagnostic(runtime, decision.reason_code, action=decision.action, state=decision.state,
                        target_message_ids=list(decision.target_message_ids), length=decision.length,
                        latency_ms=round((now - started) * 1000), shadow=item.shadow)
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
            runtime.interaction_state = decision.state
            return
        async with self.bridge.session_lock(turn.session_key):
            if not self.valid(runtime, item) or not await self.bridge.current(event, persona):
                return
            provider = await p.llm.resolve_provider_id(turn.session_key)
            understand = bool(getattr(runtime, "request_media_understand", False))
            output = await self.bridge.generate(
                event,
                item.events,
                reply_prompt(turn, decision),
                persona,
                provider,
                execution_log=runtime.tool_executions,
                history_text=turn.text,
                media_understand=understand,
            )
            # A first request may create the host conversation; keep that exact identity for sending.
            effective = await self.bridge.snapshot(event)
            if persona.conversation_id and effective.fingerprint != persona.fingerprint:
                return
            if (effective.persona_id, effective.prompt) != (persona.persona_id, persona.prompt):
                return
            if not self.valid(runtime, item):
                return
            fragments = delivery_fragments(output.chains, output.text, p.pacer)
            rhythm_action = ""
            if gate is not None and gate.rhythm is not None:
                rhythm_action = gate.rhythm.action
            rhythm_action = rhythm_action or getattr(runtime, "last_rhythm_action", "")
            if is_rhythm_short_act(rhythm_action):
                fragments = fragments[:1]
            delivered = []
            target_id = decision.target_message_ids[0]
            dag_parent_id = target_id if _dag_node(runtime, target_id) is not None else None
            platform_parent_id = _platform_reply_id(runtime, item, target_id)
            delay_scale = float(getattr(runtime, "last_delay_scale", 1.0) or 1.0)
            if gate is not None:
                delay_scale = float(gate.delay_scale or delay_scale)
            try:
                for index, fragment in enumerate(fragments):
                    delay = (min(1.5, p.pacer.inter_burst_interval) if index else
                             max(0.0, min(1.0, p.pacer.base_thinking_delay - (p.time_service.time() - turn.started_at))))
                    delay = scale_delay(delay, delay_scale)
                    if delay:
                        await p.time_service.sleep(delay)
                    if not await self.bridge.current(event, effective):
                        break
                    async with runtime.send_lock:
                        if not self.valid(runtime, item):
                            break
                        sent = await p._send_owned(runtime, event, fragment, reply_to_id=platform_parent_id)
                    if not sent.success:
                        p._metric("send_failed")
                        break
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
                                                  metadata={"platform_message_id": real_message_id is not None})
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
                                    now=p.time_service.wall_time(),
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
