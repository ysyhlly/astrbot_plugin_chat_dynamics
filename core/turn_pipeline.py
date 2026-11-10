"""Turn preparation, enrichment and admission lifecycle.

Functions receive the runtime host explicitly. They never own a parallel session
registry; callers retain the prepare/finish state-lock boundary and model work
runs outside that lock. AstrBot hook registration remains in main.py.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any, List, Optional
from astrbot.api import logger

from .addressivity import AddressivityLevel
from .arbiter import ArbitrationResult
from .debounce import DebounceResult
from .decision_gate import GateResult
from .graph import ConversationDAG, ConversationNode
from .native_delivery import _NativeEventContext
from .outcome_recorder import STAGE_GATE, mark_not_attempted, mark_suppressed
from .persona_engine import is_request_supplement, snapshot_turn
from .platform_bridge import parse_group_event, is_poke_placeholder
from .session_runtime import PendingTurn, SessionRuntime
from .thread_router import build_contextual_query
from .turn_latency import degrade, record_stage
from .turn_limits import MAX_INPUT_CHARS as _MAX_INPUT_CHARS, MAX_TURN_CHARS as _MAX_TURN_CHARS
from .vibe_analyzer import GroupChatMode

@dataclass
class _PokeJob:
    node: Any
    event: Any
    parsed: Any
    now: float


@dataclass
class _PreparedTurn:
    """Local routing snapshot carried across optional model work."""

    result: DebounceResult
    runtime: SessionRuntime
    dag: ConversationDAG
    node: ConversationNode
    turn_nodes: tuple[ConversationNode, ...]
    parsed_events: tuple[Any, ...]
    explicit_platform: bool
    now: float
    analysis_text: str
    vibe_mode: GroupChatMode
    telemetrics: Any
    neural_ready: Any
    epoch: int
    revision: int
    owner_revision: int
    config_id: str = ''
    policy_id: str = ''
    # Small decisions asked while the lock is free and read inside it. None means
    # nothing was asked — an explicit turn skips enrichment entirely — and is the
    # same thing to a decision point as a model with no opinion.
    decisions: Any = None
    semantic_routing: Any = None


def _commit_gate_result(runtime: SessionRuntime, gate: GateResult) -> None:
    """Stage gate state; actual quota accounting happens after successful send."""
    runtime._pending_gate_skin = gate.skin
    runtime._pending_gate_proactive = gate.proactive
    runtime._pending_gate_rhythm = gate.rhythm
    runtime.last_proactive = gate.proactive.as_dict() if gate.proactive is not None else {}
    runtime.last_rhythm = gate.rhythm.as_dict() if gate.rhythm is not None else {}
    if gate.length_hint:
        runtime.last_length_hint = gate.length_hint
    if gate.delay_scale:
        runtime.last_delay_scale = gate.delay_scale
    runtime.last_rhythm_action = gate.rhythm.action if gate.rhythm is not None else ""


def _resolve_gate_result(
    host, runtime: SessionRuntime, session_id: str, arb_res: ArbitrationResult,
    gate: GateResult, wall_now: float,
) -> ArbitrationResult:
    """Apply hard blocks, gate vetoes and explicit proactive exceptions in order.

    ``wall_now`` is the civil clock, not the monotonic turn clock: the gate's
    bookkeeping (manners daily counters and why-silent stamps) rolls its day with
    ``time.localtime``, so a monotonic value resets that day to 1970 on every
    withheld turn and wipes today's counters with it.
    """
    hard_block = bool(
        arb_res.in_deep_cooling or arb_res.is_energy_asymmetric or arb_res.private_topic
    )
    whitelist_open = bool(
        gate.should_speak
        and not hard_block
        and (
            (gate.proactive is not None and bool(getattr(gate.proactive, "proactive", False)))
            or (
                gate.rhythm is not None
                and gate.rhythm.allow
                and gate.rhythm.action
                in {
                    "goodnight_reply",
                    "wake_reply",
                    "morning_hi",
                    "day_share",
                    "insomnia_line",
                }
            )
        )
    )
    if arb_res.should_speak and not gate.should_speak:
        # WTS already authorized this turn. useful_proactive's idle vetoes
        # (no gap / newcomer ambient-name) must not cancel that; manners,
        # media, rhythm, deciding and quota still win.
        if gate.reason_code in {"no_gap", "newcomer_caution"} and not hard_block:
            host._commit_gate_result(runtime, gate)
        else:
            arb_res = replace(
                arb_res, should_speak=False,
                reason=f"{gate.reason_code}: {gate.reason_zh}",
            )
            host.arbiter.remember_decision(session_id, arb_res)
    elif not arb_res.should_speak and whitelist_open:
        arb_res = replace(
            arb_res, should_speak=True,
            willingness_score=max(float(arb_res.willingness_score or 0.0), arb_res.threshold),
            reason=f"{gate.reason_code}: {gate.reason_zh}",
        )
        host.arbiter.remember_decision(session_id, arb_res)
        host._commit_gate_result(runtime, gate)
    elif not arb_res.should_speak:
        host.decision_gate.note_arbiter_silence(session_id, arb_res.reason)
    else:
        # Will speak — remember hyped quota / intervene counts after pass.
        host._commit_gate_result(runtime, gate)

    return arb_res


def _prepare_turn_locked(host, result: DebounceResult) -> _PreparedTurn | _PokeJob | None:
    """Mutate a session after its state lock has been acquired."""
    if host._shutting_down:
        return
    first_event = result.first_event
    if (getattr(first_event, "_chat_dynamics_scope_kind", "") == "private"
            or getattr(host._sessions.get(result.session_id), "scope_kind", "group") == "private"):
        return _prepare_private_turn_locked(host, result)
    if not host.debounce.is_result_current(result):
        logger.info(
            "[ChatDynamics] Ignored stale debounced turn for reset session %s",
            host._session_label(result.session_id),
        )
        return
    session_id = result.session_id
    user_id = result.user_id
    text = host._bounded_text(result.consolidated_text, _MAX_TURN_CHARS)
    analysis_text = host._bounded_text(text, _MAX_INPUT_CHARS)
    if text != result.consolidated_text or result.metadata.get("truncated"):
        host._metric("turn_truncated")
    now = host.time_service.time()
    last_event = result.last_event
    parsed_last = parse_group_event(last_event) if last_event is not None else None
    parsed_events = []
    turn_mentions: List[str] = []
    is_wake = False
    silence_seen = False

    runtime = host._sessions.get(session_id)
    if runtime is not None and any(
            getattr(raw, "_chat_dynamics_epoch", runtime.epoch) != runtime.epoch
            for raw in result.raw_events):
        host._metric("stale_turn_ignored")
        return
    for raw in result.raw_events:
        parsed = parse_group_event(raw)
        try:
            from . import member_boundary
            policy, boundary_token, boundary_state = member_boundary.peek(host, raw, parsed.sender_id)
            if not boundary_state.available and member_boundary.fail_closed(boundary_state):
                logger.warning(
                    "[ChatDynamics] boundary fail-closed session=%s message_id=%s reason=boundary_service_unavailable",
                    result.session_id, str(getattr(parsed, "message_id", "") or ""),
                )
                if hasattr(raw, "stop_event"):
                    raw.stop_event()
                host._block_native_for_mode(raw)
                host._metric("boundary_unavailable")
                return
            if member_boundary.blocked(policy, purpose="reply"):
                mode = str((policy or {}).get("mode", "")).lower()
                if mode == "shield":
                    host._block_native_for_mode(raw)
                    host._metric("boundary_shield_queued_turn")
                    return
                silence_seen = True
            setattr(raw, "_chat_dynamics_boundary_uid", parsed.sender_id)
            setattr(raw, "_chat_dynamics_boundary_token", boundary_token)
            if getattr(raw, "_chat_dynamics_boundary_policy_snapshot", None) is None:
                snapshot, snapshot_state = member_boundary.capture_policy_snapshot(host, raw)
                if not snapshot_state.available and (member_boundary.fail_closed(snapshot_state)
                                                     or snapshot_state.service is not None):
                    logger.warning(
                        "[ChatDynamics] boundary fail-closed session=%s message_id=%s reason=boundary_policy_snapshot_unavailable",
                        result.session_id, str(getattr(parsed, "message_id", "") or ""),
                    )
                    host._block_native_for_mode(raw)
                    host._metric("boundary_unavailable")
                    return
                if snapshot_state.available and not snapshot_state.disabled and snapshot is not None:
                    setattr(raw, "_chat_dynamics_boundary_policy_snapshot", snapshot)
        except Exception as exc:
            if getattr(getattr(host, "_runtime_config", None), "autonomous_blocking_required", False):
                logger.warning(
                    "[ChatDynamics] boundary fail-closed session=%s message_id=%s reason=boundary_service_error type=%s",
                    result.session_id, str(getattr(parsed, "message_id", "") or ""), type(exc).__name__,
                )
                host._block_native_for_mode(raw)
                host._metric("boundary_unavailable")
                return
        if runtime is None:
            runtime = host._get_or_create_runtime(
                session_id,
                group_id=parsed.group_id or session_id,
                umo=parsed.unified_msg_origin or session_id,
                bot_id=parsed.self_id,
            )
        elif parsed.self_id:
            runtime.bot_id = parsed.self_id
        parsed_events.append(parsed)
        for mention in parsed.platform_mentions:
            if mention not in turn_mentions:
                turn_mentions.append(mention)
        if host._looks_like_strong_address(parsed, runtime):
            is_wake = True

    if runtime is None:
        runtime = host._get_or_create_runtime(session_id, group_id=session_id, umo=session_id)
    if parsed_last and parsed_last.self_id:
        runtime.bot_id = parsed_last.self_id

    dag = runtime.dag
    if dag is None:
        raise RuntimeError("session DAG is unavailable")
    host._mark_panel_runtime_dirty()
    runtime.turn_sequence += 1
    turn_id = f"turn_{runtime.turn_sequence}_{user_id}"
    turn_nodes: List[ConversationNode] = []
    previous_node: Optional[ConversationNode] = None
    for index, parsed in enumerate(parsed_events):
        item = result.messages[index] if index < len(result.messages) else None
        fragment_text = host._bounded_text(
            (getattr(item, "text", "") or parsed.text or "").strip(),
            _MAX_INPUT_CHARS,
        )
        fragment_time = float(getattr(item, "timestamp", now) or now)
        fragment_id = parsed.message_id or getattr(item, "node_id", "") or f"{turn_id}_{index}"
        if item is not None:
            item.node_id = fragment_id
        actual_mentions = list(dict.fromkeys(parsed.platform_mentions))
        current_node = dag.add_message(
            msg_id=fragment_id,
            user_id=parsed.sender_id or user_id,
            text=fragment_text,
            timestamp=fragment_time,
            reply_to_id=parsed.reply_to_id,
            mentioned_users=actual_mentions,
            metadata={
                "turn_id": turn_id,
                "turn_index": index,
                "topic_source_text": parsed.text or "",
                "message_count": result.message_count,
                "duration": result.duration,
                "is_wake": host._looks_like_strong_address(parsed, runtime),
                "host_is_at_or_wake": parsed.is_at_or_wake,
                "poke_at_bot": bool(parsed.has_poke and parsed.poke_at_bot),
                "actual_mentions": actual_mentions,
                "quoted_author_id": getattr(parsed, "reply_sender_id", ""),
                "quoted_author_name": parsed.reply_sender_name,
                "sender_name": parsed.sender_name,
                "sender_platform": parsed.platform,
                "platform_message_id": bool(parsed.message_id),
            },
        )
        current_node.metadata["platform_message_id"] = bool(parsed.message_id)
        if previous_node is not None and not parsed.reply_to_id:
            dag.link_related(current_node.msg_id, previous_node.msg_id)
        turn_nodes.append(current_node)
        previous_node = current_node

    if silence_seen:
        # Silence keeps the source in the conversation DAG for observation,
        # while bypassing routing, recall, decision, and every reply fallback.
        host._metric("boundary_silence_observed")
        return None

    if not turn_nodes:
        node = dag.add_message(
            msg_id=f"{turn_id}_0",
            user_id=user_id,
            text=text,
            timestamp=now,
            mentioned_users=turn_mentions,
            metadata={
                "turn_id": turn_id,
                "message_count": result.message_count,
                "is_wake": is_wake,
                "platform_message_id": False,
            },
        )
        node.metadata["platform_message_id"] = False
    else:
        node = turn_nodes[-1]
        # Addressivity is evaluated for the complete debounced turn, so
        # carry forward pointers that appeared in an earlier fragment.
        node.mentioned_users = list(dict.fromkeys(node.mentioned_users + turn_mentions))
        node.metadata["is_wake"] = is_wake
        node.metadata["consolidated_text"] = text

    poke_events = [item for item in parsed_events if getattr(item, "has_poke", False)]
    poke_only = bool(parsed_events) and all(
        getattr(item, "has_poke", False)
        and not getattr(item, "has_media", False)
        and is_poke_placeholder(getattr(item, "text", "") or "")
        for item in parsed_events
    )
    poke_at_bot = any(getattr(item, "poke_at_bot", False) for item in poke_events)
    # Kev-only mode sends a poke through the same persona turn as any other
    # message.  The old poke policy could otherwise authorize a reply or a
    # poke-back without consulting the decision model.
    if poke_only:
        if last_event is not None and not host.shadow_mode:
            if poke_at_bot:
                host._claim_poke_event(last_event)
            else:
                host._block_native_for_mode(last_event)
        if not poke_at_bot:
            return None
        if not host._persona_mode():
            # Legacy mode keeps the local poke policy.
            return _PokeJob(node=node, event=last_event, parsed=poke_events[-1], now=now)
        # persona_model: fall through so the Kev decision and persona engine
        # handle a poke-at-bot exactly like any other turn.

    atmosphere = host.vibe_analyzer.get_atmosphere(session_id, current_time=now)
    vibe_mode = host.vibe_analyzer.get_mode(session_id, current_time=now)
    telemetrics = atmosphere.energy
    runtime.vibe_message_count += 1
    host._vibe_msg_counts[session_id] = runtime.vibe_message_count
    if not host.shadow_mode:
        # Both decision modes dispatch the LLM-request hook, so the partner's
        # approved memories are warmed here for either of them.
        host._schedule_mood_recall(session_id, user_id)

    # Resolve all fragments before constructing the immutable persona snapshot.
    for turn_node in turn_nodes or [node]:
        host._route_message(runtime, turn_node)
    task = host._schedule_neural_embed(session_id, node)
    for turn_node in turn_nodes:
        if turn_node is not node:
            host._schedule_neural_embed(session_id, turn_node)
    explicit_platform = any(host._looks_like_strong_address(p, runtime) for p in parsed_events)
    routing = node.metadata.get("routing", {})
    neural_ready = None
    if (task is not None and not explicit_platform
            and getattr(host._runtime_config, "conversation_router_enabled", True)
            and (routing.get("ambiguous") or len(node.text.strip()) <= 16)):
        neural_ready = getattr(task, "routing_ready", None)
    return _PreparedTurn(
        result=result, runtime=runtime, dag=dag, node=node,
        turn_nodes=tuple(turn_nodes), parsed_events=tuple(parsed_events),
        explicit_platform=explicit_platform, now=now, analysis_text=analysis_text,
        vibe_mode=vibe_mode, telemetrics=telemetrics, neural_ready=neural_ready,
        epoch=runtime.epoch, revision=runtime.revision,
        owner_revision=runtime.user_revisions.get(str(user_id or ""), 0),
        config_id=host._turn_config_identity(), policy_id=host._turn_policy_identity(),
    )


def _prepare_private_turn_locked(host, result: DebounceResult):
    """Build a private direct-reply turn without group routing or Jev state."""
    if not host._persona_mode() or not host.debounce.is_result_current(result):
        return None
    raw_events = tuple(result.raw_events or ())
    if not raw_events:
        return None
    session_id = str(result.session_id)
    runtime = host._sessions.get(session_id)
    parsed_events = tuple(parse_group_event(event) for event in raw_events)
    if (any(parsed.group_id for parsed in parsed_events)
            or len(parsed_events) != len(result.messages)):
        return None
    if runtime is None:
        first = parsed_events[0]
        runtime = host._get_or_create_runtime(
            session_id, group_id="", umo=first.unified_msg_origin or session_id,
            bot_id=first.self_id, scope_kind="private",
        )
    if runtime.scope_kind != "private" or not host.is_runtime_scope_enabled(runtime):
        return None
    if any(getattr(event, "_chat_dynamics_epoch", runtime.epoch) != runtime.epoch
           for event in raw_events):
        host._metric("stale_turn_ignored")
        return None

    # Recheck member policy after debounce: a queued private message can outlive
    # a boundary change. Private turns do not contribute group learning rows.
    from . import member_boundary
    silence_seen = False
    for event, parsed in zip(raw_events, parsed_events):
        state = member_boundary.resolve_state(host, event)
        if not state.available and member_boundary.fail_closed(state):
            logger.warning(
                "[ChatDynamics] boundary fail-closed session=%s message_id=%s reason=boundary_service_unavailable mode=private",
                session_id, str(getattr(parsed, "message_id", "") or ""),
            )
            host._stop_boundary_event(event)
            host._metric("boundary_unavailable")
            return None
        policy, token, state = member_boundary.peek(host, event, parsed.sender_id)
        if not state.available and member_boundary.fail_closed(state):
            logger.warning(
                "[ChatDynamics] boundary fail-closed session=%s message_id=%s reason=boundary_service_unavailable mode=private",
                session_id, str(getattr(parsed, "message_id", "") or ""),
            )
            host._stop_boundary_event(event)
            host._metric("boundary_unavailable")
            return None
        if member_boundary.blocked(policy, purpose="reply"):
            if str((policy or {}).get("mode", "")).lower() == "shield":
                host._stop_boundary_event(event)
                host._metric("boundary_shield_queued_turn")
                return None
            silence_seen = True
        captured = getattr(event, "_chat_dynamics_boundary_token", None)
        if captured is not None and token != captured:
            host._metric("stale_turn_ignored")
            return None

    media_types = [kind for parsed in parsed_events
                   for kind in (getattr(parsed, "media_component_types", ()) or ())]
    has_media = any(bool(getattr(parsed, "has_media", False)) for parsed in parsed_events)
    runtime.request_media_understand = False
    if has_media:
        try:
            verdict = host.decision_gate.media.evaluate(
                text=host._bounded_text(result.consolidated_text, _MAX_TURN_CHARS),
                has_media=True, media_component_types=media_types,
                outline=" ".join(str(getattr(parsed, "outline", "") or "") for parsed in parsed_events),
                explicit=True, quoted_bot=any(bool(getattr(parsed, "reply_to_id", "")) for parsed in parsed_events),
                media_privacy_strict=host._runtime_config.media_privacy_strict,
                media_image_gate_enabled=host._runtime_config.media_image_gate_enabled,
                media_voice_gate_enabled=host._runtime_config.media_voice_gate_enabled,
                media_understand_reply_enabled=host._runtime_config.media_understand_reply_enabled,
            )
            runtime.last_media_gate = verdict.as_dict()
            runtime.request_media_understand = bool(verdict.request_understand)
            if verdict.privacy_hit:
                host._metric("private_media_privacy_blocked")
                return None
        except Exception as exc:
            host._metric("private_media_gate_unavailable")
            logger.warning("private media gate unavailable code=CD_PRIVATE_MEDIA_GATE type=%s",
                           type(exc).__name__)
            return None

    dag = runtime.dag
    if dag is None:
        raise RuntimeError("private session DAG is unavailable")
    runtime.touch(host.time_service.time())
    runtime.turn_sequence += 1
    turn_id = f"private_{runtime.turn_sequence}_{result.user_id}"
    turn_nodes = []
    previous = None
    for index, (parsed, item) in enumerate(zip(parsed_events, result.messages)):
        text = host._bounded_text(getattr(item, "text", "") or parsed.text or "", _MAX_INPUT_CHARS)
        msg_id = parsed.message_id or getattr(item, "node_id", "") or f"{turn_id}_{index}"
        item.node_id = msg_id
        node = dag.add_message(
            msg_id=msg_id, user_id=parsed.sender_id or result.user_id, text=text,
            timestamp=float(getattr(item, "timestamp", host.time_service.time()) or host.time_service.time()),
            reply_to_id=parsed.reply_to_id, mentioned_users=list(dict.fromkeys(parsed.platform_mentions)),
            metadata={
                "turn_id": turn_id, "turn_index": index, "topic_source_text": parsed.text or "",
                "message_count": result.message_count, "is_wake": False,
                "actual_mentions": list(dict.fromkeys(parsed.platform_mentions)),
                "quoted_author_id": parsed.reply_sender_id, "quoted_author_name": parsed.reply_sender_name,
                "sender_name": parsed.sender_name, "sender_platform": parsed.platform,
                "platform_message_id": bool(parsed.message_id), "scope_kind": "private",
                "decision_source": "private_direct",
            },
        )
        if previous is not None and not parsed.reply_to_id:
            dag.link_related(node.msg_id, previous.msg_id)
        turn_nodes.append(node)
        previous = node
    if not turn_nodes:
        return None
    node = turn_nodes[-1]
    node.metadata["consolidated_text"] = host._bounded_text(result.consolidated_text, _MAX_TURN_CHARS)
    host._mark_panel_runtime_dirty()
    if silence_seen:
        host._metric("boundary_silence_observed")
        return None
    if not host.shadow_mode:
        host._schedule_mood_recall(session_id, result.user_id)
    item = snapshot_turn(
        runtime, result, [turn_node.msg_id for turn_node in turn_nodes], parsed_events,
        True, {"scope": "private", "decision_source": "direct"}, host.shadow_mode,
        wake_kind="private", boundary_host=host,
    )
    return replace(item, private_direct=True) if item is not None else None


def _prepared_turn_current(host, turn: _PreparedTurn) -> bool:
    runtime = turn.runtime
    return bool(
        not host._shutting_down
        and host._sessions.get(turn.result.session_id) is runtime
        and host.debounce.is_result_current(turn.result)
        and runtime.epoch == turn.epoch
        and runtime.revision == turn.revision
        and runtime.user_revisions.get(str(turn.result.user_id or ""), 0) == turn.owner_revision
        and runtime.dag is turn.dag
        and turn.dag.get_node(turn.node.msg_id) is turn.node
        and (not turn.config_id or turn.config_id == host._turn_config_identity())
        and (not turn.policy_id or turn.policy_id == host._turn_policy_identity())
    )




async def _enrich_turn(host, turn: _PreparedTurn, *, allow_llm: bool = True) -> None:
    if turn.explicit_platform:
        return
    # Reuse the largest configured stage allowance as the entire enrichment
    # allowance: serial stages do not each receive a new full deadline.
    cfg = host._runtime_config
    if getattr(cfg, 'rule_topic_routing_enabled', False):
        if getattr(cfg, 'discussion_judgment_mode', 'rerank') == 'llm':
            judge = host._topic_reranker()
            if judge is not None and not turn.node.metadata.get('routing', {}).get('discussion_rank_attempted'):
                config_id = host._turn_config_identity()
                started = time.perf_counter()
                try:
                    await host.thread_router.rerank_pending(
                        turn.runtime, turn.node, judge,
                        is_current=lambda: (host._prepared_turn_current(turn)
                                            and host._turn_config_identity() == config_id))
                finally:
                    record_stage(turn.node, 'discussion_llm', started)
        # Object rules and native discussion ranking do not consume embeddings.
        # Keep warmups alive for the independent addressivity/memory consumers.
        host._metric('routing_embedding_wait_skipped')
        return
    allowance = max(float(cfg.routing_neural_timeout),
                    float(cfg.topic_reranker_timeout) if cfg.topic_reranker_enabled else 0.0)
    deadline = time.perf_counter() + max(0.0, allowance)
    started = time.perf_counter()
    if turn.neural_ready is not None:
        wait_started = time.perf_counter()
        try:
            await asyncio.wait_for(
                turn.neural_ready.wait(),
                timeout=max(0.0, min(float(cfg.routing_neural_timeout), deadline - time.perf_counter())),
            )
        except asyncio.TimeoutError:
            degrade(turn.node, 'embedding_timeout')
            # asyncio's timer resolution can wake a fraction before perf_counter
            # reaches the numerical deadline. If this stage owned the entire
            # allowance, its timeout is authoritative: do not start another call.
            if float(cfg.routing_neural_timeout) >= allowance:
                degrade(turn.node, 'enrichment_budget_exhausted')
                record_stage(turn.node, 'enrichment', started)
                return
        finally:
            record_stage(turn.node, 'embedding_wait', wait_started)
    async with turn.runtime.state_lock:
        if not host._prepared_turn_current(turn):
            return
        if turn.neural_ready is not None:
            query = build_contextual_query(turn.node, turn.dag)
            if host.embeddings.cached(query) is not None:
                host._route_message(turn.runtime, turn.node)
        reranker = host._topic_reranker() if allow_llm else None
    if reranker is not None:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            degrade(turn.node, 'enrichment_budget_exhausted')
            record_stage(turn.node, 'enrichment', started)
            return
        wait_started = time.perf_counter()
        commit_allowed = True
        task = host._create_background_task(host.thread_router.rerank_pending(
            turn.runtime, turn.node, reranker,
            is_current=lambda: commit_allowed and host._prepared_turn_current(turn) and time.perf_counter() < deadline))
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if task in done:
                task.result()
            else:
                degrade(turn.node, 'enrichment_budget_exhausted')
        finally:
            commit_allowed = False
            if not task.done():
                task.cancel()
            record_stage(turn.node, 'rerank_wait', wait_started)
        host._mark_panel_runtime_dirty()
    record_stage(turn.node, 'enrichment', started)


async def _enrich_topic_background(host, turn: _PreparedTurn) -> None:
    """Optional display enrichment never delays a reply or outlives reset."""
    host._track_hook_task(turn.result.session_id)
    runtime = turn.runtime
    async with runtime.state_lock:
        if (host._shutting_down or host._sessions.get(turn.result.session_id) is not runtime
                or runtime.epoch != turn.epoch or runtime.dag is not turn.dag
                or turn.dag.get_node(turn.node.msg_id) is not turn.node):
            return
        host.topic_batches.enqueue(runtime, turn.turn_nodes or (turn.node,))
    if host.debounce.semantic and not turn.explicit_platform:
        await _enrich_turn(host, turn, allow_llm=False)


def _finish_turn_locked(host, turn: _PreparedTurn) -> Any:
    if turn.semantic_routing and host._prepared_turn_current(turn):
        from .decision_routing import apply_routing_answers
        apply_routing_answers(turn, *turn.semantic_routing)
    result, runtime, dag, node = turn.result, turn.runtime, turn.dag, turn.node
    session_id, user_id = result.session_id, result.user_id
    turn_nodes, parsed_events = turn.turn_nodes, turn.parsed_events
    explicit_platform, now = turn.explicit_platform, turn.now
    analysis_text, vibe_mode, telemetrics = turn.analysis_text, turn.vibe_mode, turn.telemetrics
    last_event = result.last_event
    last_bot_node = runtime.last_bot_node
    if last_bot_node is not None and last_bot_node.timestamp > node.timestamp:
        last_bot_node = next((n for n in reversed(dag.get_recent_nodes(0))
                             if n.timestamp <= node.timestamp and n.user_id == runtime.bot_id), None)
    runtime.expire_hovers(now)
    evidence_context = dict(session_id=session_id, message_id=node.msg_id, epoch=runtime.epoch,
                                config_id=host._turn_config_identity(), policy_id=host._turn_policy_identity(),
                                visible_before=node.timestamp, turn_id=node.metadata.get('turn_id', node.msg_id),
                                revision=runtime.revision, owner_revision=turn.owner_revision)
    addressivity = host.addressivity_router.compute_addressivity(
        node=node,
        dag=dag,
        last_bot_node=last_bot_node,
        bot_id=runtime.bot_id,
        prior_hover=runtime.pending_hover,
        prior_hovers=list(runtime.pending_hovers),
        semantic_match_fn=host.embeddings.match,
        evidence_context=evidence_context,
    )

    from .bot_identity import BotIdentityMatcher
    from .routing_trace import build_routing_trace, compact_trace_inputs, finalize_decision_trace
    from .routing_contract import ROUTING_WEIGHTS_VERSION
    identity = BotIdentityMatcher.match(node.text, host.bot_names,
        mentions=node.mentioned_users, bot_id=runtime.bot_id)
    dialogue = runtime.active_dialogue
    trace_recent = [n for n in dag.get_recent_nodes(0) if n.timestamp <= node.timestamp][-80:]
    evidence = getattr(addressivity, 'turn_evidence', None)
    if evidence is not None and not evidence.is_current(**{k: evidence_context[k] for k in
            ('session_id', 'message_id', 'turn_id', 'epoch', 'revision', 'owner_revision', 'config_id', 'policy_id')}):
        host._metric('stale_turn_ignored')
        return
    node.metadata["decision_trace"] = build_routing_trace(
        routing=node.metadata.get("routing", {}), identity=identity,
        participation={"score": addressivity.score, "level": addressivity.level,
                       "should_reply": None,
                       "evidence": addressivity.evidence,
                       "family_contributions": addressivity.family_contributions,
                       "contribution_total": addressivity.contribution_total},
        state={"pending_hover": bool(runtime.pending_hover),
               "active_interlocutor": dialogue.user_id if dialogue else runtime.last_interlocutor,
               "last_bot_message_id": dialogue.last_bot_message_id if dialogue else None,
               "last_bot_was_question": dialogue.last_bot_was_question if dialogue else None,
               "waiting_for_answer": dialogue.accepts_answer(node, trace_recent) if dialogue else False,
               "intervening_users": len({n.user_id for n in trace_recent
                   if last_bot_node is not None and last_bot_node.timestamp < n.timestamp < node.timestamp
                   and n.user_id not in {runtime.bot_id, node.user_id}})},
        mode="persona" if host._persona_mode() else "legacy",
        weights_version=ROUTING_WEIGHTS_VERSION,
        shadow=None)
    if evidence is not None:
        node.metadata['decision_trace']['turn'] = evidence.identity()
    # Every processed turn starts as "the reply flow was never entered" and
    # is upgraded as the turn progresses. Recording a default beats leaving
    # the field out: "we did not try" and "we never found out" are different
    # facts, and only a written one can be counted.
    # The frozen trace lives only in memory, so the sections a rebuild needs are
    # kept separately for the panel snapshot: an annotation saved after a
    # restart would otherwise record a participation block of nulls, and that
    # block is the learning layer's only basis for threshold replay.
    node.metadata["trace_inputs"] = compact_trace_inputs(node.metadata["decision_trace"])
    mark_not_attempted(node)

    if host._persona_mode():
        kinds = {host._wake_kind(parsed, runtime) for parsed in parsed_events}
        # The combined host flag may be a rejected nickname overlap. Only a
        # classified wake or a real pending request can authorize continuation.
        explicit = bool(kinds & {"at", "quote", "name", "poke"})
        if not explicit and not result.metadata.get("peer_directed") and is_request_supplement(runtime, user_id, now):
            # Same-user addendum to a recent @/wake request is not ambient chatter.
            explicit = True
        observations = {"mpm": telemetrics.mpm, "mode": vibe_mode.value,
                        "addressivity": addressivity.score, "scene_tags": list(telemetrics.scene_tags)}
        canonical_ids = tuple(turn_node.msg_id for turn_node in turn_nodes) or (node.msg_id,)
        wake_kind = next((kind for kind in ("at", "quote", "name", "poke") if kind in kinds),
                         "supplement" if explicit else "none")
        return snapshot_turn(
            runtime,
            result,
            canonical_ids,
            parsed_events,
            explicit,
            observations,
            host.shadow_mode,
            wake_kind=wake_kind,
            boundary_host=host,
        )

    logger.debug(
        "[ChatDynamics] Session %s turn flushed: vibe=%s addressivity=%s (%.2f)",
        host._session_label(session_id),
        vibe_mode.value,
        addressivity.level.value,
        addressivity.score,
    )

    if not host.shadow_mode:
        host.arbiter.maybe_auto_cool(
            session_id=session_id,
            vibe_mode=vibe_mode,
            telemetrics=telemetrics,
            current_time=now,
        )

    # The thread identity is what makes the energy gate "same-thread": without it
    # every message from the last interlocutor counted as continuing the bot's
    # own conversation, and an off-topic "6" could be scored as a dying exchange.
    arb_res = host.arbiter.evaluate(
        session_id=session_id,
        addressivity=addressivity,
        telemetrics=telemetrics,
        vibe_mode=vibe_mode,
        user_id=user_id,
        text=analysis_text,
        topic_id=str((node.metadata.get("routing") or {}).get("topic_id") or ""),
        parent_id=str(node.reply_to_id or ""),
        runtime=runtime,
        current_time=now,
        allow_ambient=host._allow_ambient(),
        decisions=getattr(turn, "decisions", None),
        decision_floor=float(0.6),
    )

    explicit_addr = addressivity.level == AddressivityLevel.STRONG
    if not explicit_addr and not result.metadata.get("peer_directed") and is_request_supplement(runtime, user_id, now):
        explicit_addr = True
    recent_nodes = dag.get_recent_nodes(limit=12) if dag is not None and hasattr(dag, "get_recent_nodes") else []
    media_types = []
    outline_bits = []
    for pe in parsed_events:
        media_types.extend(list(getattr(pe, "media_component_types", None) or []))
        if getattr(pe, "outline", None):
            outline_bits.append(str(pe.outline))
    has_media_turn = any(bool(getattr(pe, "has_media", False)) for pe in parsed_events) or bool(media_types)
    quoted_bot = False
    try:
        bot = str(getattr(runtime, "bot_id", "") or "")
        if bot and last_bot_node is not None and str(getattr(node, "reply_to_id", "") or "") == str(
            getattr(last_bot_node, "msg_id", "") or ""
        ):
            quoted_bot = True
        if bot and bot in list(getattr(node, "mentioned_users", None) or []):
            quoted_bot = True
    except Exception:
        quoted_bot = False
    gate = host.decision_gate.evaluate(
        session_id=session_id,
        user_id=str(user_id or ""),
        text=analysis_text,
        vibe_mode=vibe_mode,
        telemetrics=telemetrics,
        recent_nodes=recent_nodes,
        bot_id=str(getattr(runtime, "bot_id", "") or ""),
        explicit=explicit_addr,
        willingness=float(getattr(arb_res, "willingness_score", 0.0) or 0.0),
        cfg=host._runtime_config,
        decisions=getattr(turn, "decisions", None),
        decision_floor=float(0.6),
        now=host.time_service.wall_time(),
        node_now=now,
        has_media=has_media_turn,
        media_component_types=media_types,
        outline=" ".join(outline_bits),
        quoted_bot=quoted_bot,
        group_memory=getattr(host, "group_memory", None),
        committed_reply=False,
    )
    runtime.last_occasion = gate.skin.as_dict()
    runtime.last_manners = gate.manners.as_dict()
    runtime.last_media_gate = gate.media.as_dict() if gate.media is not None else {}
    runtime.request_media_understand = bool(gate.request_understand)
    # Gate bookkeeping runs on the civil clock while the turn clock is monotonic
    # (node timestamps, cooldowns). Passing the turn clock here made every
    # withheld turn roll the manners day bucket back to 1970.
    arb_res = host._resolve_gate_result(
        runtime, session_id, arb_res, gate, host.time_service.wall_time())
    node.metadata["decision_trace"] = finalize_decision_trace(
        node.metadata["decision_trace"], should_reply=bool(arb_res.should_speak),
        branch=('explicit_platform:' if explicit_platform else 'policy:') + str(arb_res.reason))
    node.metadata["trace_inputs"] = compact_trace_inputs(node.metadata["decision_trace"])

    runtime.commit_participation(node, addressivity.level, now)

    native_pipeline = bool(result.metadata.get("native_pipeline"))
    if host.shadow_mode:
        predicted_action = (
            "suppress"
            if not arb_res.should_speak
            else ("native_pass" if native_pipeline else "generate")
        )
        host._record_shadow_decision(
            session_id,
            action=predicted_action,
            reason=arb_res.reason,
            decision=arb_res,
            timestamp=now,
        )
        host._metric("shadow_decision")
        # Shadow mode answers "what would the gate have decided" and then
        # does nothing, so the turn is suppressed by *this* plugin rather
        # than by the gate. Saying so keeps the reason honest: the learning
        # layer reads every reason it does not recognise as unclassified
        # instead of filing it under the gate.
        mark_suppressed(node, "shadow_mode", stage=STAGE_GATE, now=now)
        host._clear_pending_gate(runtime)
        return
    if not arb_res.should_speak:
        logger.info(
            "[ChatDynamics] Speech withheld for session %s: %s",
            host._session_label(session_id),
            arb_res.reason,
        )
        mark_suppressed(node, arb_res.reason, stage=STAGE_GATE, now=now)
        host._metric("speech_withheld")
        if native_pipeline:
            host._suppress_native_llm(last_event)
        return

    if native_pipeline:
        runtime.native_trigger_node = node
        runtime.native_vibe_mode = vibe_mode
        if last_event is not None:
            host._set_native_context(
                (session_id, id(last_event)),
                _NativeEventContext(
                    event=last_event,
                    trigger_node=node,
                    vibe_mode=vibe_mode,
                    epoch=runtime.epoch,
                    owner_user_id=str(user_id or ""),
                    owner_revision=runtime.user_revisions.get(str(user_id or ""), 0),
                ),
                overwrite=True,
            )
            try:
                setattr(last_event, "_chat_dynamics_epoch", runtime.epoch)
            except Exception:
                pass
        host._metric("native_pass")
        logger.info(
            "[ChatDynamics] Native pipeline released for session %s: %s",
            host._session_label(session_id),
            arb_res.reason,
        )
        return

    generation_active = runtime.generation_task is not None and not runtime.generation_task.done()
    if generation_active:
        existing_strong = runtime.generation_level == AddressivityLevel.STRONG or (
            runtime.latest_pending is not None
            and runtime.latest_pending.addressivity_level == AddressivityLevel.STRONG
        )
        if existing_strong and addressivity.level != AddressivityLevel.STRONG:
            logger.info(
                "[ChatDynamics] Kept STRONG turn; ignored weaker follow-up for %s",
                host._session_label(session_id),
            )
            return
    # Only accepted replacements may invalidate the in-flight response.
    runtime.revision += 1
    pending = PendingTurn(
        result=result,
        revision=runtime.revision,
        node=node,
        vibe_mode=vibe_mode,
        raw_event=last_event,
        addressivity_level=addressivity.level,
        epoch=runtime.epoch,
        owner_user_id=str(user_id or ""),
        owner_revision=runtime.user_revisions.get(str(user_id or ""), 0),
    )
    if generation_active:
        runtime.latest_pending = pending
        logger.info(
            "[ChatDynamics] Replaced pending speech for session %s",
            host._session_label(session_id),
        )
        return
    logger.info(
        "[ChatDynamics] Speech approved for session %s: %s",
        host._session_label(session_id),
        arb_res.reason,
    )
    runtime.generation_level = addressivity.level
    task = host._create_background_task(host._run_generation_loop(runtime, pending))
    runtime.generation_task = task
