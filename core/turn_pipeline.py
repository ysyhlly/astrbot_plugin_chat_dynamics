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
from .runtime_persistence import host_version as _host_plugin_version
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

    runtime = host._sessions.get(session_id)
    for raw in result.raw_events:
        parsed = parse_group_event(raw)
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
        for mention in parsed.mentions:
            if mention not in turn_mentions:
                turn_mentions.append(mention)
        if parsed.is_at_or_wake:
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
        fragment_id = parsed.message_id or f"{turn_id}_{index}"
        actual_mentions = list(dict.fromkeys(parsed.mentions))
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
                "is_wake": parsed.is_at_or_wake,
                "actual_mentions": actual_mentions,
                "quoted_author_id": getattr(parsed, "reply_sender_id", ""),
                "platform_message_id": bool(parsed.message_id),
            },
        )
        current_node.metadata["platform_message_id"] = bool(parsed.message_id)
        if previous_node is not None and not parsed.reply_to_id:
            dag.link_related(current_node.msg_id, previous_node.msg_id)
        turn_nodes.append(current_node)
        previous_node = current_node

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
    if poke_only:
        if last_event is not None and not host.shadow_mode:
            if poke_at_bot:
                host._claim_poke_event(last_event)
            else:
                host._block_native_for_mode(last_event)
        if poke_at_bot:
            return _PokeJob(node=node, event=last_event, parsed=poke_events[-1], now=now)
        return None

    atmosphere = host.vibe_analyzer.get_atmosphere(session_id, current_time=now)
    vibe_mode = host.vibe_analyzer.get_mode(session_id, current_time=now)
    telemetrics = atmosphere.energy
    runtime.vibe_message_count += 1
    host._vibe_msg_counts[session_id] = runtime.vibe_message_count
    if not host.shadow_mode and not host._persona_mode():
        host._schedule_vibe_llm(session_id, analysis_text, now)
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


async def _fill_turn_decisions(host, turn: _PreparedTurn, deadline: float) -> None:
    """Ask the turn's small decisions while the session lock is free.

    One bounded request covers every question `_finish_turn_locked` will want, and
    the decision points there read the result with a plain lookup. Laya answers the
    whole batch in a single forward pass, so gathering them here is what keeps the
    synchronous step free of network calls instead of trading one blocking call for
    three.

    Every way this can fail leaves the slots empty rather than wrong: no client, no
    budget left in the enrichment allowance, a transport that could not answer, an
    answer that cannot carry a decision. The decision points then keep the
    heuristics they already had.
    """
    client = getattr(host, "laya", None)
    if client is None:
        return
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        degrade(turn.node, 'enrichment_budget_exhausted')
        return
    from .turn_decisions import TurnDecisions

    decisions = TurnDecisions.for_turn()
    turn.decisions = decisions
    started = time.perf_counter()
    try:
        learning = getattr(host, "decision_learning", None)
        if learning is not None and learning.enabled(turn.result.session_id):
            from .decision_routing import build_routing_tasks, message_snapshot
            routing_state, routing_questions, mapping = build_routing_tasks(turn)
            context = [message_snapshot(n) for n in list(turn.dag.nodes.values())
                       if n is not turn.node and n.timestamp <= turn.node.timestamp][-12:]
            answers = await learning.evaluate(session_id=turn.result.session_id,
                state={"text": turn.analysis_text, "recent_messages": context,
                       "current": message_snapshot(turn.node),
                       "routing": turn.node.metadata.get("routing", {}), "routing_semantics": routing_state},
                questions={**decisions.questions, **routing_questions}, timeout=remaining, outcome_node=turn.node)
            decisions.source = "decision_learning"
            turn.semantic_routing = (answers or {}, mapping)
        else:
            answers = await client.evaluate(
            # The opinions are about the message itself, which is exactly what the
            # regex heuristics they replace read. Room physics stays out of it.
                state={"text": turn.analysis_text},
                questions=decisions.questions,
                timeout=min(float(host._runtime_config.laya_timeout), remaining),
            )
    finally:
        record_stage(turn.node, 'small_decisions', started)
    decisions.ingest(answers)


async def _enrich_turn(host, turn: _PreparedTurn) -> None:
    if turn.explicit_platform:
        return
    # Reuse the largest configured stage allowance as the entire enrichment
    # allowance: serial stages do not each receive a new full deadline.
    cfg = host._runtime_config
    learning = getattr(host, "decision_learning", None)
    learning_enabled = learning is not None and learning.enabled(turn.result.session_id)
    allowance = max(float(cfg.routing_neural_timeout),
                    float(cfg.topic_reranker_timeout) if cfg.topic_reranker_enabled else 0.0)
    if learning_enabled:
        allowance = max(allowance, float(cfg.decision_timeout))
    deadline = time.perf_counter() + max(0.0, allowance)
    started = time.perf_counter()
    if not learning_enabled:
        await _fill_turn_decisions(host, turn, deadline)
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
        reranker = host._topic_reranker()
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
    if learning_enabled and host._prepared_turn_current(turn):
        # Judge the final candidate shortlist; an earlier opinion would be
        # immediately invalidated by the embedding/reranker commits above.
        await _fill_turn_decisions(host, turn, deadline)
    record_stage(turn.node, 'enrichment', started)


async def _enrich_topic_background(host, turn: _PreparedTurn) -> None:
    """Optional display enrichment never delays a reply or outlives reset."""
    host._track_hook_task(turn.result.session_id)
    runtime = turn.runtime
    async with runtime.state_lock:
        if (host._shutting_down or host._sessions.get(turn.result.session_id) is not runtime
                or runtime.epoch != turn.epoch or runtime.dag is not turn.dag
                or runtime.user_revisions.get(str(turn.result.user_id or ''), 0) != turn.owner_revision
                or turn.dag.get_node(turn.node.msg_id) is not turn.node):
            return
        reranker = host._topic_reranker()
    if reranker is not None:
        if turn.explicit_platform:
            await host.thread_router.rerank_pending(runtime, turn.node, reranker,
                is_current=lambda: not host._shutting_down and runtime.epoch == turn.epoch
                and runtime.user_revisions.get(str(turn.result.user_id or ''), 0) == turn.owner_revision)
        async with runtime.state_lock:
            if (host._shutting_down or runtime.epoch != turn.epoch
                    or runtime.user_revisions.get(str(turn.result.user_id or ''), 0) != turn.owner_revision
                    or host._sessions.get(turn.result.session_id) is not runtime
                    or runtime.dag is not turn.dag
                    or turn.dag.get_node(turn.node.msg_id) is not turn.node):
                return
        await host.thread_router.title_topic(runtime, turn.node, reranker,
            is_current=lambda: not host._shutting_down and runtime.epoch == turn.epoch
            and runtime.user_revisions.get(str(turn.result.user_id or ''), 0) == turn.owner_revision)
        host._mark_panel_runtime_dirty()


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
    # Phase one of a shadow run: the runtime keeps the baseline behaviour and
    # the policy's decision is computed beside it. Computed here, before the
    # trace is frozen, so the comparison travels inside the same snapshot the
    # rest of the decision does.
    shadow_runtime = getattr(host, "learning_policy", None)
    shadow_recorded = (
        shadow_runtime.shadow_decision(
            score=float(addressivity.contribution_total or 0.0),
            level=addressivity.level.value if hasattr(addressivity.level, "value")
            else str(addressivity.level),
            evidence_codes=[item.code for item in addressivity.evidence],
            has_prior_bot=last_bot_node is not None,
            baseline_threshold=float(host.addressivity_router.strong_threshold),
            now=host.time_service.wall_time(),
        ) if shadow_runtime is not None else None)
    if shadow_recorded is not None:
        node.metadata["shadow_decision"] = shadow_recorded
        host.shadow_telemetry.record(
            shadow_recorded, session=session_id, message_id=node.msg_id,
            host_version=_host_plugin_version())
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
        shadow=shadow_recorded)
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
        explicit = explicit_platform
        if not explicit and is_request_supplement(runtime, user_id, now):
            # Same-user addendum to a recent @/wake request is not ambient chatter.
            explicit = True
        observations = {"mpm": telemetrics.mpm, "mode": vibe_mode.value,
                        "addressivity": addressivity.score, "scene_tags": list(telemetrics.scene_tags)}
        canonical_ids = tuple(turn_node.msg_id for turn_node in turn_nodes) or (node.msg_id,)
        return snapshot_turn(
            runtime,
            result,
            canonical_ids,
            parsed_events,
            explicit,
            observations,
            host.shadow_mode,
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
        decision_floor=float(host._runtime_config.laya_min_confidence),
    )

    explicit_addr = addressivity.level == AddressivityLevel.STRONG
    if not explicit_addr and is_request_supplement(runtime, user_id, now):
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
        decision_floor=float(host._runtime_config.laya_min_confidence),
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
