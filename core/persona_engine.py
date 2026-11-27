"""One model decision per turn, outside session locks, with bounded admission."""

from __future__ import annotations
import json

import asyncio
import hashlib
from astrbot.api import logger
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Sequence

from .agent_bridge import AstrBotAgentBridge, PersonaChanged
from .pacer import PacingShaper, is_rhythm_short_act, scale_delay
from .topic_identity import confirmed_topic_id
from .active_dialogue import dialogue_for_node
from .dialogue_context import capture_dialogue
from .platform_bridge import _comp_type_name, _iter_reply_media_types, _safe_call, chain_plain_text
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
    MAX_TOTAL_STATE_CHARS,
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
from . import evaluative_reading, tone_context
from .systemone import window as decision_window
from .pipeline_telemetry import PipelineTelemetry
from .context_retrieval import clip_text, select_background, select_peer_continuation_candidate
from .routing_contract import commit_topic_evidence
from .narrative import DECISION_GUIDANCE
from .reply_failure import ModelReplyFailure, unavailable_reply
from . import member_boundary
from .context_completion import complete_context
from .early_reply import final_fragments
from .group_profiles import resolve_effective_group_profile
from .native_decision import selected_decision_client
from .integrations import dynamics_horizons
from .integrations import dynamics_learning
from .integrations import group_ecosystem
from .integrations import scene_learning



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
_WAKE_ADMITTED_REASONS = frozenset(f'{backend}_{reason}' for backend in ('jev', 'llm')
    for reason in ('wake_addressed', 'wake_low_confidence_acknowledged'))
_WAITING_REASONS = frozenset({'jev_waiting_for_completion', 'llm_waiting_for_completion'})


def _decision_prefix(plugin):
    return 'llm_' if plugin._runtime_config.decision_backend == 'llm' else 'jev_'


_SOFT_TIMEOUT_HIGH_DIRECTIVITY_CUES = (
    "在吗", "在不", "在不在", "问一下", "问个", "请问", "帮我", "帮忙",
    "有空吗", "那个", "话说", "等下", "等等", "顺便", "对了", "?", "？",
)


def _should_soft_timeout_wait(turn: TurnContext, item: "ModelTurn") -> bool:
    """Return True when a `wait` turn should trigger a soft-timeout final re-check instead of 60s drop."""
    if item.observations.get("force_complete"):
        return False
    if item.private_direct:
        return True
    if turn.soft_wake or turn.mandatory_reply or turn.wake_kind in {"at", "quote", "name", "private", "supplement"}:
        return True
    if turn.dialogue is not None and turn.dialogue.continuation_kind:
        return True
    clean = str(turn.text or "").strip()
    if clean and len(clean) <= 24 and any(cue in clean for cue in _SOFT_TIMEOUT_HIGH_DIRECTIVITY_CUES):
        return True
    return False


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
    followup_merge_count: int = 0

    @property
    def context_node_ids(self) -> frozenset[str] | None:
        return self.context.visible_node_ids


def _is_priority_wake(item: ModelTurn | None) -> bool:
    """True for explicit priority wakes (@, pure/bot quote, private direct)."""
    if item is None:
        return False
    ctx = getattr(item, "context", None)
    return bool(
        getattr(item, "private_direct", False)
        or (ctx is not None and (getattr(ctx, "mandatory_reply", False) or getattr(ctx, "wake_kind", "") == "quote"))
    )


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


def _actual_send_message_ids(sent: Any, fragment: Any, delivered_text: str) -> tuple[str, ...]:
    """Return only platform IDs carried by a successful send receipt."""
    if not getattr(sent, "success", False):
        return ()
    candidates = list(getattr(sent, "message_ids", ()) or ())
    candidates.append(getattr(sent, "message_id", None))
    identified = getattr(sent, "identified_messages", None)
    if callable(identified):
        candidates.extend(message_id for message_id, _ in identified(fragment, delivered_text))
    result: list[str] = []
    for value in candidates:
        message_id = str(value or "").strip()[:128]
        if message_id and message_id not in result:
            result.append(message_id)
    return tuple(result)


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


def _automatic_group_reply_id(runtime: Any, item: ModelTurn, platform_id: str | None) -> str | None:
    """Quote a real member message when it identifies the turn being answered."""
    if item.private_direct or not platform_id or not item.context.messages:
        return None
    target = _dag_node(runtime, platform_id)
    author = str(getattr(target, "user_id", "") or "")
    if not author or author == runtime.bot_id:
        return None
    messages = item.context.messages
    if platform_id != messages[-1].message_id or any(
        (message.reply_to or getattr(message.semantics, "quoted_message_id", ""))
        not in ("", message.message_id)
        for message in messages
    ):
        return platform_id
    return None


def _strip_leading_caption(semantic: str, caption: str) -> str:
    """Remove the already-delivered caption prefix from a split text_meme delivery note."""
    clean_semantic = str(semantic or "")
    clean_caption = str(caption or "").strip()
    if not clean_semantic or not clean_caption:
        return clean_semantic
    stripped = clean_semantic.lstrip()
    if stripped.startswith(clean_caption):
        remainder = stripped[len(clean_caption) :].lstrip("\r\n")
        return remainder
    return clean_semantic


def split_text_meme_chains(
    chains: Sequence[Any],
    pacer: Any,
    *,
    max_fragments: int = 10,
) -> list[Any]:
    """Split mixed Plain+Image text_meme chains into separate text bubbles followed by a pure image bubble."""
    items = list(chains or ())
    if max_fragments < 2 or not items:
        return items
    result: list[Any] = []
    for chain in items:
        components = list(getattr(chain, "chain", ()) or ())
        if not components:
            result.append(chain)
            continue
        plain_parts: list[Any] = []
        image_parts: list[Any] = []
        other_parts = False
        for component in components:
            kind = _comp_type_name(component).lower()
            if kind in {"plain", "text"}:
                plain_parts.append(component)
            elif kind == "image":
                image_parts.append(component)
            else:
                other_parts = True
                break
        if other_parts or not plain_parts or not image_parts:
            result.append(chain)
            continue
        caption = "".join(str(getattr(c, "text", "")) for c in plain_parts).strip()
        if not caption:
            result.append(chain)
            continue
        text_budget = max(1, int(max_fragments) - 1)
        raw_parts = (
            pacer.persona_fragments(caption)
            if pacer is not None and callable(getattr(pacer, "persona_fragments", None))
            else [caption]
        )
        text_parts = [part for part in raw_parts if str(part or "").strip()] or [caption]
        text_parts = list(PacingShaper.limit_fragments(text_parts, text_budget))

        try:
            image_chain = type(chain)(list(image_parts))
        except Exception:
            import copy

            image_chain = copy.copy(chain)
            image_chain.chain = list(image_parts)

        for attr in ("_cd_media_validate_v1", "_cd_media_success_v1"):
            value = getattr(chain, attr, None)
            if value is not None:
                setattr(image_chain, attr, value)
        semantic = getattr(chain, "_cd_delivery_text_v1", None)
        if isinstance(semantic, str) and semantic.strip():
            trimmed = _strip_leading_caption(semantic, caption)
            if trimmed.strip():
                image_chain._cd_delivery_text_v1 = trimmed
        image_chain._cd_meme_fallback_text_v1 = ""
        image_chain._cd_meme_text_already_sent_v1 = True

        result.extend(text_parts)
        result.append(image_chain)
    return result


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


def _uid_names_from_dag(dag: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    getter = getattr(dag, "get_recent_nodes", None) if dag is not None else None
    if not callable(getter):
        return names
    try:
        recent = getter(limit=0)
    except Exception:
        return names
    for node in recent:
        uid = str(getattr(node, "user_id", "") or "")
        name = str(_node_metadata(node).get("sender_name") or "")
        if uid and name:
            names[uid] = name
    return names


def _node_poke_fields(node: Any, uid_names: dict[str, str], parsed: Any = None) -> tuple[str, str, str]:
    meta = _node_metadata(node)
    poke_target_id = str(meta.get("poke_target_id") or getattr(parsed, "poke_target_id", "") or "")
    poke_target_name = str(
        meta.get("poke_target_name") or (uid_names.get(poke_target_id, "") if poke_target_id else "") or ""
    )
    poke_action_text = str(meta.get("poke_action_text") or getattr(parsed, "poke_action_text", "") or "")
    return poke_target_id, poke_target_name, poke_action_text


def _is_valid_poke_uid(value: Any, *, bot_id: str = "") -> bool:
    uid = str(value or "").strip()
    if not uid or uid in {"0", "all"} or (bot_id and uid == bot_id):
        return False
    return uid.isascii() and uid.isdigit()


_HISTORY_MEDIA_COMPONENT_TYPES = frozenset({
    "image", "sticker", "face", "emoji", "animatedemoji", "wechatemoji",
})


def _format_action_marker(kind: str, *, target_label: str = "", detail: str = "") -> str:
    """Format a semantic action marker for assistant conversation history."""
    from . import platform_bridge as _pb

    helper = getattr(_pb, "format_delivered_action_marker", None)
    if callable(helper):
        try:
            formatted = str(helper(kind, target_label=target_label, detail=detail) or "").strip()
            if formatted:
                return formatted
        except Exception:
            pass
    clean_target = " ".join(str(target_label or "").split()).strip()[:40]
    clean_detail = " ".join(str(detail or "").split()).strip()[:40]
    if kind == "poke":
        return f"[动作: 戳了 {clean_target}]" if clean_target else "[动作: 戳了对方]"
    if kind == "meme":
        return f"[动作: 发了一张表情（{clean_detail}）]" if clean_detail else "[动作: 发了一张表情]"
    if kind == "image":
        return f"[动作: 发了一张图片（{clean_detail}）]" if clean_detail else "[动作: 发了一张图片]"
    return f"[动作: {kind}]"


def _extract_fragment_media_detail(components: Sequence[Any]) -> str:
    """Return a concise human-readable label for a delivered meme/image if present."""
    for comp in components:
        if _comp_type_name(comp).lower() not in _HISTORY_MEDIA_COMPONENT_TYPES:
            continue
        for attr in ("summary", "desc", "description", "alt", "label", "caption"):
            raw = getattr(comp, attr, None)
            if not isinstance(raw, str):
                continue
            text = " ".join(raw.split()).strip()
            if not text or text.startswith(("http://", "https://", "base64://", "file://", "/", "\\")):
                continue
            if len(text) > 40:
                text = text[:39].rstrip() + "…"
            return text
    return ""


def _resolve_poke_target_label(
    runtime: Any,
    turn: TurnContext,
    poke_target: str,
) -> str:
    uid = str(poke_target or "").strip()
    if not uid:
        return ""
    for msg in (*turn.messages, *turn.background):
        if msg.author == uid and msg.author_name:
            return str(msg.author_name).strip()
        if msg.poke_target_id == uid and msg.poke_target_name:
            return str(msg.poke_target_name).strip()
    names = _uid_names_from_dag(getattr(runtime, "dag", None))
    return str(names.get(uid) or uid).strip()


def _chain_visible_plain_text(fragment: Any) -> str:
    """Extract only actual Plain/Text components without stringifying Image/media objects."""
    if fragment is None:
        return ""
    if isinstance(fragment, str):
        return fragment.strip()
    comps = getattr(fragment, "chain", None)
    if isinstance(comps, (list, tuple)):
        parts: list[str] = []
        for comp in comps:
            if isinstance(comp, str):
                parts.append(comp)
            elif _comp_type_name(comp).lower() in {"plain", "text"}:
                text = getattr(comp, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return chain_plain_text(fragment).strip()


def _format_fragment_for_history(
    fragment: Any,
    delivered_text: str,
    *,
    is_poke: bool,
    reply_mode: str,
    poke_target: str,
    turn: TurnContext,
    runtime: Any,
    private_direct: bool,
) -> str:
    """Build the semantic assistant-history representation of one delivered fragment."""
    if is_poke:
        is_third_party = bool(not private_direct and poke_target and poke_target != str(turn.author or ""))
        target_label = _resolve_poke_target_label(runtime, turn, poke_target) if is_third_party else ""
        return _format_action_marker("poke", target_label=target_label)
    if not isinstance(fragment, str):
        semantic_attr = getattr(fragment, "_cd_delivery_text_v1", None)
        if isinstance(semantic_attr, str) and semantic_attr.strip():
            return semantic_attr.strip()
        comps = list(getattr(fragment, "chain", ()) or ())
        has_image = any(_comp_type_name(comp).lower() in _HISTORY_MEDIA_COMPONENT_TYPES for comp in comps)
        if has_image:
            detail = _extract_fragment_media_detail(comps)
            kind = "meme" if reply_mode in {"meme_only", "text_meme", "poke_meme"} else "image"
            marker = _format_action_marker(kind, detail=detail)
            plain_text = _chain_visible_plain_text(fragment)
            return f"{plain_text}\n{marker}" if plain_text else marker
    return str(delivered_text or "").strip()


def _join_history_delivered(entries: Sequence[str]) -> str:
    """Join action markers with single newlines while preserving double-newline text paragraphs."""
    cleaned = [str(entry or "").strip() for entry in entries if str(entry or "").strip()]
    if not cleaned:
        return ""
    out = cleaned[0]
    for nxt in cleaned[1:]:
        prev_is_action = out.rsplit("\n", 1)[-1].startswith(("[动作:", "[表情历史"))
        nxt_is_action = nxt.startswith(("[动作:", "[表情历史"))
        sep = "\n" if (prev_is_action or nxt_is_action) else "\n\n"
        out = f"{out}{sep}{nxt}"
    return out


def _read_outbound_ledger_note(plugin: Any, runtime: Any, item: ModelTurn, event: Any) -> str:
    """Read a bounded read-only note of recent external sends and backfilled poke actions."""
    ledger = getattr(plugin, "outbound_ledger", None)
    formatter = getattr(ledger, "format_recent_outbound_note", None)
    if not callable(formatter):
        return ""
    session_key = str(getattr(runtime, "umo", "") or getattr(runtime, "session_key", "") or "")
    if not session_key:
        return ""
    platform_id = str(_safe_call(event, "get_platform_name", "") or "")
    target_id = str(item.context.author or "") if item.private_direct else str(getattr(runtime, "group_id", "") or "")
    scope = "private" if item.private_direct else "group"
    now = plugin.time_service.time() if getattr(plugin, "time_service", None) is not None else None
    try:
        note = formatter(
            session_key,
            now=now,
            platform_id=platform_id,
            target_id=target_id,
            scope=scope,
        )
    except TypeError:
        try:
            note = formatter(session_key)
        except Exception:
            return ""
    except Exception:
        return ""
    return str(note or "").strip()


def _merge_sleep_wakeup_pending(plugin: Any, runtime: Any, item: ModelTurn, pacing: Any) -> ModelTurn:
    """Prepend earlier deferred messages consumed on sleep wake-up in chronological order."""
    merged_ids = tuple(getattr(pacing, "merged_pending_ids", ()) or ())
    merged_texts = tuple(getattr(pacing, "merged_pending_texts", ()) or ())
    merged_events = tuple(getattr(pacing, "merged_pending_events", ()) or ())
    count = max(len(merged_ids), len(merged_texts))
    if count <= 0:
        return item
    turn = item.context
    existing_ids = {str(m.message_id) for m in turn.messages}
    uid_names = _uid_names_from_dag(getattr(runtime, "dag", None))
    default_author_name = turn.messages[-1].author_name if turn.messages else ""
    default_platform = turn.messages[-1].platform if turn.messages else ""
    trigger_event = item.events[-1] if item.events else None
    trigger_umo = getattr(trigger_event, "unified_msg_origin", None)

    prepended_snapshots: list[MessageSnapshot] = []
    prepended_texts: list[str] = []
    prepended_nodes: list[Any] = []
    prepended_plat_ids: set[str] = set()
    merged_id_set: set[str] = set()
    now_mono = plugin.time_service.time() if getattr(plugin, "time_service", None) is not None else time.monotonic()

    for idx in range(count):
        mid = str(merged_ids[idx] or "").strip() if idx < len(merged_ids) else ""
        raw_text = str(merged_texts[idx] or "").strip() if idx < len(merged_texts) else ""
        if mid and mid in existing_ids:
            continue
        node = _dag_node(runtime, mid) if mid else None
        text = raw_text or str(getattr(node, "text", "") or "").strip()
        if not text:
            continue
        if not mid:
            next_id = getattr(plugin, "_next_outgoing_id", None)
            mid = str(next_id()) if callable(next_id) else f"rhythm-wake-{idx}"
        if mid in existing_ids or mid in merged_id_set:
            continue
        if node is None and getattr(runtime, "dag", None) is not None:
            node = runtime.dag.add_message(
                msg_id=mid,
                user_id=str(turn.author or ""),
                text=text,
                timestamp=now_mono - max(1.0, float(count - idx)),
                metadata={"platform_message_id": bool(idx < len(merged_ids) and merged_ids[idx])},
            )
        meta = _node_metadata(node)
        poke_tid, poke_tname, poke_action = _node_poke_fields(node, uid_names) if node is not None else ("", "", "")
        snap = MessageSnapshot(
            message_id=mid,
            author=str(getattr(node, "user_id", "") or turn.author),
            text=text,
            reply_to=str(getattr(node, "reply_to_id", "") or ""),
            source_text=text,
            timestamp=getattr(node, "timestamp", None),
            author_name=str(meta.get("sender_name") or default_author_name),
            platform=str(meta.get("platform") or default_platform),
            poke_target_id=poke_tid,
            poke_target_name=poke_tname,
            poke_action_text=poke_action,
        )
        prepended_snapshots.append(snap)
        prepended_texts.append(text)
        merged_id_set.add(mid)
        if meta.get("platform_message_id") is True:
            prepended_plat_ids.add(mid)
        if node is not None:
            prepended_nodes.append(node)

    if not prepended_snapshots:
        return item

    new_messages = (*prepended_snapshots, *turn.messages)
    combined_text_parts = [*prepended_texts, str(turn.text or "").strip()]
    new_text = "\n".join(part for part in combined_text_parts if part)
    base_source = turn.source_text if turn.source_text is not None else turn.text
    new_source_parts = [*prepended_texts, str(base_source or "").strip()]
    new_source_text = "\n".join(part for part in new_source_parts if part)
    new_bg = tuple(m for m in turn.background if m.message_id not in merged_id_set)
    new_visible = (
        frozenset((*turn.visible_node_ids, *merged_id_set))
        if turn.visible_node_ids is not None
        else None
    )
    new_turn = replace(
        turn,
        messages=new_messages,
        text=new_text,
        source_text=new_source_text,
        background=new_bg,
        visible_node_ids=new_visible,
    )

    existing_ev_ids = {id(ev) for ev in item.events}
    prepended_events = [
        ev
        for ev in merged_events
        if ev is not None
        and id(ev) not in existing_ev_ids
        and (trigger_umo is None or getattr(ev, "unified_msg_origin", None) == trigger_umo)
    ]
    # Keep item.events[-1] as the current trigger event.
    new_events = (*prepended_events, *item.events) if prepended_events else item.events
    new_outcome_nodes = (
        (*prepended_nodes, *item.outcome_nodes)
        if (item.outcome_nodes and len(prepended_nodes) == len(prepended_snapshots))
        else item.outcome_nodes
    )
    new_plat_ids = item.platform_message_ids | prepended_plat_ids
    return replace(
        item,
        context=new_turn,
        events=new_events,
        outcome_nodes=new_outcome_nodes,
        platform_message_ids=new_plat_ids,
        observations={
            **item.observations,
            "rhythm_sleep_wakeup_merged_ids": tuple(s.message_id for s in prepended_snapshots),
        },
    )


def _collect_trusted_poke_targets(turn: TurnContext, bot_id: str) -> set[str]:
    trusted: set[str] = set()

    def _add(candidate: Any) -> None:
        uid = str(candidate or "").strip()
        if _is_valid_poke_uid(uid, bot_id=bot_id):
            trusted.add(uid)

    _add(getattr(turn, "author", ""))
    poke_ctx = getattr(turn, "poke_context", None)
    if poke_ctx is not None:
        target_ident = (
            poke_ctx.get("poke_target_identity")
            if isinstance(poke_ctx, dict)
            else getattr(poke_ctx, "poke_target_identity", None)
        )
        if isinstance(target_ident, dict):
            _add(target_ident.get("user_id"))
            _add(target_ident.get("qq"))
    for msg in (
        *(getattr(turn, "messages", ()) or ()),
        *(getattr(turn, "background", ()) or ()),
        *(getattr(turn, "nearby_context", ()) or ()),
    ):
        _add(getattr(msg, "author", ""))
        _add(getattr(msg, "poke_target_id", ""))
        for mentioned in getattr(msg, "mentioned_users", ()) or ():
            _add(mentioned)
        semantics = getattr(msg, "semantics", None)
        if semantics is not None:
            _add(getattr(semantics, "quoted_author_id", ""))
            for mentioned in getattr(semantics, "mentioned_user_ids", ()) or ():
                _add(mentioned)
    return trusted


def _resolve_poke_target(turn: TurnContext, event: Any, bot_id: str, requested_uid: str) -> str:
    """Return a strictly validated QQ UID to poke, or '' when invalid/unsupported."""
    event_sender = str(_safe_call(event, "get_sender_id", "") or "").strip()
    group_id = str(_safe_call(event, "get_group_id", "") or "").strip()
    platform_name = str(_safe_call(event, "get_platform_name", "") or "").lower()
    call_action = getattr(getattr(event, "bot", None), "call_action", None)
    if (
        platform_name != "aiocqhttp"
        or not callable(call_action)
        or (group_id and (not group_id.isascii() or not group_id.isdigit()))
    ):
        return ""
    trusted = _collect_trusted_poke_targets(turn, bot_id)
    explicit_target = str(requested_uid or "").strip()
    if not explicit_target:
        default_target = str(getattr(turn, "author", "") or "").strip()
        if (
            not _is_valid_poke_uid(default_target, bot_id=bot_id)
            or not _is_valid_poke_uid(event_sender, bot_id=bot_id)
            or event_sender != default_target
            or default_target not in trusted
        ):
            return ""
        return default_target
    if not _is_valid_poke_uid(explicit_target, bot_id=bot_id):
        return ""
    if explicit_target not in trusted:
        return ""
    if not group_id and event_sender != explicit_target:
        return ""
    return explicit_target


def _is_poke_chain(fragment: Any) -> bool:
    if isinstance(fragment, str):
        return False
    components = getattr(fragment, "chain", None)
    if not isinstance(components, (list, tuple)) or len(components) != 1:
        return False
    return _comp_type_name(components[0]).lower() == "poke"


def _snapshot_background(runtime, nodes, *, eligible_ids=None, retained=()) -> tuple[MessageSnapshot, ...]:
    dag = runtime.dag
    uid_names = _uid_names_from_dag(dag)
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
        limit = (
            400
            if any(
                p.relation in {"poke_sender", "poke_reply", "poke_event", "poke_bot_recent", "poke_group_recent"}
                for p in proofs[mid]
            )
            else 1200
        )
        if mid in original:
            text = clip_text(original[mid].text, min(limit, budget))
            background.append(replace(original[mid], text=text, context_evidence=proofs[mid]))
        else:
            n = selections[mid].node
            text = clip_text(n.text, min(limit, budget))
            poke_target_id, poke_target_name, poke_action_text = _node_poke_fields(n, uid_names)
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
                    poke_target_id=poke_target_id,
                    poke_target_name=poke_target_name,
                    poke_action_text=poke_action_text,
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
    poke_context: Any = None,
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

    uid_names = _uid_names_from_dag(dag)
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
            poke_target_id=_node_poke_fields(resolved, uid_names, parsed)[0],
            poke_target_name=_node_poke_fields(resolved, uid_names, parsed)[1],
            poke_action_text=_node_poke_fields(resolved, uid_names, parsed)[2],
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
        poke_context=poke_context,
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
    _dynamics_horizons_v1 = True
    _dynamics_learning_social_v2 = True
    _dynamics_learning_v1 = True
    _decision_compaction_release = True
    _decision_nearby_context_release = True
    _recipient_guard_release = True

    def __init__(self, plugin):
        self.plugin = plugin
        self.bridge = AstrBotAgentBridge(plugin.context)
        slot_count = 4
        try:
            from .. import series_panels as _series_panels
        except Exception:
            try:
                import series_panels as _series_panels
            except Exception:
                _series_panels = None
        if _series_panels is not None:
            try:
                profile = _series_panels.resolve_series_resource_profile(
                    getattr(plugin, "context", None)
                )
                slot_count = 4 if profile == "standard" else 2
            except Exception:
                slot_count = 4
        self.slots = asyncio.Semaphore(slot_count)
        self._projection_cache = OrderedDict()
        self._projection_tasks = {}
        self.telemetry = PipelineTelemetry()

    def _invalid(self, runtime, item: ModelTurn, reason: str, *, stage: str = "") -> bool:
        """Refuse a turn and leave one WARNING with ids only (never message text)."""
        drop_stage = str(stage or "unknown").strip() or "unknown"
        record_terminal_outcome(runtime, item, outcomes.mark_suppressed, reason, stage="generation")
        event = item.events[-1] if item.events else None
        if event is not None and not getattr(event, "_cd_invalid_warned_v1", False):
            try:
                setattr(event, "_cd_invalid_warned_v1", True)
            except Exception:
                pass
            logger.warning(
                "[ChatDynamics] turn dropped session=%s turn=%s reason=%s stage=%s",
                runtime.session_key,
                _turn_log_id(runtime, item),
                reason,
                drop_stage,
            )
        return False

    def _is_turn_recalled(self, runtime, item: ModelTurn) -> bool:
        """Return True if any message backing this turn was recalled on the platform."""
        checker = getattr(runtime, "is_recalled", None)
        if not callable(checker):
            return False
        for message in getattr(item.context, "messages", ()) or ():
            mid = str(getattr(message, "message_id", "") or "")
            if mid and checker(mid):
                return True
        for pid in getattr(item, "platform_message_ids", ()) or ():
            if pid and checker(str(pid)):
                return True
        for ev in getattr(item, "events", ()) or ():
            ev_mid = getattr(ev, "message_id", None)
            if not ev_mid:
                msg_obj = getattr(ev, "message_obj", None)
                ev_mid = getattr(msg_obj, "message_id", None) if msg_obj is not None else None
            if ev_mid and checker(str(ev_mid)):
                return True
        return False

    def _valid_prefix(self, runtime, item: ModelTurn, *, stage: str = ""):
        """事件/边界前置检查。

        返回值 True/False 表示已可判定，None 表示需要继续做证据校验与尾部判定。
        """
        p, turn = self.plugin, item.context
        if self._is_turn_recalled(runtime, item):
            return self._invalid(runtime, item, "message_recalled", stage=stage)
        if item.events:
            event = item.events[-1]
            if getattr(event, "_chat_dynamics_boundary_failed_closed", False):
                return self._invalid(runtime, item, "boundary_failed_closed", stage=stage)
            if getattr(event, "_chat_dynamics_evidence_failed_closed", False):
                return self._invalid(runtime, item, "evidence_failed_closed", stage=stage)
            policy, token, state = member_boundary.peek(p, event, turn.author)
            if not state.available and member_boundary.fail_closed(state):
                return self._invalid(runtime, item, "boundary_service_unavailable", stage=stage)
            if member_boundary.blocked(policy):
                return self._invalid(runtime, item, "boundary_policy_blocked", stage=stage)
            captured = getattr(event, "_chat_dynamics_boundary_token", None)
            if captured is not None and token != captured:
                return self._invalid(runtime, item, "boundary_policy_changed", stage=stage)
        return None

    def _valid_suffix(self, runtime, item: ModelTurn, *, stage: str = "") -> bool:
        """尾部判定（会话/epoch/冷却等纯本地状态检查）。"""
        p, turn = self.plugin, item.context
        if self._is_turn_recalled(runtime, item):
            return self._invalid(runtime, item, "message_recalled", stage=stage)
        if not dynamics_learning.current(runtime, item):
            return self._invalid(runtime, item, "learning_revision_changed", stage=stage)
        scope_enabled = (
            p.is_runtime_scope_enabled(runtime)
            if callable(getattr(p, "is_runtime_scope_enabled", None))
            else p.is_group_takeover_enabled(runtime.group_id)
        )
        effective_shadow = (
            p.is_session_shadow_mode(runtime.session_key, group_id=runtime.group_id)
            if callable(getattr(p, "is_session_shadow_mode", None))
            else bool(p.shadow_mode)
        )
        valid = (
            not p._shutting_down
            and p._persona_mode()
            and effective_shadow == item.shadow
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
            return self._invalid(runtime, item, reason, stage=stage)
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

    def valid(self, runtime, item: ModelTurn, *, stage: str = "") -> bool:
        verdict = self._valid_prefix(runtime, item, stage=stage)
        if verdict is not None:
            return verdict
        if not self._evidence_view_current(item):
            return self._invalid(runtime, item, "evidence_revision_changed", stage=stage)
        return self._valid_suffix(runtime, item, stage=stage)

    async def valid_async(self, runtime, item: ModelTurn, *, stage: str = "") -> bool:
        """与 valid() 完全同判据，但证据读库不阻塞事件循环。

        供 async 调用点使用；valid() 保持同步与逐字原行为，供少数同步调用点
        （_finish_generation_failure / has_pending_explicit_request）继续使用。
        """
        verdict = self._valid_prefix(runtime, item, stage=stage)
        if verdict is not None:
            return verdict
        if not await self._evidence_view_current_async(item):
            return self._invalid(runtime, item, "evidence_revision_changed", stage=stage)
        return self._valid_suffix(runtime, item, stage=stage)

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
            "[ChatDynamics] reply generation failed session=%s turn=%s reason=%s valid=%s%s pipeline_turn_id=%s",
            runtime.session_key,
            _turn_log_id(runtime, item),
            reason,
            valid,
            (" error=" + error_type) if error_type else "",
            self.telemetry.turn_id(item.context),
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

    def _try_gentle_preempt(self, runtime) -> bool:
        """Yield an active ambient turn to a newly queued priority wake without breaking valid replies.

        Caller holds ``runtime.state_lock``.
        """
        active = getattr(runtime, "active_model_turn", None)
        if active is None or _is_priority_wake(active):
            return False
        owner = str(getattr(getattr(active, "context", None), "author", "") or "")
        if not owner:
            return False
        owned_tasks = getattr(runtime, "owned_turn_tasks", {}) or {}
        reply_tasks = getattr(runtime, "reply_generation_tasks", set()) or set()
        has_reply_task = any(
            task in reply_tasks and not task.done()
            for task, task_owner in owned_tasks.items()
            if task_owner == owner
        )
        in_reply = (
            has_reply_task
            or int(getattr(runtime, "reply_generation_owners", {}).get(owner, 0) or 0) > 0
        )
        if not in_reply:
            # (a) Active ambient turn has not entered reply generation yet:
            # cancel immediately so prepare/decision/media_listen yields.
            runtime.cancel_owned_turns(owner, pre_reply_only=True)
            runtime.mark_turn_preempted(active)
            return True
        # (b) Active ambient turn is in reply generation: only cancel if first
        # sentence has not been sent AND the turn is already superseded/invalid.
        first_sent = int(getattr(runtime, "reply_first_sentence_sent", {}).get(owner, 0) or 0)
        if first_sent == 0 and not self.valid(runtime, active):
            cancelled = runtime.cancel_owned_turns(owner, pre_reply_only=False)
            if cancelled > 0:
                runtime.mark_turn_preempted(active)
                return True
        return False

    async def submit(self, runtime, item: ModelTurn) -> None:
        item = replace(item, submitted_monotonic=time.monotonic())
        is_merged = getattr(runtime, "is_merged_followup", None)
        if callable(is_merged) and item.context.messages:
            msg_ids = [str(m.message_id) for m in item.context.messages if getattr(m, "message_id", "")]
            if msg_ids and all(is_merged(mid) for mid in msg_ids):
                self.plugin.debounce.finish_result(item.debounce_result)
                return
        if not await self.valid_async(runtime, item):
            self.plugin.debounce.finish_result(item.debounce_result)
            return
        now = self.plugin.time_service.time()
        priority = _is_priority_wake(item)
        addressed = (
            item.private_direct
            or turn_is_addressed(runtime, item.context, now)
            or turn_is_continuation(runtime, item.context, now)
        )
        if runtime.model_admission.locked() and not addressed:
            self.diagnostic(runtime, "overload_ambient")
            self.plugin.debounce.finish_result(item.debounce_result)
            return
        if priority and runtime.model_admission.locked():
            evicted = None
            async with runtime.state_lock:
                if runtime.model_admission.locked():
                    evicted = runtime.evict_oldest_non_priority(_is_priority_wake)
            if evicted is not None:
                self.plugin.debounce.finish_result(evicted.debounce_result)
                self.plugin.debounce.release_result_media(evicted.debounce_result)
                if evicted.debounce_result is None:
                    from .deferred_media import release_event_media

                    for ev in getattr(evicted, "events", ()) or ():
                        release_event_media(ev)
                record_terminal_outcome(
                    runtime, evicted, outcomes.mark_suppressed, "preempted_by_wake", stage="queue"
                )
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
                runtime.enqueue_model_turn(replace(item, fallback=fallback), is_priority_fn=_is_priority_wake)
                queued = True
                if priority:
                    self._try_gentle_preempt(runtime)
                if runtime.generation_task is None or runtime.generation_task.done():
                    runtime.generation_task = self.plugin._create_background_task(self.run(runtime))
        finally:
            runtime.model_waiting.pop(token, None)
            if admitted and not queued:
                runtime.model_admission.release()
            if not queued:
                self.plugin.debounce.finish_result(item.debounce_result)

    def has_in_flight_reply_generation(self, runtime, user_id: str) -> bool:
        """True while generate_ready() or active fragment delivery is running for user_id."""
        uid = str(user_id or "")
        owners = getattr(runtime, "reply_generation_owners", None)
        if isinstance(owners, dict) and int(owners.get(uid, 0) or 0) > 0:
            return True
        first_sent = getattr(runtime, "reply_first_sentence_sent", None)
        if isinstance(first_sent, dict) and int(first_sent.get(uid, 0) or 0) > 0:
            return True
        return False

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
        """Decide participation and resolve context-dependent praise within the same turn budget."""
        p, turn = self.plugin, item.context
        prefix = _decision_prefix(p)
        rule_routing = bool(
            getattr(p._runtime_config, "rule_topic_routing_enabled", False)
            and p._runtime_config.conversation_router_enabled
            and not item.private_direct
        )
        client = selected_decision_client(p)
        if client is None:
            return TurnDecision.fallback(turn, prefix + "unavailable")
        wide = decision_window.enabled(client)
        request_limit = decision_window.WIRE_BYTES if wide else MAX_TOTAL_REQUEST_CHARS
        state_limit = decision_window.WIRE_BYTES if wide else MAX_TOTAL_STATE_CHARS
        if wide:
            turn = decision_window.decision_turn(turn, item.events, p)
        timeout = (p._runtime_config.decision_llm_timeout if p._runtime_config.decision_backend == 'llm'
                   else p._runtime_config.jev_timeout)
        review_deadline = time.monotonic() + min(timeout, p._runtime_config.decision_timeout) - 0.5
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
        affection = dynamics_learning.affection(p, runtime, item) if runtime is not None else None
        integrations = getattr(p, "integrations", None)
        read_affection = getattr(integrations, "affection_for_decision", None)
        if affection is None and runtime is not None and callable(read_affection) and item.narrative_bundle is None:
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
        decision_prompt = p._runtime_config.decision_prompt
        if item.narrative_bundle:
            # Life context supplies evidence and continuity, not a hidden
            # participation preset. Keep the operator's public-chat policy.
            decision_prompt += "\n" + DECISION_GUIDANCE
        learning = dynamics_learning.selected(runtime, item)
        learning_context = str(learning.get("context") or "").strip() if isinstance(learning, dict) else ""
        horizons_slot = dynamics_horizons.decision_slot(runtime, item)
        if item.narrative_bundle is None and horizons_slot is not None:
            decision_prompt = decision_prompt.rstrip() + "\n" + dynamics_horizons.HORIZONS_DECISION_GUIDANCE
        if item.narrative_bundle and item.events:
            boundary_state = member_boundary.resolve_state(p, item.events[-1])
            if boundary_state.available and not boundary_state.disabled:
                decision_prompt += (
                    "\n可使用已授权的交互边界工具：仅根据当前发言者本人可核验的互动证据，必要时暂停与此人的互动；"
                    "不需另行向此人确认。普通批评或反馈本身不自动构成暂停理由，也不得按他人一句命令封锁第三人。"
                    "仅在确有需要时调用，不必每轮调用。"
                )
        force_complete = bool(observations.get("force_complete"))
        game_ctx = observations.get("active_game_context") if isinstance(observations.get("active_game_context"), dict) else None
        if game_ctx and game_ctx.get("active"):
            decision_prompt = decision_prompt.rstrip() + "\n" + group_ecosystem.GAMES_DECISION_GUIDANCE
        try:
            from .nearby_context import filter_nearby

            turn = replace(
                turn, nearby_context=filter_nearby(turn.nearby_context, p, item.events[-1] if item.events else None)
            )
            char_state = item.narrative_bundle.decision_context() if item.narrative_bundle else None
            if char_state is None and horizons_slot is not None:
                char_state = {"horizons": horizons_slot}
            elif isinstance(char_state, dict) and horizons_slot is not None and "horizons" not in char_state:
                merged_cs = {**char_state, "horizons": horizons_slot}
                if len(json.dumps(merged_cs, ensure_ascii=False, separators=(",", ":"))) <= 2400:
                    char_state = merged_cs
            if game_ctx and game_ctx.get("active"):
                game_slot = {
                    "active": True,
                    "engines": list(game_ctx.get("engines") or [])[:4],
                    "companion_mode_hint": str(game_ctx.get("companion_mode_hint") or "accompany_quietly")[:64],
                    "summary": str(game_ctx.get("summary") or "")[:200],
                }
                if char_state is None:
                    char_state = {"active_game": game_slot}
                elif isinstance(char_state, dict) and "active_game" not in char_state:
                    merged_game_cs = {**char_state, "active_game": game_slot}
                    if len(json.dumps(merged_game_cs, ensure_ascii=False, separators=(",", ":"))) <= 2600:
                        char_state = merged_game_cs
            group_eff = resolve_effective_group_profile(
                p,
                getattr(runtime, "umo", "") or turn.session_key,
                group_id=getattr(runtime, "group_id", "") if runtime is not None else "",
            )["effective"]
            bundle_character = item.narrative_bundle.wire()["character"] if item.narrative_bundle else None
            bounded_state = build_state(
                turn,
                max_chars=state_limit,
                previous_state=state,
                observations=observations,
                presence=group_eff["presence_knob"],
                persona_prompt=(
                    bundle_character["core_prompt"]
                    if bundle_character is not None
                    else getattr(persona, "prompt", "")
                ),
                decision_prompt=decision_prompt,
                active_topics=topics,
                affection=affection,
                character_state=char_state,
                character_rules=bundle_character.get("group_rules", "")
                if bundle_character is not None
                else None,
                learning_context=learning_context or None,
            )
            # Initial state packing may prune optional scene observations.
            # Reoffer the filtered snapshot to the whole-request packer so
            # optional labels can yield before the entire scene disappears.
            from .nearby_context import decision_payload, POLICY

            scene = decision_payload(turn)
            if scene:
                bounded_state["conversation"]["nearby_context"] = scene
                bounded_state["conversation"]["nearby_context_policy"] = POLICY
            core_questions = build_questions(
                turn,
                narrative=item.narrative_bundle is not None,
                force_complete=force_complete,
                **context_options,
            )
            if rule_routing:
                core_questions.pop("recipient", None)
            topic_questions, topic_mapping = align_topic_task(bounded_state, topic_questions, topic_mapping)
            questions = {**core_questions, **topic_questions}
            if rule_routing:
                current_questions = {}
            else:
                current_questions, _ = build_addressee_task(turn, bounded_state, {}, limit=1, current_only=True,
                                                            max_chars=request_limit)
            # The core recipient question already judges continuation/soft
            # wakes. Do not reserve a second estimate of the same recipient
            # at the expense of current evidence; add it later if it fits.
            recipient_reservation = {} if "recipient" in questions else current_questions
            intent_questions = build_intent_task({}, {}, compact=item.narrative_bundle is not None)
            topics_before = len(bounded_state.get("active_topics") or ())
            bounded_state, intent_questions = bound_request_with_optional(
                bounded_state, {**questions, **recipient_reservation}, intent_questions,
                max_chars=request_limit, max_state_chars=state_limit,
                tone_context=tone_context.tone_context(turn, p, item.events[-1] if item.events else None),
            )
            if topic_mapping is not None and len(bounded_state.get("active_topics") or ()) != topics_before:
                topic_questions, topic_mapping = align_topic_task(bounded_state, topic_questions, topic_mapping)
                questions = {**core_questions, **topic_questions}
            # Current recipient first, current semantics second, historical
            # recipient estimates last. Keep the same single model call.
            if rule_routing:
                addressee_questions, addressee_mapping = {}, {}
            else:
                addressee_questions, addressee_mapping = build_addressee_task(
                    turn, bounded_state, {**questions, **intent_questions}, max_chars=request_limit
                )
            questions = {**questions, **addressee_questions, **intent_questions}
            request_budget = {
                "limit_chars": request_limit,
                "context_window_tokens": decision_window.CONTEXT_TOKENS if wide else None,
                "request_chars": request_size(bounded_state, questions),
                "decision_persona_chars": len(bounded_state.get("persona", "")),
                "decision_guidance_chars": len(bounded_state.get("decision_prompt", "")),
                "decision_question_chars": len(json.dumps(questions, ensure_ascii=False)),
                "nearby_context_messages": len(bounded_state["conversation"].get("nearby_context", ())),
                "nearby_context_candidates": len(turn.nearby_context),
                "tone_context_messages": len(bounded_state.get("tone_exchange", {}).get("preceding", ())),
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
        reading = None
        review_tone = (runtime is not None and p._runtime_config.decision_backend == "jev"
                       and callable(getattr(client, "evaluate_tone", None)) and callable(getattr(client, "tone_selection", None))
                       and "expression_mode" in intent_questions and "speaker_stance" in intent_questions)
        stamp = client.tone_selection() if review_tone else None
        async with self.slots:
            eval_kwargs: dict[str, Any] = {}
            if p._runtime_config.decision_backend == 'llm':
                eval_kwargs['umo'] = turn.session_key
            elif getattr(client, 'is_systemone_router', False):
                eval_kwargs['group_id'] = turn.session_key
            answers = await client.evaluate(
                state=bounded_state,
                questions=questions,
                timeout=timeout,
                **eval_kwargs,
            )
            if stamp is not None and answers:
                reading = await evaluative_reading.review(p, item.events[-1] if item.events else None,
                    turn, bounded_state, answers, client, stamp, review_deadline, floor)
                answers = evaluative_reading.project(answers, reading)
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
            runtime.jev_decision["backend"] = p._runtime_config.decision_backend
            runtime.jev_decision["effective_participation"] = {
                "managed": item.narrative_bundle is not None,
                "presence": bounded_state.get("participation_policy", {}).get("presence_knob"),
                "operator_guidance_included": str(bounded_state.get("decision_prompt", "")).startswith(
                    p._runtime_config.decision_prompt.strip()
                ),
            }
            if rule_routing:
                runtime.jev_decision["recipient_source"] = (
                    "llm" if turn.messages and any(e.startswith("recipient_llm_")
                        for e in getattr(turn.messages[-1].semantics, "routing_evidence", ())) else "rules")
            runtime.jev_addressees = resolved_addressees
            runtime.jev_decision["interaction_frame"] = {
                "session_key": turn.session_key,
                "speaker_id": turn.author,
                "trigger_message_id": turn.messages[-1].message_id if turn.messages else "",
                "tone_sources": tone_context.source_signatures(bounded_state.get("tone_exchange", {}).get("preceding", ())),
                "answers": {key: runtime.jev_decision[key] for key in intent_questions if key in runtime.jev_decision},
            }
            if reading is not None:
                runtime.jev_decision["evaluative_reading"] = reading
                runtime.jev_decision["interaction_frame"]["evaluative_reading"] = {
                    key: reading[key] for key in ("choice", "reason", "source_message_id") if key in reading
                }
        p._metric(prefix + "decision")
        base_join_floor = (
            learning["threshold"]
            if learning["threshold"] is not None
            else group_eff["reply_probability_threshold"]
        ) / 100.0
        if game_ctx and game_ctx.get("active") and not turn.mandatory_reply and not turn.soft_wake and not item.private_direct:
            base_join_floor = min(0.95, base_join_floor + 0.12)
        decision = decision_from_answers(
            turn,
            answers,
            min_confidence=floor,
            join_floor=base_join_floor,
            quote_recipient_floor=getattr(p._runtime_config, "quote_reply_threshold", 35.0) / 100.0,
            name_recipient_floor=getattr(p._runtime_config, "name_reply_threshold", 70.0) / 100.0,
            prefix=prefix,
            narrative=item.narrative_bundle is not None,
            addressees=resolved_addressees,
            force_complete=force_complete,
        )
        decision = annotate_model_decision(decision, turn, answers, resolved_addressees)
        if topic_mapping is not None and decision.reason_code not in _WAITING_REASONS:
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

    async def generate_ready(self, runtime, turn, *args, allow_early: bool = True, **kwargs):
        """Release a validated final output before optional cleanup/diagnostics."""
        started = time.monotonic()
        ready = asyncio.get_running_loop().create_future()
        budget_purpose = str(kwargs.get("budget_purpose", "reply") or "reply").strip()
        early = bool(
            allow_early
            and budget_purpose != "afterthought"
            and getattr(self.plugin._runtime_config, "reply_first_sentence_enabled", True)
        )

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
        reply_tasks = getattr(runtime, "reply_generation_tasks", None)
        caller_task = asyncio.current_task()
        added_caller = False
        if isinstance(reply_tasks, set):
            reply_tasks.add(child)
            if caller_task is not None and caller_task not in reply_tasks:
                reply_tasks.add(caller_task)
                added_caller = True

        def finished(task):
            runtime.owned_turn_tasks.pop(task, None)
            if isinstance(reply_tasks, set):
                reply_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        child.add_done_callback(finished)
        try:
            if early:
                completed, _ = await asyncio.wait({child, ready}, return_when=asyncio.FIRST_COMPLETED)
                if ready in completed and not ready.cancelled():
                    return ready.result()
                return child.result()
            return await child
        except BaseException:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            raise
        finally:
            if added_caller and isinstance(reply_tasks, set) and caller_task is not None:
                reply_tasks.discard(caller_task)
            if not ready.done():
                ready.cancel()

    def cancel_member(self, runtime, user_id: str) -> None:
        runtime.recent_images.discard(user_id)
        # Finish trace facts before the command advances the owner's revision.
        for item in (runtime.active_model_turn, *runtime.model_queue, *runtime.model_waiting.values()):
            if item is not None and item.context.author == user_id:
                record_outcome(runtime, item, outcomes.mark_suppressed, "member_stopped", stage="generation")
        runtime.cancel_owned_turns(user_id)

    def _schedule_media_maintenance(self, umo: str, conversation_id: str) -> None:
        """Run MediaArchive touch/prune in a background thread off the reply hot path."""
        archive = getattr(self.plugin, "media_archive", None)
        if archive is None:
            return

        def _work() -> None:
            archive.touch(umo, conversation_id)
            try:
                archive.prune(min_interval=30.0)
            except TypeError:
                archive.prune()

        async def _run() -> None:
            try:
                await asyncio.to_thread(_work)
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                _work()
            except Exception:
                pass
            return
        tasks = getattr(self, "_media_maintenance_tasks", None)
        if tasks is None:
            tasks = set()
            self._media_maintenance_tasks = tasks
        if len(tasks) >= 16:
            return
        task = loop.create_task(_run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def generate(self, event, events, prompt, persona, provider, **kwargs):
        """All owned native agents share the adapter's per-provider admission budget."""
        budget_purpose = str(kwargs.pop("budget_purpose", "reply") or "reply").strip() or "reply"
        timeout = self.plugin._runtime_config.reply_timeout
        started = time.monotonic()
        event_group_id = str(getattr(event, "unified_msg_origin", "") or "")
        if isinstance(self.bridge, AstrBotAgentBridge):
            event._cd_memory_provider_started_v1 = False
            kwargs["reply_timeout"] = timeout
            self.bridge.media_archive = self.plugin.media_archive
            private_direct = bool(kwargs.pop("private_direct", False))
            narrative_coord = getattr(self.plugin, "narrative", None)
            if private_direct and narrative_coord is not None:
                extras = getattr(event, "_extras", None)
                is_bound = isinstance(extras, dict) and bool(extras.get("_narrative_managed_v1"))
                enabled_fn = getattr(narrative_coord, "enabled", None)
                if not is_bound and not (callable(enabled_fn) and enabled_fn(event_group_id)):
                    narrative_coord = None
            kwargs["narrative_override"] = narrative_coord
            self._schedule_media_maintenance(event.unified_msg_origin, persona.conversation_id)
        call = self.plugin.llm.provider_budget.run(
            provider,
            budget_purpose,
            lambda: self.bridge.generate(event, events, prompt, persona, provider, boundary_host=self.plugin, **kwargs),
            plugin_id="astrbot_plugin_chat_dynamics",
            group_id=event_group_id,
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
                        budget_purpose,
                        lambda: self.bridge.generate(
                            event, events, prompt, persona, provider, boundary_host=self.plugin, **kwargs
                        ),
                        plugin_id="astrbot_plugin_chat_dynamics",
                        group_id=event_group_id,
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
                    if runtime.consume_turn_preempted(item):
                        item_cancelled = True
                        record_terminal_outcome(
                            runtime,
                            item,
                            outcomes.mark_suppressed,
                            "preempted_by_wake",
                            stage="generation",
                        )
                    elif await self.valid_async(runtime, item):
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
                    preempted = runtime.consume_turn_preempted(item)
                    if task.cancelling():
                        record_terminal_outcome(
                            runtime,
                            item,
                            outcomes.mark_suppressed,
                            "worker_cancelled",
                            stage="generation",
                        )
                        raise
                    if preempted:
                        wrote = record_terminal_outcome(
                            runtime,
                            item,
                            outcomes.mark_suppressed,
                            "preempted_by_wake",
                            stage="generation",
                        )
                        if not wrote and not item.shadow:
                            for message, node in zip(item.context.messages, item.outcome_nodes):
                                if node is not None and runtime.dag.get_node(message.message_id) is node:
                                    if outcomes.read_outcome(node).get("final_outcome") != outcomes.VALUE_DELIVERED:
                                        outcomes.mark_suppressed(node, "preempted_by_wake", stage="generation")
                    else:
                        record_terminal_outcome(
                            runtime,
                            item,
                            outcomes.mark_suppressed,
                            "member_cancelled",
                            stage="generation",
                        )
                except asyncio.TimeoutError:
                    reason = "poke_reply_timeout" if item.context.wake_kind in {"poke", "poke_peer"} else "reply_timeout"
                    self._finish_generation_failure(runtime, item, reason)
                    self.diagnostic(runtime, reason)
                    p._metric("llm_reply_unavailable")
                except PersonaChanged:
                    self._finish_generation_failure(runtime, item, "persona_changed", suppressed=True)
                    self.diagnostic(runtime, "persona_changed")
                except member_boundary.BoundaryCancelled as exc:
                    item_cancelled = True
                    bc_reason = str(exc).strip()[:64] or "boundary_cancelled"
                    self._finish_generation_failure(
                        runtime,
                        item,
                        bc_reason,
                        suppressed=True,
                        error_type=type(exc).__name__,
                    )
                    self.diagnostic(
                        runtime,
                        "boundary_cancelled",
                        reason=bc_reason,
                        error_type=type(exc).__name__,
                    )
                except Exception as exc:
                    self._finish_generation_failure(runtime, item, "agent_failed", error_type=type(exc).__name__)
                    self.diagnostic(runtime, "agent_failed", error_type=type(exc).__name__)
                finally:
                    deferred_media.finish()
                    try:
                        quote_gen_failed = (
                            item.context.wake_kind == "quote"
                            and any(
                                outcomes.read_outcome(node).get("final_outcome") == "generation_failed"
                                for node in item.outcome_nodes
                                if node is not None
                            )
                        )
                        if (
                            not item_cancelled
                            and (item.context.mandatory_reply or item.private_direct or quote_gen_failed)
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
                        runtime.consume_turn_preempted(item)
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
        if any(
            outcomes.read_outcome(node).get("delivered")
            or str(outcomes.read_outcome(node).get("suppression_reason") or "").startswith(
                ("rhythm_deferred_", "rhythm_catchup_skipped_")
            )
            or node.metadata.get("reply_choice", {}).get("no_reply") is True
            or node.metadata.get("reply_choice", {}).get("intentional_nonverbal") is True
            for node in nodes
        ):
            return
        if str(runtime.model_diagnostic.get("reason_code") or "").startswith(
            ("rhythm_deferred_", "rhythm_catchup_skipped_")
        ):
            return
        failure = next(
            (
                outcomes.read_outcome(node).get("suppression_reason")
                for node in nodes
                if outcomes.read_outcome(node).get("suppression_reason")
            ),
            runtime.model_diagnostic.get("reason_code", "empty_reply"),
        )
        is_quote_fallback = (
            turn.wake_kind == "quote" and not turn.mandatory_reply and not item.private_direct
        )
        if item.private_direct and not turn.mandatory_reply:
            # Private turns have no @ to acknowledge; a generic receipt is the
            # honest visible ending when generation failed.
            text = "收到你的消息了，但这次暂时没能生成完整回复。"
        elif is_quote_fallback:
            text = unavailable_reply(failure)
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
                    runtime, fallback_event, text,
                    reply_to_id=_automatic_group_reply_id(runtime, item, _platform_reply_id(runtime, item, target)),
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
            "private_reply_fallback"
            if (item.private_direct and not turn.mandatory_reply)
            else "quote_reply_fallback"
            if is_quote_fallback
            else "at_reply_fallback"
        )
        fallback_choice = None
        for node in nodes:
            choice = node.metadata.get("reply_choice")
            if isinstance(choice, dict):
                choice.update(actual="text", no_reply=False, delivered=True, reason=fallback_reason)
                fallback_choice = dict(choice)
        record_outcome(runtime, item, outcomes.mark_delivered, reason=fallback_reason)
        self.diagnostic(
            runtime,
            fallback_reason,
            failure_reason=failure,
            action="acknowledge",
            wake_kind=(
                "private"
                if item.private_direct and not turn.mandatory_reply
                else "quote"
                if is_quote_fallback
                else "at"
            ),
            reply_required=True,
            **({"reply_choice": fallback_choice} if fallback_choice else {}),
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
        raw_narrative = getattr(p, "narrative", None)
        narrative = (
            raw_narrative
            if (
                raw_narrative is not None
                and (
                    not item.private_direct
                    or (callable(getattr(raw_narrative, "enabled", None)) and raw_narrative.enabled(runtime.umo))
                )
            )
            else None
        )
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
        group_profile_eff = resolve_effective_group_profile(
            p, runtime.umo or runtime.session_key, group_id=runtime.group_id
        )["effective"]
        rhythm_ctrl = (
            getattr(p, "natural_rhythm", None)
            if group_profile_eff.get("natural_reply_rhythm_enabled", True)
            else None
        )
        rhythm_urgent_breakthrough = False
        rhythm_woken_from_sleep = False
        rhythm_temp_awake_active = False
        rhythm_sleep_wake_note = ""
        avail_now = "available"
        if bundle is not None:
            snap = bundle.snapshot
            if not snap.get("stale"):
                avail_now = str(snap.get("availability") or "available").lower()
        if rhythm_ctrl is not None and not item.shadow and not item.observations.get("rhythm_catchup"):
            if bundle is None:
                try:
                    avail_now = await rhythm_ctrl._probe_world_availability(
                        runtime, persona_id=str(getattr(persona, "persona_id", "") or "")
                    )
                except TypeError:
                    avail_now = await rhythm_ctrl._probe_world_availability(runtime)
            if avail_now == "available":
                rhythm_ctrl.on_availability_changed(runtime, avail_now)
            primary_mid = turn.messages[-1].message_id if turn.messages else ""
            primary_node = _dag_node(runtime, primary_mid)
            primary_topic = str((getattr(primary_node, "metadata", None) or {}).get("routing", {}).get("topic_id") or "")
            pacing = rhythm_ctrl.evaluate_ingress_pacing(
                runtime,
                msg_id=primary_mid,
                user_id=str(turn.author or ""),
                text=str(turn.text or ""),
                topic_id=primary_topic,
                wake_kind=str(turn.wake_kind or "none"),
                directed=bool(item.private_direct or turn.mandatory_reply or turn.soft_wake or turn.explicit),
                private_direct=bool(item.private_direct),
                availability=avail_now,
                persona_fingerprint=persona.fingerprint,
                node=primary_node,
                live_event=event,
                live_events=item.events,
            )
            rhythm_urgent_breakthrough = bool(getattr(pacing, "urgent_breakthrough", False))
            rhythm_woken_from_sleep = bool(getattr(pacing, "woken_from_sleep", False))
            rhythm_temp_awake_active = bool(getattr(pacing, "temp_awake_active", False))
            rhythm_sleep_wake_note = str(getattr(pacing, "sleep_wake_note", "") or "").strip()
            if pacing.defer:
                p.debounce.finish_result(item.debounce_result)
                record_outcome(runtime, item, outcomes.mark_suppressed, pacing.reason_code, stage="gate")
                self.diagnostic(
                    runtime,
                    pacing.reason_code,
                    action="ignore",
                    reason_zh=pacing.reason_zh,
                    wake_at_wall=pacing.wake_at_wall,
                )
                return
            merged_item = _merge_sleep_wakeup_pending(p, runtime, item, pacing)
            if merged_item is not item:
                item = merged_item
                turn = item.context
                decision_turn = turn
                event = item.events[-1] if item.events else event
            if not rhythm_sleep_wake_note:
                get_note = getattr(rhythm_ctrl, "get_sleep_wake_note", None)
                if callable(get_note):
                    try:
                        rhythm_sleep_wake_note = str(get_note(runtime, availability=avail_now) or "").strip()
                    except TypeError:
                        try:
                            rhythm_sleep_wake_note = str(get_note(runtime) or "").strip()
                        except Exception:
                            rhythm_sleep_wake_note = ""
                    except Exception:
                        rhythm_sleep_wake_note = ""
        dynamics_learning.begin(p, runtime, item, judgement_persona)
        dynamics_horizons.begin(p, runtime, item, judgement_persona)
        speculative_prepare_task: asyncio.Task | None = None
        speculative_prepare_mutated = False
        if (
            narrative is not None
            and bundle is not None
            and not item.private_direct
            and not item.shadow
            and (turn.mandatory_reply or turn.wake_kind == "quote")
        ):
            speculative_prepare_task = asyncio.create_task(
                narrative.prepare(runtime, item, persona)
            )

        async def _cancel_speculative_prepare() -> None:
            nonlocal speculative_prepare_task
            spec_task = speculative_prepare_task
            speculative_prepare_task = None
            if spec_task is not None:
                if not spec_task.done():
                    spec_task.cancel()
                try:
                    await spec_task
                except BaseException:
                    pass

        try:
            game_ctx = None
            if not item.private_direct:
                game_ctx = await group_ecosystem.read_game_context(
                    p, runtime.umo or runtime.session_key, query=str(turn.text or "")
                )
                if game_ctx.get("active"):
                    item = replace(item, observations={**item.observations, "active_game_context": game_ctx})
            private_outreach_observer = None
            private_outreach_scope = None
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
                # When narrative.prepare manages the private turn, route_life_events and
                # begin_send/finish_send already own journal ingress/delivery events.
                if bundle is None:
                    private_outreach_observer = getattr(p, "narrative", None)
                    if private_outreach_observer is not None and private_outreach_observer.outreach_allowed(runtime.umo):
                        private_outreach_scope = p._create_background_task(
                            private_outreach_observer.observe_private_turn(runtime, item, persona)
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
                semantic = reply_interaction(
                    turn, runtime.jev_decision.get("interaction_frame"), floor=p._runtime_config.jev_min_confidence
                )
                recipient = runtime.jev_decision.get("recipient", {})
                if (
                    semantic.get("turn_update", {}).get("choice") == "corrects_previous"
                    and recipient.get("choice") == "bot"
                    and recipient.get("confidence", 0) >= p._runtime_config.jev_min_confidence
                    and decision.action != "ignore"
                ):
                    # Attach classifier evidence to the routed copy, never rewrite
                    # the observed platform mention or quote on the original event.
                    payload = bundle.wire()
                    payload["feedback_speaker_id"] = turn.author
                    bundle = type(bundle)(bundle.turn_id, json.dumps(payload, ensure_ascii=False))
                    item = replace(item, narrative_bundle=bundle)
                    impact = "relationship"
                await narrative.route_life_events_async(bundle, impact, decision_evidence={
                    **runtime.jev_decision,
                    "social_context_current": (
                        runtime.epoch == turn.epoch
                        and runtime.user_revisions.get(turn.author, 0) == turn.revision
                        and not p._shutting_down and not item.shadow
                    ),
                    "social_stop_revision": runtime.topic_stop_revisions.get(turn.author, 0),
                }, event=item.events[-1] if item.events else None)
                revised = await narrative.advance_important(item, bundle, impact)
                if revised.payload != bundle.payload:
                    bundle = revised
                    item = replace(item, narrative_bundle=bundle)
                    speculative_prepare_mutated = True
                    await _cancel_speculative_prepare()
                    if not item.private_direct:
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
                    and decision.reason_code not in _WAITING_REASONS
                    and reason not in {"other_recipient", "boundary_or_sensitive"}
                    and not item.observations.get("context_completion", {}).get("attempted")
                    and not any(set(m.mentioned_users) - {turn.bot_id} for m in turn.messages)
                ):
                    speculative_prepare_mutated = True
                    await _cancel_speculative_prepare()
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
                    dynamics_horizons.begin(p, runtime, item, judgement_persona)
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
                        reason_code=_decision_prefix(p) + "context_clarify",
                        response_goal="当前仍缺少作答所需的关键信息。只问一个具体、必要的澄清问题，不猜测缺失内容，不声称未检索到的事情从未发生。",
                    )
            if bundle is not None and decision.action != "ignore" and decision.reason_code not in _WAITING_REASONS:
                previous = bundle.wire()
                if speculative_prepare_task is not None and not speculative_prepare_mutated:
                    spec_task = speculative_prepare_task
                    speculative_prepare_task = None
                    full_bundle = await self.telemetry.run(
                        "prepare_reply", turn, spec_task, none_is_unavailable=True
                    )
                else:
                    await _cancel_speculative_prepare()
                    full_bundle = await self.telemetry.run(
                        "prepare_reply", turn, narrative.prepare(runtime, item, persona), none_is_unavailable=True
                    )
                if full_bundle is None:
                    self._finish_generation_failure(runtime, item, "narrative_prepare_unavailable", suppressed=True)
                    return
                bundle = full_bundle
                item = replace(item, narrative_bundle=bundle)
                material = bundle.wire()["character"]
                judgement_persona = replace(
                    persona, prompt=material["core_prompt"] + "\n" + material.get("group_rules", "")
                )
                dynamics_horizons.begin(p, runtime, item, judgement_persona)
                prior_view = previous.get("evidence_view")
                current_view = bundle.wire().get("evidence_view")
                if (
                    not item.private_direct
                    and (
                        previous["character"].get("fingerprint") != material.get("fingerprint")
                        or previous["snapshot"].get("revision") != bundle.snapshot.get("revision")
                        or previous["snapshot"].get("scope_revision") != bundle.snapshot.get("scope_revision")
                        or bundle.wire().get("availability", {}).get("supplement_context_status") == "conflicting"
                        or isinstance(prior_view, dict)
                        and isinstance(current_view, dict)
                        and prior_view.get("correction_revision") != current_view.get("correction_revision")
                    )
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
        finally:
            await _cancel_speculative_prepare()
        proposed = decision
        async with runtime.state_lock:
            if not await self.valid_async(runtime, item):
                return
            if not item.private_direct and decision.reason_code not in _WAITING_REASONS:
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
            if decision.reason_code in _WAITING_REASONS:
                dynamics_learning.finalize(runtime, item, decision)
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
                                runtime, _decision_prefix(p) + "incomplete_expired", action="ignore", reason_zh="补充输入等待已结束"
                            )

                soft_timeout_val = float(getattr(p._runtime_config, "pending_input_soft_timeout", 12.0) or 0.0)
                if soft_timeout_val > 0 and getattr(p.debounce, "pending_input_soft_timeout", None) is not None:
                    soft_timeout_val = float(p.debounce.pending_input_soft_timeout)
                use_soft_timeout = soft_timeout_val > 0 and _should_soft_timeout_wait(turn, item)

                async def soft_timed_out(_res):
                    if getattr(p, "_shutting_down", False):
                        return
                    async with runtime.state_lock:
                        if (
                            p._sessions.get(runtime.session_key) is not runtime
                            or runtime.epoch != turn.epoch
                            or runtime.user_revisions.get(turn.author, 0) != turn.revision
                            or self._is_turn_recalled(runtime, item)
                        ):
                            return
                    retry_item = replace(
                        item,
                        observations={
                            **item.observations,
                            "force_complete": True,
                            "wait_soft_timeout_seconds": soft_timeout_val,
                        },
                        debounce_result=None,
                    )
                    await self.submit(runtime, retry_item)

                deferred = await p.debounce.defer_result(
                    item.debounce_result,
                    on_expire=expired,
                    soft_timeout=soft_timeout_val if use_soft_timeout else None,
                    on_soft_timeout=soft_timed_out if use_soft_timeout else None,
                )
                reason = decision.reason_code if deferred else _decision_prefix(p) + "incomplete_expired"
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
                "llm_dialogue_answer", "llm_dialogue_continue", "llm_dialogue_reopen",
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
            opening_limit = participation_policy(group_profile_eff["presence_knob"])[
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
                    rhythm_sleep_awake = bool(
                        rhythm_woken_from_sleep
                        or rhythm_temp_awake_active
                        or (
                            rhythm_ctrl is not None
                            and callable(getattr(rhythm_ctrl, "is_temp_awake", None))
                            and rhythm_ctrl.is_temp_awake(runtime)
                        )
                    )
                    if availability == "asleep" and not sleep_wake_admitted and not rhythm_sleep_awake:
                        decision = replace(decision, action="ignore", reason_code="narrative_asleep")
                    elif (
                        availability in {"asleep", "busy"}
                        and sleep_wake_admitted
                        and decision.action != "ignore"
                        and not (availability == "busy" and rhythm_urgent_breakthrough)
                        and not (availability == "asleep" and rhythm_sleep_awake)
                    ):
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
                        public_topic=decision.reason_code in {"jev_open_group_topic", "llm_open_group_topic"},
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
                    action="acknowledge" if wake_admitted and proposed.action != "ignore" else "ignore",
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

            dynamics_learning.finalize(runtime, item, decision)

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
                action_answer = runtime.jev_decision.get("action", {})
                action_confidence = action_answer.get("confidence") if isinstance(action_answer, dict) else None
                if (
                    proposed.action == "ignore"
                    and isinstance(action_answer, dict)
                    and action_answer.get("type") == "choice"
                    and action_answer.get("choice") == "ignore"
                    and type(action_confidence) in (int, float)
                    and p._runtime_config.jev_min_confidence <= action_confidence <= 1
                ):
                    for node in item.outcome_nodes:
                        if node is not None:
                            node.metadata["reply_choice"] = {
                                "requested": "silent",
                                "actual": "silent",
                                "no_reply": True,
                                "reason": decision.reason_code,
                                "delivered": False,
                            }
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
                if runtime.jev_decision and decision.reason_code.startswith(("jev_", "llm_")):
                    # Both transports' response goal comes from the plugin's fixed
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
            if not await self.valid_async(runtime, item, stage="pre_generation_lock"):
                return
            if not await self.bridge.current(event, persona):
                record_outcome(runtime, item, outcomes.mark_suppressed, "persona_changed", stage="generation")
                return
            provider = await p.llm.resolve_provider_id(turn.session_key)
            async with runtime.state_lock:
                if not await self.valid_async(runtime, item, stage="pre_generation") or any(
                    _dag_node(runtime, message.message_id) is None for message in turn.messages
                ):
                    return
            understand = bool(getattr(runtime, "request_media_understand", False))
            context_images = runtime.recent_images.select(runtime, turn, event, p, p.time_service.time())
            if context_images:
                runtime.model_diagnostic["recent_image_source_ids"] = [source.node.msg_id for source in context_images]
                logger.info("[ChatDynamics] recent image context session=%s sources=%s", runtime.session_key,
                            ",".join(source.node.msg_id for source in context_images))
            try:
                reminder_context = p.group_memory.reminder_context(turn.session_key, now=p.time_service.wall_time())
            except Exception as exc:
                logger.warning("notebook context unavailable type=%s", type(exc).__name__)
                reminder_context = []
            record_outcome(runtime, item, outcomes.mark_in_flight)
            if bundle is not None:
                await narrative.attach_reply_evidence_async(item, decision, runtime.jev_decision)
            from contextlib import ExitStack

            def _release_reply_owner():
                owners = getattr(runtime, "reply_generation_owners", None)
                if isinstance(owners, dict):
                    rem = int(owners.get(turn.author, 0) or 0) - 1
                    if rem > 0:
                        owners[turn.author] = rem
                    else:
                        owners.pop(turn.author, None)
                        merge_counts = getattr(runtime, "reply_merge_counts", None)
                        if isinstance(merge_counts, dict):
                            merge_counts.pop(turn.author, None)
                        pending_fu = getattr(runtime, "pending_reply_followups", None)
                        if isinstance(pending_fu, dict):
                            pending_fu.pop(turn.author, None)

            culture_ctx = None
            if not item.private_direct:
                culture_ctx = await group_ecosystem.read_culture_context(
                    p, runtime.umo or runtime.session_key, query=str(turn.text or ""), limit=6
                )

            with ExitStack() as _reply_gen_scope:
                owners = getattr(runtime, "reply_generation_owners", None)
                if isinstance(owners, dict):
                    owners[turn.author] = int(owners.get(turn.author, 0) or 0) + 1
                    _reply_gen_scope.callback(_release_reply_owner)
                merge_counts = getattr(runtime, "reply_merge_counts", None)
                if isinstance(merge_counts, dict):
                    merge_counts[turn.author] = int(item.followup_merge_count)
                outer_task = asyncio.current_task()
                reply_tasks = getattr(runtime, "reply_generation_tasks", None)
                if outer_task is not None and isinstance(reply_tasks, set) and outer_task not in reply_tasks:
                    reply_tasks.add(outer_task)
                    _reply_gen_scope.callback(reply_tasks.discard, outer_task)

                slow_ack_sent = False
                slow_ack_eligible = (
                    not item.shadow
                    and (turn.mandatory_reply or turn.wake_kind == "quote" or item.private_direct)
                    and bool(getattr(p._runtime_config, "slow_turn_ack_enabled", True))
                    and float(getattr(p._runtime_config, "slow_turn_ack_seconds", 8.0) or 0.0) > 0.0
                )
                slow_ack_delay = float(getattr(p._runtime_config, "slow_turn_ack_seconds", 8.0) or 8.0)

                while True:
                    slow_ack_task: asyncio.Task | None = None
                    try:
                        p._metric("native_agent_started")
                        generation_started = time.monotonic()
                        if slow_ack_eligible and not slow_ack_sent:
                            async def _slow_turn_ack_watchdog(
                                _delay: float = slow_ack_delay,
                                _started: float = generation_started,
                                _ev: Any = event,
                                _item: ModelTurn = item,
                            ) -> None:
                                nonlocal slow_ack_sent
                                try:
                                    await asyncio.sleep(_delay)
                                except asyncio.CancelledError:
                                    return
                                if slow_ack_sent or getattr(p, "_shutting_down", False):
                                    return
                                if self._is_turn_recalled(runtime, _item) or not await self.valid_async(runtime, _item, stage="slow_ack"):
                                    return
                                fs_map = getattr(runtime, "reply_first_sentence_sent", None)
                                if isinstance(fs_map, dict) and int(fs_map.get(turn.author, 0) or 0) > 0:
                                    return
                                from .platform_bridge import try_send_typing_status

                                ack_mode = ""
                                typed = await try_send_typing_status(
                                    _ev,
                                    group_id=str(getattr(runtime, "group_id", "") or ""),
                                    user_id=str(turn.author or ""),
                                )
                                if typed:
                                    ack_mode = "typing"
                                else:
                                    async with runtime.send_lock:
                                        if (
                                            slow_ack_sent
                                            or self._is_turn_recalled(runtime, _item)
                                            or not await self.valid_async(runtime, _item, stage="slow_ack")
                                        ):
                                            return
                                        sent_ack = await p._send_owned(runtime, _ev, "稍等一下，我在看…")
                                        if sent_ack.success:
                                            ack_mode = "micro_ack"
                                if ack_mode:
                                    slow_ack_sent = True
                                    elapsed = max(0.0, time.monotonic() - _started)
                                    p._metric("slow_turn_ack_sent")
                                    for nd in _item.outcome_nodes:
                                        if nd is not None and isinstance(getattr(nd, "metadata", None), dict):
                                            nd.metadata["slow_turn_ack"] = {
                                                "mode": ack_mode,
                                                "elapsed_seconds": round(elapsed, 2),
                                            }

                            slow_ack_task = asyncio.create_task(_slow_turn_ack_watchdog())

                        frame, tone_rows = tone_context.for_reply(turn, runtime.jev_decision.get("interaction_frame"), p, event)
                        reply_prompt_json = group_ecosystem.augment_reply_prompt(
                            dynamics_horizons.augment_reply(
                                await scene_learning.prepare_all(reply_prompt(
                                    turn,
                                    decision,
                                    interaction_frame=frame,
                                    tone_context=tone_rows,
                                    interaction_floor=p._runtime_config.jev_min_confidence,
                                    conversation_map=conversation_map(
                                        turn, getattr(runtime, "jev_addressees", None), floor=p._runtime_config.jev_min_confidence
                                    ),
                                    delivery_constraints={
                                        "length_hint": decision.length,
                                        "rhythm_state": str(getattr(getattr(gate, "rhythm", None), "state", "")),
                                        "rhythm_action": str(getattr(getattr(gate, "rhythm", None), "action", "")),
                                        **({"rhythm_mode": "catchup"} if item.observations.get("rhythm_catchup") else {}),
                                    },
                                    reply_guidance=p._runtime_config.reply_prompt,
                                    group_reminders=reminder_context,
                                    character_context=(
                                        {"horizons": h_slot}
                                        if bundle is None and (h_slot := dynamics_horizons.decision_slot(runtime, item))
                                        else None
                                    ),
                                    include_reply_guidance=bool(str(p._runtime_config.reply_prompt or "").strip()),
                                ), runtime, item),
                                runtime,
                                item,
                            ),
                            game_context=game_ctx,
                            culture_context=culture_ctx,
                        )
                        outbound_note = _read_outbound_ledger_note(p, runtime, item, event)
                        turn_notes = tuple(
                            note for note in (outbound_note, rhythm_sleep_wake_note) if note
                        )
                        generate_kwargs: dict[str, Any] = {
                            "execution_log": runtime.tool_executions,
                            "history_text": turn.history_text(),
                            "media_understand": understand,
                            "context_images": context_images,
                            "private_direct": item.private_direct,
                            "deferred_reply_media": deferred_media,
                        }
                        if turn_notes:
                            generate_kwargs["turn_notes"] = turn_notes
                        output = await self.generate_ready(
                            runtime,
                            turn,
                            event,
                            item.events,
                            reply_prompt_json,
                            persona,
                            provider,
                            **generate_kwargs,
                        )
                    except ModelReplyFailure as exc:
                        p._metric("native_agent_failed")
                        record_outcome(runtime, item, outcomes.mark_generation_failed, exc.reason_code)
                        self.diagnostic(runtime, exc.reason_code, reason_zh="回复模型未提供可发送内容")
                        p._metric("llm_reply_unavailable")
                        logger.warning(
                            "[ChatDynamics] reply generation failed session=%s turn=%s reason=%s private=%s",
                            getattr(event, "unified_msg_origin", ""),
                            _turn_log_id(runtime, item) or getattr(getattr(item, "narrative_bundle", None), "turn_id", "") or "unknown",
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
                    finally:
                        if slow_ack_task is not None and not slow_ack_task.done():
                            slow_ack_task.cancel()
                            await asyncio.gather(slow_ack_task, return_exceptions=True)

                    # Smart follow-up merge: if the same speaker sent substantive follow-up(s)
                    # before the first sentence was sent, merge and regenerate at most ONCE.
                    merged_followups: list[dict[str, Any]] = []
                    if item.followup_merge_count < 1:
                        pending_map = getattr(runtime, "pending_reply_followups", None)
                        if isinstance(pending_map, dict) and pending_map.get(turn.author):
                            async with runtime.state_lock:
                                raw_followups = pending_map.pop(turn.author, [])
                                valid_fu = [
                                    fu for fu in raw_followups
                                    if isinstance(fu, dict)
                                    and str(fu.get("text") or "").strip()
                                    and not (
                                        fu.get("message_id")
                                        and callable(getattr(runtime, "is_recalled", None))
                                        and runtime.is_recalled(str(fu.get("message_id")))
                                    )
                                ]
                                if valid_fu and await self.valid_async(runtime, item, stage="followup_merge"):
                                    merged_followups = valid_fu
                                    if isinstance(merge_counts, dict):
                                        merge_counts[turn.author] = item.followup_merge_count + 1
                                    merged_id_set: set[str] = set()
                                    new_msgs = list(turn.messages)
                                    new_events = list(item.events)
                                    new_nodes = list(item.outcome_nodes)
                                    new_plat_ids = set(item.platform_message_ids)
                                    extra_texts: list[str] = []
                                    for fu in merged_followups:
                                        fu_text = str(fu.get("text") or "").strip()
                                        fu_mid = str(fu.get("message_id") or "").strip() or p._next_outgoing_id()
                                        fu_ts = float(fu.get("timestamp") or p.time_service.time())
                                        fu_ev = fu.get("event")
                                        extra_texts.append(fu_text)
                                        merged_id_set.add(fu_mid)
                                        if callable(getattr(runtime, "remember_merged_followup", None)):
                                            runtime.remember_merged_followup(fu_mid)
                                        if fu.get("message_id"):
                                            new_plat_ids.add(fu_mid)
                                        node = _dag_node(runtime, fu_mid)
                                        if node is None and getattr(runtime, "dag", None) is not None:
                                            node = runtime.dag.add_message(
                                                msg_id=fu_mid,
                                                user_id=turn.author,
                                                text=fu_text,
                                                timestamp=fu_ts,
                                                metadata={"platform_message_id": bool(fu.get("message_id"))},
                                            )
                                        if node is not None:
                                            new_nodes.append(node)
                                        if fu_ev is not None:
                                            new_events.append(fu_ev)
                                        new_msgs.append(
                                            MessageSnapshot(
                                                message_id=fu_mid,
                                                author=turn.author,
                                                text=fu_text,
                                                timestamp=fu_ts,
                                                author_name=str(fu.get("sender_name") or ""),
                                                platform=str(fu.get("platform") or ""),
                                            )
                                        )
                                    if callable(getattr(runtime, "drop_merged_model_turns", None)):
                                        runtime.drop_merged_model_turns(turn.author, merged_id_set)
                                    await p.debounce.discard(runtime.session_key, user_id=turn.author)
                                    merge_trace = {
                                        "count": len(merged_followups),
                                        "message_ids": sorted(merged_id_set)[:4],
                                        "snippets": [t[:120] for t in extra_texts[:4]],
                                    }
                                    for orig_node in item.outcome_nodes:
                                        if orig_node is not None and isinstance(getattr(orig_node, "metadata", None), dict):
                                            orig_node.metadata["merged_followups"] = dict(merge_trace)
                                    merged_text = "\n".join([turn.text, *extra_texts])
                                    turn = replace(
                                        turn,
                                        text=merged_text,
                                        messages=tuple(new_msgs),
                                        snapshot_at=p.time_service.time(),
                                    )
                                    item = replace(
                                        item,
                                        context=turn,
                                        events=tuple(new_events),
                                        outcome_nodes=tuple(new_nodes),
                                        platform_message_ids=frozenset(new_plat_ids),
                                        followup_merge_count=item.followup_merge_count + 1,
                                    )
                                    event = item.events[-1] if item.events else event
                                    decision = replace(
                                        decision,
                                        target_message_ids=(turn.messages[-1].message_id, *decision.target_message_ids),
                                    )
                    if merged_followups:
                        completion = getattr(output, "completion_task", None)
                        if completion is not None and not completion.done():
                            completion.cancel()
                            await asyncio.gather(completion, return_exceptions=True)
                        p._metric("followup_merged_regeneration")
                        logger.info(
                            "[ChatDynamics] merged %d follow-up(s) before first send; regenerating once session=%s turn=%s",
                            len(merged_followups),
                            runtime.session_key,
                            _turn_log_id(runtime, item),
                        )
                        continue
                    break
                reply_mode = getattr(output, "reply_mode", "")
                narration = str(getattr(output, "narration", "") or "")
                reply_reason = getattr(output, "reply_reason", "")
                requested_poke_target = str(getattr(output, "poke_target_uid", "") or "").strip()
                poke_target = ""
                if reply_mode == "narration" and not turn.mandatory_reply:
                    # An admitted ambient turn is not permission to publish a no-reply notice.
                    reply_mode = "silent"
                    narration = ""
                    reply_reason = "narration_requires_at"
                poke_target_invalid = False
                if reply_mode in {"poke", "poke_text", "poke_meme"}:
                    poke_target = _resolve_poke_target(turn, event, str(runtime.bot_id or ""), requested_poke_target)
                    if not poke_target:
                        poke_target_invalid = True
                        if reply_mode == "poke_text":
                            reply_mode = "text"
                            reply_reason = "invalid_poke_target"
                        elif reply_mode == "poke_meme":
                            reply_mode = "meme_only"
                            reply_reason = "invalid_poke_target"
                nonverbal_mode = reply_mode in {"narration", "poke"}
                if reply_mode:
                    if deferred_media is not None:
                        deferred_media.cancel()
                    choice = {
                        "requested": getattr(output, "requested_reply_mode", reply_mode),
                        "actual": reply_mode,
                        "reason": reply_reason,
                        "no_reply": reply_mode == "silent",
                        "intentional_nonverbal": nonverbal_mode,
                        "delivered": False,
                    }
                    if reply_mode == "narration":
                        choice["no_reply"] = False
                        choice["narration"] = True
                    if poke_target and reply_mode in {"poke", "poke_text", "poke_meme"}:
                        choice["poke_target_uid"] = poke_target
                    for node in item.outcome_nodes:
                        if node is not None:
                            node.metadata["reply_choice"] = dict(choice)
                    # Later ingress may replace the session diagnostic while a send
                    # is awaiting its receipt. Keep this turn's choice by reference.
                    runtime.model_diagnostic["reply_choice"] = choice
                    if reply_mode == "silent":
                        record_outcome(
                            runtime, item, outcomes.mark_suppressed, choice["reason"] or "model_silent", stage="generation"
                        )
                        p._metric("speech_withheld")
                        return
                    if reply_mode == "narration" and not narration:
                        record_outcome(runtime, item, outcomes.mark_delivery_failed, "empty_narration")
                        return
                    if reply_mode == "poke" and (poke_target_invalid or not poke_target):
                        record_outcome(runtime, item, outcomes.mark_delivery_failed, "invalid_poke_target")
                        return
                if not getattr(output, "stream_prefix", ""):
                    if output.text or output.chains or nonverbal_mode:
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
                if not await self.valid_async(runtime, item, stage="post_generation"):
                    return
            rhythm_action = str(
                getattr(getattr(gate, "rhythm", None), "action", "") or getattr(runtime, "last_rhythm_action", "")
            )
            max_fragments = 1 if is_rhythm_short_act(rhythm_action) else p._runtime_config.max_fragments
            fragments = []
            from .stream_reply import output_fragments

            delivered = []
            verbal_delivered = []
            history_delivered = []
            delivered_message_ids = []
            poke_delivered_in_turn = False
            first_verbal_committed = False
            first_sentence_marked = False
            send_cancellation = None
            send_failed_in_turn = False
            delivery_uncertain_in_turn = False
            committed = False
            last_bot_msg_id = ""
            target_id = decision.target_message_ids[0]
            target_node = _dag_node(runtime, target_id)
            dag_parent_id = target_id if target_node is not None else None
            platform_target_id = _platform_reply_id(runtime, item, target_id)
            first_quote_id = (
                platform_target_id
                if (item.observations.get("rhythm_force_quote") and platform_target_id)
                else _automatic_group_reply_id(runtime, item, platform_target_id)
            )
            send_options = {}
            target_author = getattr(target_node, "user_id", None)
            if target_author and target_author not in {turn.author, runtime.bot_id}:
                send_options["boundary_target_uid"] = target_author
            delay_scale = float(getattr(runtime, "last_delay_scale", 1.0) or 1.0)
            if gate is not None:
                delay_scale = float(gate.delay_scale or delay_scale)
            try:
                delivery_output = output
                if reply_mode == "narration":
                    from .platform_bridge import build_plain_chain

                    delivery_output = replace(
                        output,
                        text="",
                        chains=[build_plain_chain(narration)],
                        stream_prefix="",
                    )
                elif reply_mode == "poke":
                    from .platform_bridge import build_poke_chain

                    delivery_output = replace(
                        output,
                        text="",
                        chains=[build_poke_chain(poke_target)],
                        stream_prefix="",
                    )
                elif reply_mode == "poke_text":
                    from .platform_bridge import build_poke_chain

                    delivery_output = replace(
                        output,
                        chains=[build_poke_chain(poke_target), *list(output.chains or ())],
                        stream_prefix="",
                    )
                elif reply_mode == "poke_meme":
                    from .platform_bridge import build_poke_chain

                    delivery_output = replace(
                        output,
                        text="",
                        chains=[build_poke_chain(poke_target), *list(output.chains or ())],
                        stream_prefix="",
                    )
                elif reply_mode == "text_meme" and output.chains:
                    delivery_output = replace(
                        output,
                        chains=split_text_meme_chains(
                            output.chains, p.pacer, max_fragments=max_fragments
                        ),
                        stream_prefix="",
                    )
                async for index, fragment in output_fragments(
                    delivery_output,
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
                    is_poke_fragment = _is_poke_chain(fragment)
                    fragment_nonverbal = nonverbal_mode or is_poke_fragment
                    frag_text = fragment if isinstance(fragment, str) else chain_plain_text(fragment)
                    if rhythm_ctrl is not None and rhythm_ctrl.micro_pacing_enabled():
                        from .vibe_analyzer import GroupChatMode as _GCM
                        cur_mpm = 0.0
                        cur_mode = _GCM.CHILL_FADE
                        try:
                            cur_mpm = float(p.vibe_analyzer.get_telemetrics(runtime.session_key).mpm or 0.0)
                            cur_mode = p.vibe_analyzer.peek_mode(runtime.session_key) or _GCM.CHILL_FADE
                        except Exception:
                            pass
                        if index == 0:
                            if item.observations.get("rhythm_skip_micro_delay"):
                                delay = 0.0
                            else:
                                idle_gap = (
                                    max(0.0, p.time_service.time() - float(runtime.last_model_send or 0.0))
                                    if getattr(runtime, "last_model_send", 0.0)
                                    else 300.0
                                )
                                target_delay = p.pacer.calculate_online_target_delay(
                                    first_fragment=frag_text,
                                    input_text=str(turn.text or ""),
                                    mode=cur_mode,
                                    idle_seconds=idle_gap,
                                    mpm=cur_mpm,
                                    delay_scale=delay_scale,
                                    jitter_seed=f"{turn.session_key}:{target_id}",
                                )
                                elapsed_gen = max(0.0, time.monotonic() - processing_started)
                                deficit = target_delay - elapsed_gen
                                delay = round(deficit, 2) if deficit > 0.05 else 0.0
                        else:
                            delay = p.pacer.calculate_inter_burst_delay(
                                mode=cur_mode,
                                fragment_text=frag_text,
                                mpm=cur_mpm,
                                delay_scale=delay_scale,
                                jitter=True,
                                jitter_seed=f"{turn.session_key}:{target_id}:{index}",
                            )
                    else:
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
                        if self._is_turn_recalled(runtime, item):
                            record_outcome(runtime, item, outcomes.mark_suppressed, "message_recalled", stage="delivery")
                            break
                        if not await self.valid_async(runtime, item, stage="delivery"):
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
                        quote_id = first_quote_id if not first_verbal_committed and not fragment_nonverbal else None
                        fragment_quote_id = quote_id
                        if fragment_quote_id and any(
                            _comp_type_name(component).lower() in {"reply", "file", "node", "nodes", "poke"}
                            for component in getattr(fragment, "chain", ())
                        ):
                            fragment_quote_id = None
                        fragment_send_options = dict(send_options)
                        if is_poke_fragment and poke_target and poke_target != turn.author:
                            fragment_send_options["boundary_target_uid"] = poke_target
                        try:
                            event_extras = getattr(event, "_extras", None)
                            previous_nonverbal_marker = None
                            if fragment_nonverbal:
                                if not isinstance(event_extras, dict):
                                    event_extras = {}
                                    event._extras = event_extras
                                previous_nonverbal_marker = event_extras.get("_chat_dynamics_nonverbal_send_v1")
                                event_extras["_chat_dynamics_nonverbal_send_v1"] = (
                                    "poke" if is_poke_fragment else reply_mode
                                )
                            try:
                                if send_source_validator is None:
                                    sent = await p._send_owned(
                                        runtime, event, fragment, reply_to_id=fragment_quote_id, **fragment_send_options
                                    )
                                else:
                                    sent = await p._send_owned(
                                        runtime,
                                        event,
                                        fragment,
                                        reply_to_id=fragment_quote_id,
                                        native_source_validator=output.native_sources_current,
                                        **fragment_send_options,
                                    )
                            finally:
                                if fragment_nonverbal:
                                    if previous_nonverbal_marker is None:
                                        event_extras.pop("_chat_dynamics_nonverbal_send_v1", None)
                                    else:
                                        event_extras["_chat_dynamics_nonverbal_send_v1"] = previous_nonverbal_marker
                        except asyncio.CancelledError as exc:
                            sent = getattr(exc, "chat_dynamics_send_result", None)
                            if sent is None or not sent.success:
                                raise
                            send_cancellation = exc
                        if not sent.success and sent.error == "joint_meme_candidate_unavailable" and reply_mode:
                            fallback_text = getattr(fragment, "_cd_meme_fallback_text_v1", "")
                            text_already_sent = bool(
                                getattr(fragment, "_cd_meme_text_already_sent_v1", False)
                            ) and bool(verbal_delivered)
                            if fallback_text:
                                fragment = fallback_text
                                sent = await p._send_owned(
                                    runtime,
                                    event,
                                    fragment,
                                    reply_to_id=quote_id,
                                    native_source_validator=output.native_sources_current,
                                    **send_options,
                                )
                                actual = "text"
                            elif text_already_sent:
                                actual = "text"
                            elif poke_delivered_in_turn:
                                actual = "poke"
                            else:
                                actual = "silent"
                            for node in item.outcome_nodes:
                                if node is not None:
                                    node.metadata["reply_choice"].update(
                                        actual=actual,
                                        no_reply=actual == "silent",
                                        reason="meme_unavailable",
                                        delivered=True
                                        if (actual == "poke" or text_already_sent)
                                        else node.metadata["reply_choice"].get("delivered", False),
                                        intentional_nonverbal=actual == "poke",
                                    )
                            choice.update(
                                actual=actual,
                                no_reply=actual == "silent",
                                reason="meme_unavailable",
                                delivered=True
                                if (actual == "poke" or text_already_sent)
                                else choice.get("delivered", False),
                                intentional_nonverbal=actual == "poke",
                            )
                            if actual == "poke" or text_already_sent:
                                break
                            if actual == "silent":
                                p._metric("speech_withheld")
                                record_outcome(
                                    runtime, item, outcomes.mark_suppressed, "meme_unavailable", stage="delivery"
                                )
                                break
                    if not sent.success:
                        send_failed_in_turn = True
                        if getattr(sent, "delivery_uncertain", False):
                            delivery_uncertain_in_turn = True
                        if sent.error == "native_memory_source_invalidated":
                            record_outcome(
                                runtime, item, outcomes.mark_suppressed,
                                "native_memory_source_invalidated", stage="delivery",
                            )
                            break
                        if sent.error == "boundary_policy_cancelled":
                            record_outcome(
                                runtime, item, outcomes.mark_suppressed, "boundary_policy_changed", stage="delivery"
                            )
                            p._metric("boundary_send_cancelled")
                            break
                        record_outcome(runtime, item, outcomes.mark_delivery_failed, "send_failed")
                        p._metric("send_failed")
                        break
                    if reply_mode:
                        for node in item.outcome_nodes:
                            if node is not None:
                                node.metadata["reply_choice"]["delivered"] = True
                        choice["delivered"] = True
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
                    delivered_text = "[戳一戳]" if is_poke_fragment else sent.delivered_text(fragment)
                    delivered.append(delivered_text)
                    history_entry = _format_fragment_for_history(
                        fragment,
                        delivered_text,
                        is_poke=is_poke_fragment,
                        reply_mode=reply_mode,
                        poke_target=poke_target,
                        turn=turn,
                        runtime=runtime,
                        private_direct=bool(item.private_direct),
                    )
                    if history_entry:
                        history_delivered.append(history_entry)
                    if not first_sentence_marked:
                        first_sentence_marked = True
                        fs_map = getattr(runtime, "reply_first_sentence_sent", None)
                        if isinstance(fs_map, dict):
                            fs_map[turn.author] = int(fs_map.get(turn.author, 0) or 0) + 1
                    if is_poke_fragment:
                        poke_delivered_in_turn = True
                    if not fragment_nonverbal:
                        if isinstance(fragment, str):
                            verbal_delivered.append(delivered_text)
                        else:
                            plain_only = _chain_visible_plain_text(fragment)
                            if plain_only:
                                verbal_delivered.append(plain_only)
                    for actual_id in _actual_send_message_ids(sent, fragment, delivered_text):
                        if actual_id not in delivered_message_ids:
                            delivered_message_ids.append(actual_id)
                    if not fragment_nonverbal:
                        for message_id, text in sent.identified_messages(fragment, delivered_text):
                            dynamics_learning.delivered(runtime, item, message_id, text)
                    p._metric("send_succeeded")
                    if runtime.epoch != turn.epoch:
                        break
                    real_message_id = str(sent.message_id) if sent.message_id else None
                    msg_id = real_message_id or p._next_outgoing_id()
                    bot_metadata = {
                        "platform_message_id": real_message_id is not None,
                        "dialogue_delivered": not fragment_nonverbal,
                        "reply_mode": "poke" if is_poke_fragment else (reply_mode or "text"),
                        "narration": reply_mode == "narration",
                        "trigger_user_id": turn.author,
                        "dialogue_stop_revision": runtime.topic_stop_revisions.get(turn.author, 0),
                        "dialogue_state": decision.state,
                        "dialogue_action": decision.action,
                        "wall_timestamp": p.time_service.wall_time(),
                    }
                    if is_poke_fragment:
                        bot_metadata["has_poke"] = True
                        bot_metadata["is_bot_poke"] = True
                        bot_metadata["poke_target_id"] = poke_target
                    bot = runtime.dag.add_message(
                        msg_id=msg_id,
                        user_id=runtime.bot_id,
                        text=delivered_text,
                        timestamp=p.time_service.time(),
                        reply_to_id=dag_parent_id,
                        metadata=bot_metadata,
                    )
                    last_bot_msg_id = msg_id
                    bot.metadata["platform_message_id"] = real_message_id is not None
                    if is_poke_fragment:
                        record_poke_fn = getattr(p, "record_bot_poke", None)
                        if callable(record_poke_fn):
                            try:
                                record_poke_fn(
                                    runtime.session_key,
                                    bot_id=str(runtime.bot_id or ""),
                                    target_id=poke_target,
                                    now=p.time_service.time(),
                                )
                            except Exception:
                                pass
                    if fragment_nonverbal:
                        runtime.remember_sent(msg_id)
                        runtime.last_model_send = p.time_service.time()
                        continue
                    check_sources = getattr(output, "native_sources_current", None)
                    if (send_cancellation is not None or sent.error
                            or callable(check_sources) and not await check_sources()):
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
                    if not first_verbal_committed:
                        first_verbal_committed = True
                        if private_outreach_scope is not None:
                            p._create_background_task(private_outreach_observer.observe_private_delivery(
                                private_outreach_scope, sent.delivered_text(fragment, media_placeholder=False),
                                msg_id, p.time_service.wall_time(),
                            ))
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
                if first_sentence_marked:
                    fs_map = getattr(runtime, "reply_first_sentence_sent", None)
                    if isinstance(fs_map, dict):
                        rem_fs = int(fs_map.get(turn.author, 0) or 0) - 1
                        if rem_fs > 0:
                            fs_map[turn.author] = rem_fs
                        else:
                            fs_map.pop(turn.author, None)
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
                    full_delivered_text = _join_history_delivered(history_delivered) or (
                        "\n\n".join(verbal_delivered) if verbal_delivered else "\n\n".join(delivered)
                    )
                    committed = await self.bridge.commit(event, output, full_delivered_text)
                    if not committed:
                        self.diagnostic(runtime, "history_conflict")
                    if verbal_delivered and not nonverbal_mode:
                        dynamics_horizons.delivered(
                            p,
                            runtime,
                            item,
                            "\n\n".join(verbal_delivered),
                            message_id=str(getattr(getattr(runtime, "last_bot_node", None), "msg_id", "") or ""),
                            message_ids=delivered_message_ids,
                        )
                if send_cancellation is not None:
                    raise send_cancellation
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
                deferred_media.confirm(
                    getattr(output, "native_sources_current", None),
                    text="\n\n".join(verbal_delivered) if verbal_delivered else "\n\n".join(delivered),
                )
            if rhythm_ctrl is not None and verbal_delivered and not nonverbal_mode:
                all_sent = bool(
                    fragments
                    and len(delivered) == len(fragments)
                    and not send_failed_in_turn
                    and send_cancellation is None
                )
                target_topic = str((getattr(target_node, "metadata", None) or {}).get("routing", {}).get("topic_id") or "")
                high_affection = False
                if bundle is not None:
                    try:
                        hl = bundle.snapshot.get("humanlike") or {}
                        rel = hl.get("relationship") or {}
                        high_affection = str(rel.get("stage") or "").lower() in {"close", "intimate", "warm", "trusted"} or float(rel.get("affection", 0) or 0) >= 70
                    except Exception:
                        high_affection = False
                rhythm_ctrl.on_turn_delivered(
                    runtime,
                    anchor_bot_msg_id=last_bot_msg_id or str(getattr(getattr(runtime, "last_bot_node", None), "msg_id", "") or ""),
                    trigger_msg_id=str(target_id or ""),
                    target_user_id=str(turn.author or ""),
                    anchor_topic_id=target_topic,
                    delivered_text="\n\n".join(verbal_delivered),
                    persona_fingerprint=effective.fingerprint,
                    private_direct=bool(item.private_direct),
                    all_fragments_delivered=all_sent,
                    delivery_uncertain=delivery_uncertain_in_turn,
                    decision_action=str(decision.action or "reply"),
                    rhythm_action=rhythm_action,
                    is_afterthought=bool(item.observations.get("rhythm_afterthought")),
                    high_affection=high_affection,
                )

    def _synthetic_rhythm_event(
        self,
        runtime,
        *,
        target_user_id: str = "",
        trace_turn_id: str = "",
        trace_bundle_id: str = "",
    ) -> Any:
        """Build a lightweight event bound to runtime.umo for background catch-up/afterthought."""
        p = self.plugin
        event = None
        narrative = getattr(p, "narrative", None)
        if narrative is not None and callable(getattr(narrative, "internal_event", None)):
            parts = str(runtime.umo or runtime.session_key).split(":", 2)
            platform = parts[0] if parts and parts[0] else "default"
            scope = {"platform": platform, "umo": runtime.umo or runtime.session_key}
            try:
                event = narrative.internal_event(scope)
            except Exception:
                event = None
        if event is None:
            from types import SimpleNamespace

            umo = runtime.umo or runtime.session_key

            async def _send(chain):
                return await p.context.send_message(umo, chain)

            event = SimpleNamespace(
                unified_msg_origin=umo,
                session_id=runtime.session_key,
                get_sender_id=lambda: str(runtime.bot_id or "bot"),
                get_group_id=lambda: str(runtime.group_id or ""),
                get_platform_name=lambda: umo.split(":", 1)[0] if ":" in umo else "default",
                send=_send,
                _extras={},
            )
        if getattr(event, "_extras", None) is None:
            try:
                event._extras = {}
            except Exception:
                pass
        clean_uid = str(target_user_id or "").strip()
        clean_turn = str(trace_turn_id or "").strip()
        clean_bundle = str(trace_bundle_id or clean_turn).strip()
        try:
            setattr(event, "_chat_dynamics_boundary_uid", clean_uid)
            if clean_turn or clean_bundle:
                setattr(event, "_chat_dynamics_trace_turn_id", clean_turn)
                setattr(event, "_chat_dynamics_trace_bundle_id", clean_bundle)
                extras = getattr(event, "_extras", None)
                if isinstance(extras, dict):
                    extras["_chat_dynamics_trace_v1"] = {
                        "turn_id": clean_turn,
                        "bundle_id": clean_bundle,
                    }
        except Exception:
            pass
        return event

    def _prepare_synthetic_boundary(self, event: Any, target_user_id: str) -> str | None:
        """Validate/capture member boundary metadata on synthetic rhythm events.

        Returns a skip reason string when the turn must be skipped cleanly, else None.
        """
        p = self.plugin
        clean_uid = str(target_user_id or "").strip()
        state = member_boundary.resolve_state(p, event)
        if not clean_uid and state.required:
            return "boundary_uid_missing"
        if not state.available:
            if member_boundary.fail_closed(state):
                return "boundary_unavailable"
            return None
        if state.disabled or not clean_uid:
            return None
        policy, token, policy_state = member_boundary.peek(p, event, clean_uid)
        if not policy_state.available and member_boundary.fail_closed(policy_state):
            return "boundary_unavailable"
        if member_boundary.blocked(policy):
            return "boundary_blocked"
        try:
            setattr(event, "_chat_dynamics_boundary_uid", clean_uid)
            setattr(event, "_chat_dynamics_boundary_token", token)
        except Exception:
            pass
        return None

    async def execute_catchup_item(
        self,
        runtime,
        plan_item,
        *,
        live_event=None,
        live_events: Sequence[Any] = (),
        events_by_id: dict | None = None,
    ) -> dict:
        """Run one chronological catch-up item through the standard PersonaEngine pipeline."""
        from .platform_bridge import iter_message_components

        p = self.plugin
        dag = getattr(runtime, "dag", None)
        if dag is None:
            return {"status": "skipped", "reason": "rhythm_catchup_skipped_unworthy"}
        nodes = [dag.get_node(mid) for mid in plan_item.message_ids]
        nodes = [n for n in nodes if n is not None]
        if not nodes:
            return {"status": "skipped", "reason": "rhythm_catchup_skipped_recalled"}
        catchup_events = tuple(ev for ev in live_events if ev is not None)
        if not catchup_events:
            if live_event is not None:
                catchup_events = (live_event,)
            else:
                target_uid = str(getattr(plan_item, "author_id", "") or "").strip()
                trace_id = f"catchup:{getattr(plan_item, 'primary_msg_id', '') or nodes[-1].msg_id}"
                synthetic = self._synthetic_rhythm_event(
                    runtime,
                    target_user_id=target_uid,
                    trace_turn_id=trace_id,
                    trace_bundle_id=trace_id,
                )
                skip_reason = self._prepare_synthetic_boundary(synthetic, target_uid)
                if skip_reason is not None:
                    return {"status": "skipped", "reason": f"rhythm_catchup_skipped_{skip_reason}"}
                catchup_events = (synthetic,)
        for ev in catchup_events:
            if getattr(ev, "_extras", None) is None:
                ev._extras = {}

        _media_kinds = frozenset({"image", "record", "audio", "voice", "video", "file", "face", "sticker"})
        by_id = events_by_id or {}

        def _attachments_for(node_obj) -> tuple[str, ...]:
            mid = str(node_obj.msg_id)
            evs = by_id.get(mid)
            if not evs and mid == plan_item.primary_msg_id:
                evs = catchup_events
            found: list[str] = []
            for ev in evs or ():
                for comp in iter_message_components(ev):
                    cname = type(comp).__name__.lower()
                    if cname == "reply":
                        for inner_name in _iter_reply_media_types(comp):
                            inner_lower = inner_name.lower()
                            if inner_lower not in found:
                                found.append(inner_lower)
                    elif cname in _media_kinds and cname not in found:
                        found.append(cname)
            return tuple(found)

        msgs = tuple(
            MessageSnapshot(
                message_id=str(n.msg_id),
                author=str(n.user_id),
                text=str(n.text or ""),
                reply_to=str(n.reply_to_id or ""),
                attachments=_attachments_for(n),
                timestamp=float(n.timestamp),
                mentioned_users=tuple(str(u) for u in (getattr(n, "mentioned_users", ()) or ())),
                author_name=str((getattr(n, "metadata", None) or {}).get("sender_name") or ""),
                platform=str((getattr(n, "metadata", None) or {}).get("sender_platform") or ""),
            )
            for n in nodes
        )
        all_media_types = [kind for m in msgs for kind in m.attachments]
        consolidated = "\n".join(str(n.text or "").strip() for n in nodes if str(n.text or "").strip())
        has_media = bool(all_media_types) or any(
            marker in consolidated for marker in ("[媒体", "[图片", "[语音", "[视频", "[文件")
        )
        if plan_item.private_direct:
            runtime.request_media_understand = False
            if has_media:
                gate = getattr(p, "decision_gate", None)
                media_gate = getattr(gate, "media", None) if gate is not None else None
                cfg = getattr(p, "_runtime_config", None)
                if media_gate is not None and cfg is not None:
                    try:
                        verdict = media_gate.evaluate(
                            text=consolidated[:8000],
                            has_media=True,
                            media_component_types=all_media_types,
                            outline="",
                            explicit=True,
                            quoted_bot=any(bool(m.reply_to) for m in msgs),
                            media_privacy_strict=cfg.media_privacy_strict,
                            media_image_gate_enabled=cfg.media_image_gate_enabled,
                            media_voice_gate_enabled=cfg.media_voice_gate_enabled,
                            media_understand_reply_enabled=cfg.media_understand_reply_enabled,
                        )
                        runtime.last_media_gate = verdict.as_dict()
                        runtime.request_media_understand = bool(verdict.request_understand)
                        if verdict.privacy_hit:
                            return {"status": "skipped", "reason": "rhythm_catchup_skipped_stopped"}
                    except Exception:
                        runtime.request_media_understand = False
                elif cfg is not None:
                    runtime.request_media_understand = bool(getattr(cfg, "media_understand_reply_enabled", False))
        background = _snapshot_background(runtime, nodes)
        visible_ids = frozenset(str(n.msg_id) for n in dag.get_recent_nodes(limit=0))
        turn = TurnContext(
            runtime.session_key,
            plan_item.author_id,
            consolidated[:8000],
            msgs,
            background,
            runtime.epoch,
            runtime.user_revisions.get(plan_item.author_id, 0),
            float(nodes[0].timestamp),
            bool(plan_item.directed),
            wake_kind="private" if plan_item.private_direct else ("at" if plan_item.directed else "none"),
            bot_id=str(runtime.bot_id or ""),
            visible_node_ids=visible_ids,
            snapshot_at=p.time_service.time(),
        )
        platform_ids = frozenset(
            str(n.msg_id) for n in nodes if (getattr(n, "metadata", None) or {}).get("platform_message_id")
        )
        model_turn = ModelTurn(
            turn,
            catchup_events,
            {
                "rhythm_catchup": True,
                "rhythm_force_quote": bool(plan_item.should_quote),
                "rhythm_skip_micro_delay": True,
            },
            shadow=bool(getattr(p, "shadow_mode", False)),
            platform_message_ids=platform_ids,
            outcome_nodes=tuple(nodes),
            private_direct=bool(plan_item.private_direct),
        )
        before_last_bot = getattr(getattr(runtime, "last_bot_node", None), "msg_id", None)
        try:
            await self.process(runtime, model_turn)
        except asyncio.CancelledError:
            return {"status": "uncertain"}
        except Exception as exc:
            logger.warning("[ChatDynamics] catchup item failed session=%s type=%s", runtime.session_key, type(exc).__name__)
            return {"status": "failed"}
        after_last_bot = getattr(getattr(runtime, "last_bot_node", None), "msg_id", None)
        if after_last_bot and after_last_bot != before_last_bot:
            return {"status": "sent", "message_id": str(after_last_bot)}
        primary_outcome = outcomes.read_outcome(nodes[-1])
        if primary_outcome.get("final_outcome") == outcomes.VALUE_SUPPRESSED:
            return {"status": "skipped", "reason": "rhythm_catchup_skipped_unworthy"}
        return {"status": "failed"}

    async def execute_afterthought(self, runtime, slot, *, quote_self: bool = False) -> dict:
        """Generate and send at most one lightweight post-delivery supplement."""
        from .natural_rhythm import NaturalRhythmController, build_afterthought_prompt

        p = self.plugin
        target_uid = str(getattr(slot, "target_user_id", "") or "").strip()
        slot_id = str(getattr(slot, "trigger_msg_id", "") or getattr(slot, "anchor_bot_msg_id", "") or "slot")
        trace_id = f"afterthought:{slot_id}"
        event = self._synthetic_rhythm_event(
            runtime,
            target_user_id=target_uid,
            trace_turn_id=trace_id,
            trace_bundle_id=trace_id,
        )
        skip_reason = self._prepare_synthetic_boundary(event, target_uid)
        if skip_reason is not None:
            return {"status": "skipped", "reason": f"rhythm_afterthought_skipped_{skip_reason}"}
        persona = await self.bridge.snapshot(event)
        if slot.persona_fingerprint and persona.fingerprint != slot.persona_fingerprint:
            return {"status": "skipped", "reason": "rhythm_afterthought_cancelled_stop"}
        dag = getattr(runtime, "dag", None)
        recent_rows = []
        if dag is not None:
            for n in sorted(dag.get_recent_nodes(limit=12), key=lambda x: x.timestamp):
                if (getattr(n, "metadata", None) or {}).get("recalled") is True:
                    continue
                recent_rows.append(
                    {
                        "message_id": str(n.msg_id),
                        "author": str(n.user_id),
                        "is_bot": bool(runtime.bot_id and str(n.user_id) == str(runtime.bot_id)),
                        "text": str(n.text or "")[:300],
                    }
                )
        prompt = build_afterthought_prompt(
            delivered_text=slot.delivered_text,
            recent_context=recent_rows,
            private_direct=slot.private_direct,
            tier=slot.tier,
            quote_self=quote_self,
        )
        turn = TurnContext(
            runtime.session_key,
            slot.target_user_id or "bot",
            slot.delivered_text[:500],
            (),
            (),
            runtime.epoch,
            runtime.user_revisions.get(slot.target_user_id, 0),
            p.time_service.time(),
            False,
            wake_kind="none",
            bot_id=str(runtime.bot_id or ""),
            snapshot_at=p.time_service.time(),
        )
        lock_timeout = float(
            getattr(p._runtime_config, "afterthought_lock_timeout", 5.0) or 5.0
        )
        gen_timeout = min(
            float(getattr(p._runtime_config, "reply_timeout", 30.0) or 30.0),
            30.0,
        )
        try:
            lock_cm = self.bridge.session_lock(runtime.session_key, acquire_timeout=lock_timeout)
            bounded_entered = False
        except TypeError:
            lock_cm = self.bridge.session_lock(runtime.session_key)
            bounded_entered = True
        entered = False
        try:
            if bounded_entered:
                await asyncio.wait_for(lock_cm.__aenter__(), timeout=lock_timeout)
            else:
                await lock_cm.__aenter__()
            entered = True
        except asyncio.TimeoutError:
            logger.info(
                "[ChatDynamics] afterthought skipped due to session lock timeout session=%s",
                runtime.session_key,
            )
            return {"status": "skipped", "reason": "rhythm_afterthought_lock_timeout"}
        try:
            if runtime.epoch != slot.epoch or getattr(p, "_shutting_down", False):
                return {"status": "skipped", "reason": "rhythm_afterthought_cancelled_stop"}
            if not await self.bridge.current(event, persona):
                return {"status": "skipped", "reason": "rhythm_afterthought_cancelled_stop"}
            provider = await p.llm.resolve_provider_id(runtime.session_key)
            output = None
            try:
                try:
                    output = await asyncio.wait_for(
                        self.generate_ready(
                            runtime,
                            turn,
                            event,
                            (event,),
                            prompt,
                            persona,
                            provider,
                            execution_log=runtime.tool_executions,
                            history_text="",
                            media_understand=False,
                            context_images=(),
                            private_direct=slot.private_direct,
                            budget_purpose="afterthought",
                            allow_early=False,
                        ),
                        timeout=gen_timeout,
                    )
                except TypeError:
                    output = await asyncio.wait_for(
                        self.generate_ready(
                            runtime,
                            turn,
                            event,
                            (event,),
                            prompt,
                            persona,
                            provider,
                            execution_log=runtime.tool_executions,
                            history_text="",
                            media_understand=False,
                            context_images=(),
                            private_direct=slot.private_direct,
                            budget_purpose="afterthought",
                        ),
                        timeout=gen_timeout,
                    )
                completion = getattr(output, "completion_task", None)
                if completion is not None and not completion.done():
                    try:
                        final_output = await asyncio.wait_for(completion, timeout=gen_timeout)
                        if final_output is not None:
                            output = final_output
                    except BaseException:
                        completion.cancel()
                        await asyncio.gather(completion, return_exceptions=True)
                        raise
            except member_boundary.BoundaryCancelled as exc:
                logger.info(
                    "[ChatDynamics] afterthought cancelled by boundary session=%s reason=%s",
                    runtime.session_key,
                    str(exc)[:64],
                )
                return {"status": "skipped", "reason": "rhythm_afterthought_cancelled_boundary"}
            except asyncio.TimeoutError:
                logger.warning(
                    "[ChatDynamics] afterthought generation timed out session=%s",
                    runtime.session_key,
                )
                return {"status": "skipped", "reason": "rhythm_afterthought_timeout"}
            except Exception as exc:
                logger.warning("[ChatDynamics] afterthought generation failed type=%s", type(exc).__name__)
                return {"status": "skipped", "reason": "rhythm_afterthought_skipped_empty"}
            if getattr(output, "reply_mode", "") == "silent" or NaturalRhythmController.is_silent_afterthought(
                str(getattr(output, "text", "") or "")
            ):
                return {"status": "skipped", "reason": "rhythm_afterthought_skipped_empty"}
            text = str(output.text or "").strip()
            quote_id = None
            if quote_self and slot.anchor_bot_msg_id and dag is not None:
                anchor = dag.get_node(slot.anchor_bot_msg_id)
                if anchor is not None and (getattr(anchor, "metadata", None) or {}).get("platform_message_id"):
                    quote_id = slot.anchor_bot_msg_id
            async with runtime.send_lock:
                if runtime.epoch != slot.epoch or getattr(p, "_shutting_down", False):
                    return {"status": "skipped", "reason": "rhythm_afterthought_cancelled_stop"}
                if runtime.is_recalled(slot.anchor_bot_msg_id) or runtime.is_recalled(slot.trigger_msg_id):
                    return {"status": "skipped", "reason": "rhythm_afterthought_cancelled_recall"}
                sent = await p._send_owned(runtime, event, text, reply_to_id=quote_id)
            if not sent.success:
                return {"status": "uncertain" if getattr(sent, "delivery_uncertain", False) else "failed"}
            delivered_text = sent.delivered_text(text)
            await self.bridge.commit(event, output, delivered_text)
            real_id = str(sent.message_id) if sent.message_id else None
            msg_id = real_id or p._next_outgoing_id()
            if dag is not None:
                bot = dag.add_message(
                    msg_id=msg_id,
                    user_id=runtime.bot_id or "bot",
                    text=delivered_text,
                    timestamp=p.time_service.time(),
                    reply_to_id=slot.anchor_bot_msg_id or None,
                    metadata={
                        "platform_message_id": real_id is not None,
                        "dialogue_delivered": True,
                        "reply_mode": "text",
                        "trigger_user_id": slot.target_user_id,
                        "dialogue_state": "casual",
                        "dialogue_action": "reply",
                        "wall_timestamp": p.time_service.wall_time(),
                    },
                )
                runtime.last_bot_node = bot
                p._last_bot_nodes[runtime.session_key] = bot
                runtime.remember_sent(msg_id)
                runtime.last_model_send = p.time_service.time()
                p._observe_routed_bot(runtime, bot)
            p._metric("send_succeeded")
            return {"status": "sent", "message_id": msg_id}
        finally:
            if entered:
                await lock_cm.__aexit__(None, None, None)
