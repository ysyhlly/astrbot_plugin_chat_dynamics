"""Read-only Dry-Run Decision Simulator for WebUI Replay/Config sandboxes.

Evaluates a historical DAG message or synthetic message under both the active
live configuration (baseline) and unsaved draft overrides (draft) without
mutating runtime state, writing to storage, or sending any platform messages.
"""

from __future__ import annotations

import math
import time
from typing import Any

from .group_profiles import VALID_PRESENCE_KNOBS, resolve_effective_group_profile
from .jev_decision import decision_from_answers
from .turn_decision import MessageSnapshot, TurnContext

_PRESENCE_BONUS: dict[str, float] = {
    "ghost": -0.10,
    "sensible": 0.0,
    "lively": 0.10,
}

_PRESENCE_LABELS: dict[str, str] = {
    "ghost": "隐身",
    "sensible": "懂事",
    "lively": "活跃",
}

_DIRECT_CUES = ("?", "？", "帮我", "请问", "怎么", "为什么", "能不能", "在吗", "看看", "解释", "推荐")


def _extract_historical_answers(node: Any) -> dict[str, Any] | None:
    """Reconstruct Mapping answers matching jev_decision contract from stored decision_trace."""
    meta = getattr(node, "metadata", None)
    if not isinstance(meta, dict):
        return None
    trace = meta.get("decision_trace")
    if not isinstance(trace, dict):
        return None
    judgement = (trace.get("decision_stages") or {}).get("judgement")
    if not isinstance(judgement, dict):
        return None

    out: dict[str, Any] = {}
    for key in ("completion", "recipient", "action", "state", "length", "reason"):
        raw = judgement.get(key)
        if not isinstance(raw, dict):
            continue
        choice = raw.get("choice")
        if not isinstance(choice, str) or not choice:
            continue
        conf = raw.get("confidence", raw.get("probability", 0.7))
        try:
            conf_f = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            continue
        out[key] = {"type": "choice", "choice": choice, "confidence": conf_f}

    join_raw = judgement.get("join")
    if isinstance(join_raw, dict):
        val = join_raw.get("noul", join_raw.get("probability"))
        if val is not None:
            try:
                jp = max(0.0, min(1.0, float(val)))
                out["join"] = {"type": "noul", "noul": jp}
            except (TypeError, ValueError):
                pass
    elif isinstance(judgement.get("join_probability"), (int, float)):
        jp = max(0.0, min(1.0, float(judgement["join_probability"])))
        out["join"] = {"type": "noul", "noul": jp}

    return out if out else None


def _synthesize_answers(text: str, wake_kind: str, *, presence_knob: str) -> dict[str, Any]:
    """Build deterministic, bounded heuristic answers for synthetic dry-run messages."""
    clean = str(text or "").strip()
    is_direct = wake_kind in {"at", "quote", "name", "private"}
    has_cue = any(cue in clean for cue in _DIRECT_CUES)
    length_score = min(0.18, len(clean) / 120.0)
    presence_shift = _PRESENCE_BONUS.get(presence_knob, 0.0)

    if is_direct:
        join_prob = min(0.98, max(0.75, 0.88 + presence_shift))
        recipient_choice = "bot"
        recipient_conf = 0.92 if wake_kind in {"at", "private"} else 0.84
        action_choice = "reply"
        action_conf = 0.86
        reason_choice = "addressed_question" if has_cue else "addressed_request"
    else:
        base_join = 0.52 + (0.16 if has_cue else 0.0) + length_score + presence_shift
        join_prob = max(0.05, min(0.95, round(base_join, 3)))
        recipient_choice = "group"
        recipient_conf = 0.78
        action_choice = "reply" if join_prob >= 0.5 else "ignore"
        action_conf = max(0.60, min(0.92, join_prob if action_choice == "reply" else 1.0 - join_prob))
        reason_choice = "open_group_topic" if action_choice == "reply" else "low_value_chatter"

    return {
        "completion": {"type": "choice", "choice": "complete", "confidence": 0.92},
        "recipient": {"type": "choice", "choice": recipient_choice, "confidence": recipient_conf},
        "join": {"type": "noul", "noul": join_prob},
        "action": {"type": "choice", "choice": action_choice, "confidence": action_conf},
        "state": {"type": "choice", "choice": "casual", "confidence": 0.8},
        "length": {"type": "choice", "choice": "brief", "confidence": 0.78},
        "reason": {"type": "choice", "choice": reason_choice, "confidence": 0.8},
    }


def _evaluate_once(
    *,
    session_key: str,
    text: str,
    author: str,
    author_name: str,
    msg_id: str,
    wake_kind: str,
    stored_answers: dict[str, Any] | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    presence = str(params.get("presence_knob") or "sensible").lower()
    if presence not in VALID_PRESENCE_KNOBS:
        presence = "sensible"
    threshold = float(params.get("reply_probability_threshold", 70.0))
    threshold = max(0.0, min(100.0, threshold))
    backend = str(params.get("decision_backend") or "jev").lower()
    if backend not in {"jev", "llm"}:
        backend = "jev"
    min_conf = float(params.get("jev_min_confidence", 0.55))
    min_conf = max(0.1, min(0.95, min_conf))
    shadow = bool(params.get("shadow_mode", False))
    quote_thr = float(params.get("quote_reply_threshold", 35.0))
    name_thr = float(params.get("name_reply_threshold", 70.0))

    explicit = wake_kind in {"at", "private"}
    turn = TurnContext(
        session_key=session_key or "dry_run:session",
        author=author or "dry_user",
        text=text,
        messages=(MessageSnapshot(message_id=msg_id, author=author or "dry_user", text=text, author_name=author_name),),
        background=(),
        epoch=1,
        revision=1,
        started_at=time.time(),
        explicit=explicit,
        wake_kind=wake_kind,
        bot_id="bot",
    )

    answers_map = stored_answers if stored_answers is not None else _synthesize_answers(text, wake_kind, presence_knob=presence)
    # Apply presence shift to join probability when replaying stored answers so knob differences are visible.
    if stored_answers is not None and isinstance(stored_answers.get("join"), dict):
        base_jp = float(stored_answers["join"].get("noul", 0.5))
        shifted = max(0.0, min(1.0, base_jp + _PRESENCE_BONUS.get(presence, 0.0)))
        answers_map = {**stored_answers, "join": {"type": "noul", "noul": shifted}}

    prefix = "llm_" if backend == "llm" else "jev_"
    if wake_kind == "at":
        action = "reply"
        reason_code = "at_mandatory"
        should_speak = True
        join_prob = 1.0
    else:
        decision = decision_from_answers(
            turn,
            answers_map,
            min_confidence=min_conf,
            join_floor=threshold / 100.0,
            quote_recipient_floor=quote_thr / 100.0,
            name_recipient_floor=name_thr / 100.0,
            prefix=prefix,
        )
        action = decision.action
        reason_code = decision.reason_code
        should_speak = action in {"reply", "acknowledge", "clarify"}
        join_obj = answers_map.get("join")
        join_prob = round(float(join_obj.get("noul", 0.0)), 4) if isinstance(join_obj, dict) else None

    effective_delivery = "shadow_only" if (should_speak and shadow) else ("speak" if should_speak else "silent")
    return {
        "action": action,
        "should_speak": should_speak,
        "effective_delivery": effective_delivery,
        "reason_code": reason_code,
        "join_probability": join_prob,
        "join_threshold": round(threshold / 100.0, 4),
        "decision_backend": backend,
        "presence_knob": presence,
        "presence_label": _PRESENCE_LABELS.get(presence, presence),
        "shadow_mode": shadow,
        "source": "historical_trace" if stored_answers is not None else "synthetic_model",
    }


def simulate_decision_dry_run(plugin: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Compare live baseline config vs unsaved draft overrides on a historical or simulated message."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    session_key = str(payload.get("session_key") or payload.get("umo") or "").strip()[:160]
    msg_id = str(payload.get("msg_id") or "").strip()[:128]
    raw_text = str(payload.get("text") or "").strip()[:600]
    author_name = str(payload.get("author_name") or "群友").strip()[:48]
    wake_kind = str(payload.get("wake_kind") or "").strip().lower()
    if wake_kind not in {"", "at", "quote", "name", "private", "ambient"}:
        raise ValueError("wake_kind must be one of '', 'ambient', 'at', 'quote', 'name', 'private'")
    if wake_kind == "ambient":
        wake_kind = ""

    stored_answers: dict[str, Any] | None = None
    author = "dry_user"
    matched_msg_id = msg_id or "dry_run_msg"
    resolved_session = ""
    if session_key:
        resolver = getattr(plugin, "_resolve_session_key", None)
        resolved_session = (resolver(session_key) if callable(resolver) else None) or session_key

    if resolved_session and msg_id:
        dag = getattr(plugin, "dags", {}).get(resolved_session)
        node = dag.get_node(msg_id) if dag is not None else None
        if node is not None:
            if not raw_text:
                raw_text = str(getattr(node, "text", "") or "")[:600]
            author = str(getattr(node, "user_id", "") or "dry_user")
            stored_answers = _extract_historical_answers(node)
            if not wake_kind:
                trace = (node.metadata or {}).get("decision_trace") or {}
                wk = str((trace.get("decision_stages") or {}).get("persona", {}).get("wake_kind") or "")
                if wk in {"at", "quote", "name", "private"}:
                    wake_kind = wk

    if not raw_text and stored_answers is None:
        raise ValueError("请提供模拟消息内容 text，或指定已存在的 session_key + msg_id")

    cfg = getattr(plugin, "_runtime_config", None)
    profile_info = resolve_effective_group_profile(plugin, resolved_session or session_key)
    eff_profile = profile_info["effective"]

    baseline_params: dict[str, Any] = {
        "presence_knob": eff_profile["presence_knob"],
        "reply_probability_threshold": float(eff_profile["reply_probability_threshold"]),
        "shadow_mode": bool(eff_profile["shadow_mode"]),
        "decision_backend": str(getattr(cfg, "decision_backend", "jev") or "jev"),
        "jev_min_confidence": float(getattr(cfg, "jev_min_confidence", 0.55)),
        "quote_reply_threshold": float(getattr(cfg, "quote_reply_threshold", 35.0)),
        "name_reply_threshold": float(getattr(cfg, "name_reply_threshold", 70.0)),
    }

    overrides = payload.get("draft_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("draft_overrides must be an object")
    draft_params = dict(baseline_params)
    for key in (
        "presence_knob",
        "reply_probability_threshold",
        "shadow_mode",
        "decision_backend",
        "jev_min_confidence",
        "quote_reply_threshold",
        "name_reply_threshold",
    ):
        if key in overrides and overrides[key] is not None and overrides[key] != "":
            val = overrides[key]
            if key in {"reply_probability_threshold", "quote_reply_threshold", "name_reply_threshold", "jev_min_confidence"}:
                if isinstance(val, bool):
                    raise ValueError(f"{key} must be numeric")
                num = float(val)
                if not math.isfinite(num):
                    raise ValueError(f"{key} must be finite")
                draft_params[key] = num
            elif key == "shadow_mode":
                draft_params[key] = bool(val)
            else:
                draft_params[key] = str(val).strip().lower()

    baseline = _evaluate_once(
        session_key=resolved_session or session_key,
        text=raw_text or "（历史样本）",
        author=author,
        author_name=author_name,
        msg_id=matched_msg_id,
        wake_kind=wake_kind,
        stored_answers=stored_answers,
        params=baseline_params,
    )
    draft = _evaluate_once(
        session_key=resolved_session or session_key,
        text=raw_text or "（历史样本）",
        author=author,
        author_name=author_name,
        msg_id=matched_msg_id,
        wake_kind=wake_kind,
        stored_answers=stored_answers,
        params=draft_params,
    )

    param_diffs = [
        {"key": k, "baseline": baseline_params[k], "draft": draft_params[k]}
        for k in baseline_params
        if baseline_params[k] != draft_params[k]
    ]
    changed = (
        baseline["action"] != draft["action"]
        or baseline["effective_delivery"] != draft["effective_delivery"]
        or baseline["reason_code"] != draft["reason_code"]
    )
    return {
        "ok": True,
        "dry_run": True,
        "session_key": resolved_session or session_key,
        "msg_id": matched_msg_id if msg_id else "",
        "wake_kind": wake_kind or "ambient",
        "baseline": baseline,
        "draft": draft,
        "outcome_changed": changed,
        "param_diffs": param_diffs,
    }
