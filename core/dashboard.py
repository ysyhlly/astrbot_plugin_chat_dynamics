"""Read-only snapshots of Chat Dynamics runtime state for the plugin page."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set


def _attr_int(obj: Any, key: str, default: int) -> int:
    val = getattr(obj, key, None)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _truncate(text: str, limit: int = 96) -> str:
    clean = (text or "").replace("\n", " ").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _mask_identifier(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * max(0, len(text) - 4) + text[-4:]


def collect_session_ids(plugin: Any) -> List[str]:
    ids: Set[str] = set()
    ids.update(getattr(plugin, "_sessions", {}).keys())
    ids.update(getattr(plugin, "dags", {}).keys())
    ids.update(getattr(plugin, "_last_bot_nodes", {}).keys())
    ids.update(getattr(plugin, "_vibe_msg_counts", {}).keys())
    tracker = getattr(plugin, "telemetrics", None)
    if tracker is not None and hasattr(tracker, "known_session_ids"):
        ids.update(tracker.known_session_ids())
    arbiter = getattr(plugin, "arbiter", None)
    if arbiter is not None and hasattr(arbiter, "cooling_map"):
        ids.update(arbiter.cooling_map().keys())
    return sorted(ids)


def snapshot_session(plugin: Any, session_id: str, include_nodes: bool = False) -> Dict[str, Any]:
    runtime = getattr(plugin, "_sessions", {}).get(session_id)
    group_id = runtime.group_id if runtime is not None else session_id
    now = plugin.time_service.time()
    vibe = plugin.vibe_analyzer
    telemetrics = vibe.get_telemetrics(session_id, current_time=now)
    mode = vibe.peek_mode(session_id, current_time=now) if hasattr(vibe, "peek_mode") else vibe.get_mode(session_id, current_time=now)
    dag = plugin.dags.get(session_id)
    last_bot = (
        runtime.last_bot_node
        if runtime is not None and runtime.last_bot_node is not None
        else plugin._last_bot_nodes.get(session_id)
    )
    cooling_left = plugin.arbiter.cooling_remaining(session_id, current_time=now)
    has_llm_snapshot = hasattr(vibe, "has_llm_snapshot") and vibe.has_llm_snapshot(session_id)
    last_llm_snapshot = vibe.last_llm_snapshot_time(session_id) if has_llm_snapshot else None
    pending = 0
    if hasattr(plugin.debounce, "get_pending_count"):
        pending = int(plugin.debounce.get_pending_count(session_id=session_id))
    decision = plugin.arbiter.last_decision(session_id) if hasattr(plugin.arbiter, "last_decision") else None

    payload: Dict[str, Any] = {
        "decision_mode": getattr(plugin, "decision_mode", "legacy"),
        "interaction_state": getattr(runtime, "interaction_state", "observing"),
        "model_decision": dict(getattr(runtime, "model_diagnostic", {})),
        "model_queue_depth": len(getattr(runtime, "model_queue", ())),
        "session_key": session_id,
        "session_id": group_id,
        "group_id": group_id,
        "takeover": bool(plugin.is_group_takeover_enabled(group_id)),
        "mode": mode.value if hasattr(mode, "value") else str(mode),
        "mode_source": vibe.mode_source(session_id) if hasattr(vibe, "mode_source") else "telemetrics",
        "vibe_llm_snapshot_count": vibe.llm_snapshot_count(session_id) if hasattr(vibe, "llm_snapshot_count") else 0,
        "vibe_llm_snapshot_age": round(max(0.0, now - last_llm_snapshot), 1) if last_llm_snapshot is not None else None,
        "vibe_llm_in_flight": session_id in getattr(plugin, "_vibe_llm_tasks_by_session", {}),
        "mpm": telemetrics.mpm,
        "token_density": telemetrics.token_density,
        "emoji_ratio": telemetrics.emoji_ratio,
        "unicode_emoji_ratio": getattr(telemetrics, "unicode_emoji_ratio", 0.0),
        "media_ratio": getattr(telemetrics, "media_ratio", 0.0),
        "scene_tags": list(getattr(telemetrics, "scene_tags", ())),
        "emotion_tags": list(getattr(telemetrics, "emotion_tags", ())),
        "unique_speakers": int(getattr(telemetrics, "unique_speakers", 0) or 0),
        "punctuation_formality": telemetrics.punctuation_formality,
        "sample_size": telemetrics.sample_size,
        "dag_nodes": len(dag.nodes) if dag is not None else 0,
        "pending": pending,
        "cooling": cooling_left > 0.0,
        "cooling_remaining": round(cooling_left, 1),
        "turn_count": int(plugin._vibe_msg_counts.get(session_id, 0)),
        "last_bot_text": _truncate(last_bot.text) if last_bot is not None and getattr(plugin, "console_show_message_content", False) else "",
        "last_bot_user_id": (
            last_bot.user_id
            if last_bot is not None and getattr(plugin, "console_show_message_content", False)
            else _mask_identifier(last_bot.user_id if last_bot is not None else "")
        ),
        "content_redacted": not bool(getattr(plugin, "console_show_message_content", False)),
        "last_activity": getattr(runtime, "last_activity", 0.0) if runtime is not None else 0.0,
        "epoch": getattr(runtime, "epoch", 0) if runtime is not None else 0,
        "rate_series": plugin.telemetrics.get_rate_series(session_id, current_time=now, buckets=12),
        "last_arbitration": None if decision is None else {
            "should_speak": decision.should_speak,
            "willingness_score": decision.willingness_score,
            "threshold": decision.threshold,
            "reason": decision.reason,
            "professionalism": decision.professionalism,
            "topic_relevance": decision.topic_relevance,
            "fatigue_penalty": decision.fatigue_penalty,
            "question_value": getattr(decision, "question_value", 0.0),
            "participation": getattr(decision, "participation", 0.0),
            "private_topic": decision.private_topic,
        },
        "occasion": dict(getattr(runtime, "last_occasion", {}) or {}),
        "manners": dict(getattr(runtime, "last_manners", {}) or {}),
    }
    if include_nodes:
        nodes = dag.get_recent_nodes(limit=16) if dag is not None else []
        bot_id = (
            runtime.bot_id
            if runtime is not None and runtime.bot_id
            else plugin.addressivity_router.bot_id
            or (last_bot.user_id if last_bot is not None else "")
        )
        payload["nodes"] = [
            {
                "msg_id": n.msg_id,
                "user_id": n.user_id if getattr(plugin, "console_show_message_content", False) else _mask_identifier(n.user_id),
                "text": _truncate(n.text, 140) if getattr(plugin, "console_show_message_content", False) else "",
                "timestamp": n.timestamp,
                "is_bot": bool(bot_id) and n.user_id == bot_id,
                "reply_to_id": n.reply_to_id,
                "mentions": list(n.mentioned_users) if getattr(plugin, "console_show_message_content", False) else [],
                "parent_ids": sorted(n.parent_ids),
                "child_ids": sorted(n.child_ids),
                "thread_id": getattr(n, "thread_id", "") or n.metadata.get("turn_id"),
                "turn_id": n.metadata.get("turn_id"),
            }
            for n in nodes
        ]
    return payload


def snapshot_sessions(plugin: Any) -> List[Dict[str, Any]]:
    rows = [snapshot_session(plugin, sid, include_nodes=False) for sid in collect_session_ids(plugin)]
    rows.sort(key=lambda row: (not row["takeover"], -row["mpm"], row["group_id"], row["session_key"]))
    return rows


def companion_snapshot(plugin: Any) -> Dict[str, Any]:
    bridge = getattr(plugin, "selflearning", None)
    if not callable(getattr(bridge, "snapshot", None)):
        return {"status": "missing", "lamp": "未安装", "weakened": [], "detail": "n/a", "enabled": False}
    data = bridge.snapshot()
    from .native_request import hooks_available
    native_dispatch = (getattr(plugin, "decision_mode", "legacy") == "persona_model"
                       or getattr(plugin, "pipeline_mode", "filter") != "exclusive"
                       or hooks_available())
    data["native_dispatch"] = native_dispatch
    if not native_dispatch and any(p.get("native_hooks") for p in data.get("providers", [])):
        data.update(status="degraded", lamp="降级中", detail="native_hooks_bypassed")
    return data


def snapshot_overview(plugin: Any) -> Dict[str, Any]:
    if hasattr(plugin, "_sync_runtime_from_config"):
        plugin._sync_runtime_from_config()
    sessions = snapshot_sessions(plugin)
    cooling = sum(1 for row in sessions if row["cooling"])
    live = sum(1 for row in sessions if row["sample_size"] > 0 or row["dag_nodes"] > 0)
    pending = sum(int(row["pending"]) for row in sessions)
    background = getattr(plugin, "_background_tasks", set())
    vibe_tasks = getattr(plugin, "_vibe_llm_tasks", set())
    embed_tasks = getattr(getattr(plugin, "embeddings", None), "_inflight", {})
    active_task_set = {task for task in background if not task.done()}
    active_task_set.update(task for task in vibe_tasks if not task.done())
    active_task_set.update(
        task for task in getattr(embed_tasks, "values", lambda: ())() if not task.done()
    )
    active_task_set.update(
        runtime.generation_task
        for runtime in getattr(plugin, "_sessions", {}).values()
        if getattr(runtime, "generation_task", None) is not None
        and not runtime.generation_task.done()
    )
    active_task_set.update(
        task
        for session_tasks in getattr(plugin, "_hook_tasks_by_session", {}).values()
        for task in session_tasks
        if not task.done()
    )
    active_tasks = len(active_task_set)
    cooling_task = getattr(plugin, "_cooling_persist_task", None)
    if cooling_task is not None and not cooling_task.done():
        active_task_set.add(cooling_task)
    sweep_task = getattr(plugin, "_session_sweep_task", None)
    if sweep_task is not None and not sweep_task.done():
        active_task_set.add(sweep_task)
    active_tasks = len(active_task_set)
    metrics = dict(getattr(plugin, "_metrics", {}))
    metrics["active_tasks"] = active_tasks
    metrics.update(
        {
            "received": metrics.get("message_received", 0),
            "takeover": metrics.get("takeover_considered", 0),
            "silent": metrics.get("speech_withheld", 0),
            "native_allowed": metrics.get("native_pass", 0),
            "shadow": metrics.get("shadow_decision", 0),
            "llm_success": metrics.get("llm_reply_succeeded", 0)
            + metrics.get("llm_vibe_succeeded", 0),
            "llm_failure": metrics.get("llm_reply_failed", 0)
            + metrics.get("llm_vibe_failed", 0)
            + metrics.get("llm_reply_unavailable", 0)
            + metrics.get("llm_vibe_unavailable", 0),
            "send_success": metrics.get("send_succeeded", 0),
            "send_failure": metrics.get("send_failed", 0),
            "truncated": metrics.get("input_truncated", 0) + metrics.get("turn_truncated", 0),
            "evicted": metrics.get("session_evicted", 0),
            "cool_reset": metrics.get("cooling_triggered", 0) + metrics.get("reset_triggered", 0),
            "persistence_failed": metrics.get("cooling_persist_failed", 0),
        }
    )
    return {
        "enabled": bool(plugin.enabled),
        "pipeline_mode": str(getattr(plugin, "pipeline_mode", "filter") or "filter"),
        "decision_mode": getattr(plugin, "decision_mode", "legacy"),
        "agent_bridge": getattr(getattr(getattr(plugin, "persona_engine", None), "bridge", None), "diagnostic", "not_checked"),
        "inactive_options": (["ambient_intervention", "vibe_llm_enabled", "vibe_provider", "WTS weights", "casual_emoji_enabled"]
                             if getattr(plugin, "decision_mode", "legacy") == "persona_model" else []),
        "ambient_intervention": bool(getattr(plugin, "ambient_intervention", False)),
        "takeover_all": bool(plugin.takeover_all),
        "takeover_groups": sorted(plugin.takeover_groups),
        "exclude_groups": sorted(plugin.exclude_groups),
        "bot_names": list(plugin.bot_names),
        "provider": plugin.provider_id,
        "reply_provider": str(getattr(plugin, "reply_provider_id", "") or ""),
        "vibe_provider": str(getattr(plugin, "vibe_provider_id", "") or ""),
        "provider_compatibility": {
            "legacy_configured": bool(getattr(plugin, "provider_id", "")),
            "reply_configured": bool(getattr(plugin, "reply_provider_id", "")),
            "vibe_configured": bool(getattr(plugin, "vibe_provider_id", "")),
        },
        "provider_resolution": {
            "reply": (
                plugin.llm.configured_provider("reply")
                if hasattr(getattr(plugin, "llm", None), "configured_provider")
                else str(getattr(plugin, "reply_provider_id", "") or getattr(plugin, "provider_id", "") or "")
            )
            or "current_umo",
            "vibe": (
                plugin.llm.configured_provider("vibe")
                if hasattr(getattr(plugin, "llm", None), "configured_provider")
                else str(getattr(plugin, "vibe_provider_id", "") or getattr(plugin, "provider_id", "") or "")
            )
            or "current_umo",
            "decision": (
                str(getattr(getattr(plugin, "_runtime_config", None), "decision_provider_id", "") or "")
                or (
                    plugin.llm.configured_provider("reply")
                    if hasattr(getattr(plugin, "llm", None), "configured_provider")
                    else str(getattr(plugin, "reply_provider_id", "") or getattr(plugin, "provider_id", "") or "")
                )
                or "current_umo"
            ),
        },
        "vibe_llm_enabled": bool(getattr(plugin, "vibe_llm_enabled", True)),
        "shadow_mode": bool(getattr(plugin, "shadow_mode", False)),
        "console_show_message_content": bool(getattr(plugin, "console_show_message_content", False)),
        "neural_embedding_enabled": bool(getattr(getattr(plugin, "embeddings", None), "enabled", False)),
        "embedding_provider": str(getattr(getattr(plugin, "embeddings", None), "provider_id", "") or ""),
        "embedding_backend": str(getattr(getattr(plugin, "embeddings", None), "last_backend", "hashed") or "hashed"),
        "embedding_cache_size": int(getattr(getattr(plugin, "embeddings", None), "cache_len", 0) or 0),
        "session_count": len(sessions),
        "live_count": live,
        "cooling_count": cooling,
        "pending_count": pending,
        "active_tasks": active_tasks,
        "metrics": metrics,
        "shadow_decisions": list(getattr(plugin, "_shadow_decisions", ())),
        "config_warnings": sorted(getattr(plugin, "_config_warnings_seen", ())),
        "sessions": sessions,
        "presence_knob": str(getattr(plugin, "presence_knob", "sensible") or "sensible"),
        "social_manners": {
            "enabled": bool(getattr(plugin, "social_manners_enabled", True)),
            "relay_baton": bool(getattr(plugin, "relay_baton_enabled", True)),
            "private_field": bool(getattr(plugin, "private_field_enabled", True)),
            "hyped_quota": bool(getattr(plugin, "hyped_quota_enabled", True)),
        },
        "media_gate": {
            "image": bool(getattr(plugin, "media_image_gate_enabled", True)),
            "voice": bool(getattr(plugin, "media_voice_gate_enabled", True)),
            "understand_reply": bool(getattr(plugin, "media_understand_reply_enabled", False)),
            "privacy_strict": bool(getattr(plugin, "media_privacy_strict", True)),
            "multimodal": _multimodal_lamp(plugin),
        },
        "selflearning": companion_snapshot(plugin),
        "read_air": _read_air_summary(plugin, sessions),
        "features": {
            "mood_memory": bool(getattr(plugin, "mood_memory_enabled", False)),
            "slang_trial": bool(getattr(plugin, "slang_trial_enabled", False)),
            "group_memory": bool(getattr(plugin, "group_memory_enabled", True)),
            "gap_fill_proactive": bool(getattr(plugin, "gap_fill_proactive_enabled", True)),
            "cold_memory_nudge": bool(getattr(plugin, "cold_memory_nudge_enabled", True)),
            "newcomer_caution": bool(getattr(plugin, "newcomer_caution_enabled", True)),
            "deciding_detect": bool(getattr(plugin, "deciding_detect_enabled", True)),
            "pace_align": bool(getattr(plugin, "pace_align_enabled", True)),
            "proactive_quota": bool(getattr(plugin, "proactive_quota_enabled", True)),
            "daily_rhythm": bool(getattr(plugin, "daily_rhythm_enabled", True)),
        },
        "useful_proactive": {
            "gap_fill": bool(getattr(plugin, "gap_fill_proactive_enabled", True)),
            "cold_nudge": bool(getattr(plugin, "cold_memory_nudge_enabled", True)),
            "newcomer": bool(getattr(plugin, "newcomer_caution_enabled", True)),
            "pace_align": bool(getattr(plugin, "pace_align_enabled", True)),
            "quota_enabled": bool(getattr(plugin, "proactive_quota_enabled", True)),
            "quota_per_hour": _attr_int(plugin, "proactive_quota_per_hour", 2),
            "quota_per_topic": _attr_int(plugin, "proactive_quota_per_topic", 1),
        },
        "daily_rhythm": {
            "enabled": bool(getattr(plugin, "daily_rhythm_enabled", True)),
            "morning_hi": bool(getattr(plugin, "rhythm_morning_hi_enabled", True)),
            "day_share_slots": _attr_int(plugin, "rhythm_day_share_slots", 1),
            "goodnight_quota": _attr_int(plugin, "rhythm_goodnight_text_quota", 1),
            "sleep_after_winddown": bool(getattr(plugin, "rhythm_sleep_after_winddown", True)),
            "allow_self_sleep": bool(getattr(plugin, "rhythm_allow_self_sleep", True)),
            "allow_wake": bool(getattr(plugin, "rhythm_allow_wake", True)),
            "insomnia": bool(getattr(plugin, "rhythm_insomnia_enabled", False)),
            "force_sleep": bool(getattr(plugin, "rhythm_force_sleep", False)),
            "skip_morning_hi_tonight": bool(getattr(plugin, "rhythm_skip_morning_hi_tonight", False)),
        },
    }




def _multimodal_lamp(plugin: Any) -> Dict[str, Any]:
    """Partner-style note: whether host multimodal is usable for L2."""
    gate = getattr(plugin, "decision_gate", None)
    media = getattr(gate, "media", None) if gate is not None else None
    available = None
    if media is not None and hasattr(media, "multimodal_available"):
        try:
            available = media.multimodal_available()
        except Exception:
            available = None
    if available is True:
        lamp, detail = "可用", "宿主可走多模态理解"
    elif available is False:
        lamp, detail = "不支持", "无视觉/听写时仅用门闩启发式，L2 安静降级"
    else:
        lamp, detail = "启发式", "默认启发式门闩；未探测到多模态能力"
    return {"lamp": lamp, "detail": detail, "available": available}


def _read_air_summary(
    plugin: Any,
    sessions: List[Dict[str, Any]],
    *,
    session_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Today's read-air: occasion, intervene vs quiet, recent why-silent (no raw msgs)."""
    gate = getattr(plugin, "decision_gate", None)
    manners = getattr(gate, "manners", None) if gate is not None else None
    stats = manners.today_stats() if manners is not None and hasattr(manners, "today_stats") else {
        "intervene": 0,
        "quiet": int((getattr(plugin, "_metrics", {}) or {}).get("speech_withheld", 0) or 0),
        "why_silent": [],
    }
    selected = (session_key or "").strip()
    scoped = sessions
    if selected:
        scoped = [
            row
            for row in sessions
            if selected in {
                str(row.get("session_key") or ""),
                str(row.get("session_id") or ""),
                str(row.get("group_id") or ""),
                str(row.get("umo") or ""),
            }
        ] or sessions
    occasion = {"kind": "neutral", "reason_zh": "暂无活跃群", "confidence": 0.35}
    live_rows = [row for row in scoped if row.get("sample_size", 0) or row.get("dag_nodes", 0)]
    top = None
    if live_rows:
        top = max(live_rows, key=lambda r: float(r.get("mpm") or 0))
        occ = top.get("occasion") or {}
        if occ:
            occasion = {
                "kind": occ.get("kind") or top.get("mode") or "neutral",
                "reason_zh": occ.get("reason_zh") or f"当前模式 {top.get('mode')}",
                "silence_bias": occ.get("silence_bias"),
                "length_hint": occ.get("length_hint"),
            }
        else:
            occasion = {"kind": top.get("mode") or "neutral", "reason_zh": f"当前模式 {top.get('mode')}"}
    # Confidence: higher when we have live traffic + a concrete occasion reason.
    confidence = 0.35
    if live_rows:
        confidence = 0.55
        bias = occasion.get("silence_bias")
        if bias is not None:
            try:
                confidence = max(0.2, min(0.95, 1.0 - abs(float(bias) - 0.35)))
            except (TypeError, ValueError):
                confidence = 0.6
        if occasion.get("reason_zh") and "暂无" not in str(occasion.get("reason_zh")):
            confidence = max(confidence, 0.62)
        if float((top or {}).get("mpm") or 0) >= 1.0:
            confidence = min(0.95, confidence + 0.12)
    occasion["confidence"] = round(float(confidence), 2)
    why = []
    for item in stats.get("why_silent") or []:
        why.append({
            "reason_zh": item.get("reason_zh") or item.get("reason_code") or "安静旁听",
            "reason_code": item.get("reason_code") or "",
            "ts": item.get("ts"),
        })
    media_rows = []
    for item in why:
        code = str(item.get("reason_code") or "")
        if code.startswith("media_") or code.startswith("voice_") or code.startswith("image_"):
            media_rows.append(item)
    intervene = int(stats.get("intervene") or 0)
    quiet = int(stats.get("quiet") or 0)
    total = max(1, intervene + quiet)
    presence_knob = str(getattr(plugin, "presence_knob", "sensible") or "sensible")
    hour_cap = _attr_int(plugin, "proactive_quota_per_hour", 2)
    proactive_used, proactive_cap = 0, hour_cap
    useful = getattr(gate, "useful", None) if gate is not None else None
    if useful is not None and hasattr(useful, "quota_status"):
        try:
            qs = useful.quota_status(selected or "", now=None, hour_cap=hour_cap)
            raw_used = qs.get("proactive_used")
            proactive_used = int(raw_used) if raw_used is not None else 0
            raw_cap = qs.get("proactive_cap")
            proactive_cap = int(raw_cap) if raw_cap is not None else hour_cap
        except Exception:
            pass
    rhythm_gate = getattr(gate, "rhythm", None) if gate is not None else None
    rhythm_status = {
        "state": "awake",
        "state_zh": "还醒着",
        "enabled": bool(getattr(plugin, "daily_rhythm_enabled", True)),
    }
    if rhythm_gate is not None and hasattr(rhythm_gate, "status"):
        try:
            rhythm_status = dict(rhythm_gate.status(selected or "", now=None) or {})
            rhythm_status["enabled"] = bool(getattr(plugin, "daily_rhythm_enabled", True))
        except Exception:
            pass
    if rhythm_gate is not None and hasattr(rhythm_gate, "why_silent_rows"):
        try:
            for item in rhythm_gate.why_silent_rows(selected or "", limit=8):
                why.append({
                    "reason_zh": item.get("reason_zh") or item.get("reason_code") or "作息安静",
                    "reason_code": item.get("reason_code") or "",
                    "ts": item.get("ts"),
                })
        except Exception:
            pass
    # de-dupe why by code+ts keeping order
    seen_why = set()
    deduped = []
    for item in why:
        key = (item.get("reason_code"), item.get("ts"), item.get("reason_zh"))
        if key in seen_why:
            continue
        seen_why.add(key)
        deduped.append(item)
    why = deduped
    return {
        "occasion": occasion,
        "confidence": occasion["confidence"],
        "one_liner": occasion.get("reason_zh") or "暂无读空气摘要",
        "intervene_count": intervene,
        "quiet_count": quiet,
        "why_silent": why[:8],
        "decisions": why[:5],
        "media_why_silent": media_rows[:5],
        "presence_knob": presence_knob,
        "proactive_used": proactive_used,
        "proactive_cap": proactive_cap,
        "thermometer": {
            "quiet_ratio": round(quiet / total, 3),
            "intervene_ratio": round(intervene / total, 3),
            "quiet_count": quiet,
            "intervene_count": intervene,
            "target": presence_knob,
            "proactive_used": proactive_used,
            "proactive_cap": proactive_cap,
        },
        "session_key": selected or ((top or {}).get("session_key") if top else ""),
        "empty": not bool(live_rows),
        "daily_rhythm": rhythm_status,
        "rhythm_state": rhythm_status.get("state"),
        "rhythm_state_zh": rhythm_status.get("state_zh"),
        "rhythm_summary": (
            f"{rhythm_status.get('state_zh') or '还醒着'}"
            + (f" · {rhythm_status['last_reason_zh']}" if rhythm_status.get("last_reason_zh") else "")
        ),
    }


def snapshot_session_or_none(plugin: Any, session_id: str) -> Optional[Dict[str, Any]]:
    sid = (session_id or "").strip()
    if not sid:
        return None
    resolver = getattr(plugin, "_resolve_session_key", None)
    resolved = resolver(sid) if callable(resolver) else sid
    known = set(collect_session_ids(plugin))
    if resolved is None or resolved not in known:
        return None
    return snapshot_session(
        plugin,
        resolved,
        include_nodes=bool(getattr(plugin, "console_show_message_content", False)),
    )


_REPLAY_LANES = (
    ("media", ("media_", "voice_", "image_")),
    ("rhythm", ("wind_", "asleep_", "morning", "insomnia", "rhythm_", "goodnight", "wake_")),
    ("proactive", ("gap_", "cold_nudge", "newcomer", "proactive_", "quota")),
    ("arbiter", ("arbiter", "wts", "deep_cooling", "safe_hover", "energy_", "private_topic", "filter_gate")),
    ("manners", ("relay_", "private_field", "hyped_", "presence_ghost", "conflict_")),
)


def replay_lane(reason_code: str, action: str = "silent") -> str:
    if action == "speak":
        return "speak"
    code = str(reason_code or "").lower()
    for lane, prefixes in _REPLAY_LANES:
        if any(code.startswith(prefix) or prefix in code for prefix in prefixes):
            return lane
    return "manners"


def merge_replay_blocks(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for item in events:
        action = str(item.get("action") or "silent")
        lane = str(item.get("lane") or replay_lane(str(item.get("reason_code") or ""), action))
        ts = float(item.get("ts") or 0.0)
        if blocks and blocks[-1]["action"] == action and blocks[-1]["lane"] == lane:
            block = blocks[-1]
            block["end_ts"] = max(block["end_ts"], ts)
            block["count"] = int(block["count"]) + 1
            block["reason_code"] = str(item.get("reason_code") or block["reason_code"])
            block["reason_zh"] = str(item.get("reason_zh") or block["reason_zh"])
            continue
        if blocks:
            prev = blocks[-1]
            if ts > prev["end_ts"]:
                prev["end_ts"] = ts
        blocks.append(
            {
                "start_ts": ts,
                "end_ts": ts,
                "action": action,
                "lane": lane,
                "reason_code": str(item.get("reason_code") or ""),
                "reason_zh": str(item.get("reason_zh") or ""),
                "count": 1,
            }
        )
    if blocks:
        last = blocks[-1]
        last["end_ts"] = max(float(last["end_ts"]), float(last["start_ts"]) + 20.0)
    return blocks


def scene_replay_snapshot(plugin: Any, *, session_key: str = "") -> Dict[str, Any]:
    """Color-block timeline of why the bot spoke or stayed quiet. No plaintext."""
    selected = (session_key or "").strip()
    gate = getattr(plugin, "decision_gate", None)
    manners = getattr(gate, "manners", None) if gate is not None else None
    events: List[Dict[str, Any]] = []
    if manners is not None and hasattr(manners, "scene_track"):
        events.extend(manners.scene_track(selected, limit=80))
    rhythm_gate = getattr(gate, "rhythm", None) if gate is not None else None
    if rhythm_gate is not None and hasattr(rhythm_gate, "why_silent_rows"):
        try:
            for item in rhythm_gate.why_silent_rows(selected, limit=24):
                events.append(
                    {
                        "ts": item.get("ts"),
                        "reason_code": item.get("reason_code") or "",
                        "reason_zh": item.get("reason_zh") or "作息安静",
                        "action": "silent",
                        "session_id": selected or item.get("session_id") or "",
                    }
                )
        except Exception:
            pass
    seen: Set[tuple] = set()
    ordered: List[Dict[str, Any]] = []
    for item in sorted(events, key=lambda row: float(row.get("ts") or 0)):
        action = str(item.get("action") or "silent")
        code = str(item.get("reason_code") or "")
        zh = str(item.get("reason_zh") or "")
        ts = item.get("ts")
        key = (action, code, zh, ts)
        if key in seen:
            continue
        seen.add(key)
        lane = replay_lane(code, action)
        ordered.append(
            {
                "ts": ts,
                "action": action,
                "lane": lane,
                "reason_code": code,
                "reason_zh": zh,
            }
        )
    sessions = []
    try:
        for row in snapshot_sessions(plugin):
            sessions.append(
                {
                    "session_key": row.get("session_key") or row.get("session_id"),
                    "session_id": row.get("session_id"),
                    "group_id": row.get("group_id"),
                }
            )
    except Exception:
        sessions = []
    speak = sum(1 for item in ordered if item["action"] == "speak")
    silent = sum(1 for item in ordered if item["action"] != "speak")
    return {
        "session_key": selected,
        "presence_knob": str(getattr(plugin, "presence_knob", "sensible") or "sensible"),
        "events": ordered[-64:],
        "blocks": merge_replay_blocks(ordered[-64:]),
        "speak_count": speak,
        "silent_count": silent,
        "sessions": sessions,
        "empty": not bool(ordered),
        "content_redacted": True,
    }
