"""Validated runtime configuration for Chat Dynamics."""

from __future__ import annotations

import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable, List, Tuple


PIPELINE_FILTER = "filter"
PIPELINE_EXCLUSIVE = "exclusive"
_PIPELINE_MODES = {PIPELINE_FILTER, PIPELINE_EXCLUSIVE}


@dataclass(frozen=True)
class RuntimeConfig:
    enabled: bool
    pipeline_mode: str
    ambient_intervention: bool
    takeover_all: bool
    takeover_groups: frozenset[str]
    exclude_groups: frozenset[str]
    bot_names: tuple[str, ...]
    provider_id: str
    reply_provider_id: str
    vibe_provider_id: str
    command_prefix: str
    debounce_base_cooldown: float
    debounce_extended_cooldown: float
    debounce_max_cap: float
    strong_addressivity_threshold: float
    safe_hover_threshold: float
    deep_cooling_minutes: float
    chars_per_second: float
    base_thinking_delay: float
    max_fragments: int
    max_fragment_chars: int
    inter_burst_interval: float
    casual_emoji_enabled: bool
    strip_markdown_in_banter: bool
    vibe_llm_enabled: bool
    shadow_mode: bool
    console_show_message_content: bool
    telemetrics_window_seconds: float
    fast_banter_enter_mpm: float
    chill_fade_enter_mpm: float
    wts_topic_weight: float
    wts_professionalism_weight: float
    wts_question_weight: float
    wts_participation_weight: float
    wts_fatigue_weight: float
    neural_embedding_enabled: bool
    embedding_provider: str
    neural_link_threshold: float
    embedding_cache_size: int
    conversation_router_enabled: bool = True
    topic_reranker_enabled: bool = True
    topic_reranker_provider: str = ""
    topic_reranker_timeout: float = 3.0
    routing_neural_timeout: float = 0.5
    topic_window_seconds: float = 300.0
    replay_message_limit: int = 500
    topic_join_threshold: float = 0.48
    topic_commit_threshold: float = 0.0
    topic_ambiguity_threshold: float = 0.0
    topic_margin_threshold: float = 0.06
    parent_window_seconds: float = 180.0
    parent_accept_threshold: float = 0.72
    decision_mode: str = "legacy"
    decision_provider_id: str = ""
    decision_timeout: float = 8.0
    reply_timeout: float = 60.0
    tool_agent_timeout: float = 120.0
    presence_knob: str = "sensible"
    social_manners_enabled: bool = True
    relay_baton_enabled: bool = True
    private_field_enabled: bool = True
    hyped_quota_enabled: bool = True
    mood_memory_enabled: bool = False
    slang_trial_enabled: bool = False
    group_memory_enabled: bool = True
    selflearning_integration: bool = True
    media_image_gate_enabled: bool = True
    media_voice_gate_enabled: bool = True
    media_understand_reply_enabled: bool = False
    media_privacy_strict: bool = True
    deciding_detect_enabled: bool = True
    gap_fill_proactive_enabled: bool = True
    cold_memory_nudge_enabled: bool = True
    newcomer_caution_enabled: bool = True
    pace_align_enabled: bool = True
    proactive_quota_enabled: bool = True
    proactive_quota_per_hour: int = 2
    proactive_quota_per_topic: int = 1
    daily_rhythm_enabled: bool = True
    rhythm_timezone: str = ""
    rhythm_morning_hi_enabled: bool = True
    rhythm_day_share_slots: int = 1
    rhythm_goodnight_text_quota: int = 1
    rhythm_sleep_after_winddown: bool = True
    rhythm_allow_self_sleep: bool = True
    rhythm_allow_wake: bool = True
    rhythm_insomnia_enabled: bool = False
    rhythm_force_sleep: bool = False
    rhythm_skip_morning_hi_tonight: bool = False


def _get(raw: Any, key: str, default: Any) -> Any:
    try:
        value = raw.get(key)
    except Exception:
        return default
    return default if value is None else value


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _strings(value: Any) -> tuple[str, ...]:
    source: Iterable[Any]
    if isinstance(value, str):
        source = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        source = value
    else:
        source = ()
    return tuple(dict.fromkeys(str(item).strip() for item in source if str(item).strip()))


def _number(
    raw: Any,
    key: str,
    default: float,
    valid: Callable[[float], bool],
    warnings: List[str],
) -> float:
    try:
        value = float(_get(raw, key, default))
    except (TypeError, ValueError, OverflowError):
        value = default
    if not math.isfinite(value) or not valid(value):
        warnings.append(f"{key} is invalid; using {default}")
        return default
    return value


def _integer(
    raw: Any,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
    warnings: List[str],
) -> int:
    try:
        value = float(_get(raw, key, default))
    except (TypeError, ValueError, OverflowError):
        value = float(default)
    if not math.isfinite(value) or not value.is_integer() or not minimum <= value <= maximum:
        warnings.append(f"{key} is invalid; using {default}")
        return default
    return int(value)



_PRESENCE_KNOBS = {"ghost", "sensible", "lively"}


def _presence_knob(value: Any, warnings: List[str]) -> str:
    raw = str(value or "sensible").strip().lower()
    if raw not in _PRESENCE_KNOBS:
        warnings.append("presence_knob is invalid; using sensible")
        return "sensible"
    return raw


def parse_runtime_config(raw: Any) -> Tuple[RuntimeConfig, tuple[str, ...]]:
    """Parse user configuration without allowing invalid values into the pipeline."""
    warnings: List[str] = []
    base = _number(raw, "debounce_base_cooldown", 3.5, lambda value: 0 <= value <= 30, warnings)
    extended = _number(raw, "debounce_extended_cooldown", 6.5, lambda value: 0 <= value <= 60, warnings)
    cap = _number(raw, "debounce_max_cap", 12.0, lambda value: 0 <= value <= 120, warnings)
    if extended < base:
        warnings.append("debounce_extended_cooldown is below base cooldown; using base cooldown")
        extended = base
    if cap < max(base, extended):
        warnings.append("debounce_max_cap is below cooldowns; using the larger cooldown")
        cap = max(base, extended)

    hover = _number(raw, "safe_hover_threshold", 0.40, lambda value: 0 <= value <= 1, warnings)
    strong = _number(raw, "strong_addressivity_threshold", 0.70, lambda value: 0 <= value <= 1, warnings)
    if hover >= strong:
        warnings.append("addressivity thresholds overlap; using defaults 0.40/0.70")
        hover, strong = 0.40, 0.70

    names = _strings(_get(raw, "bot_names", []))
    takeover = _strings(_get(raw, "takeover_groups", []))
    excluded = _strings(_get(raw, "exclude_groups", []))
    prefix = str(_get(raw, "command_prefix", "/") or "/").strip() or "/"
    mode = str(_get(raw, "pipeline_mode", PIPELINE_FILTER) or PIPELINE_FILTER).strip().lower()
    if mode not in _PIPELINE_MODES:
        warnings.append(f"pipeline_mode is invalid; using {PIPELINE_FILTER}")
        mode = PIPELINE_FILTER

    rhythm_timezone = str(_get(raw, "rhythm_timezone", "") or "").strip()
    if rhythm_timezone:
        try:
            ZoneInfo(rhythm_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            warnings.append("rhythm_timezone is invalid or unavailable; using system local timezone")
            rhythm_timezone = ""

    config = RuntimeConfig(
        rhythm_timezone=rhythm_timezone,
        decision_mode=str(_get(raw, "decision_mode", "legacy")) if _get(raw, "decision_mode", "legacy") in ("legacy", "persona_model") else "legacy",
        decision_provider_id=str(_get(raw, "decision_provider", "") or "").strip(),
        decision_timeout=_number(raw, "decision_timeout", 8.0, lambda value: 1 <= value <= 30, warnings),
        reply_timeout=_number(raw, "reply_timeout", 60.0, lambda value: 5 <= value <= 300, warnings),
        tool_agent_timeout=_number(raw, "tool_agent_timeout", 120.0, lambda value: 5 <= value <= 600, warnings),
        enabled=_bool(_get(raw, "enable", True), True),
        pipeline_mode=mode,
        ambient_intervention=_bool(_get(raw, "ambient_intervention", False), False),
        takeover_all=_bool(_get(raw, "takeover_all", False), False),
        takeover_groups=frozenset(takeover),
        exclude_groups=frozenset(excluded),
        bot_names=names,
        provider_id=str(_get(raw, "provider", "") or "").strip(),
        reply_provider_id=str(_get(raw, "reply_provider", "") or "").strip(),
        vibe_provider_id=str(_get(raw, "vibe_provider", "") or "").strip(),
        command_prefix=prefix,
        debounce_base_cooldown=base,
        debounce_extended_cooldown=extended,
        debounce_max_cap=cap,
        strong_addressivity_threshold=strong,
        safe_hover_threshold=hover,
        deep_cooling_minutes=_number(
            raw, "deep_cooling_minutes", 15.0, lambda value: 0 <= value <= 180, warnings
        ),
        chars_per_second=_number(raw, "chars_per_second", 25.0, lambda value: 1 <= value <= 100, warnings),
        base_thinking_delay=_number(raw, "base_thinking_delay", 0.8, lambda value: 0 <= value <= 10, warnings),
        max_fragments=_integer(raw, "max_fragments", 3, 1, 3, warnings),
        max_fragment_chars=_integer(raw, "max_fragment_chars", 120, 40, 500, warnings),
        inter_burst_interval=_number(raw, "inter_burst_interval", 1.2, lambda value: 0.6 <= value <= 3, warnings),
        casual_emoji_enabled=_bool(_get(raw, "casual_emoji_enabled", False), False),
        strip_markdown_in_banter=_bool(_get(raw, "strip_markdown_in_banter", True), True),
        vibe_llm_enabled=_bool(_get(raw, "vibe_llm_enabled", False), False),
        shadow_mode=_bool(_get(raw, "shadow_mode", False), False),
        console_show_message_content=_bool(_get(raw, "console_show_message_content", False), False),
        telemetrics_window_seconds=_number(
            raw, "telemetrics_window_seconds", 60.0, lambda value: 15 <= value <= 300, warnings
        ),
        fast_banter_enter_mpm=_number(
            raw, "fast_banter_enter_mpm", 12.0, lambda value: 4 <= value <= 40, warnings
        ),
        chill_fade_enter_mpm=_number(
            raw, "chill_fade_enter_mpm", 3.0, lambda value: 0.5 <= value <= 12, warnings
        ),
        wts_topic_weight=_number(raw, "wts_topic_weight", 0.12, lambda value: 0 <= value <= 0.5, warnings),
        wts_professionalism_weight=_number(
            raw, "wts_professionalism_weight", 0.08, lambda value: 0 <= value <= 0.5, warnings
        ),
        wts_question_weight=_number(
            raw, "wts_question_weight", 0.08, lambda value: 0 <= value <= 0.5, warnings
        ),
        wts_participation_weight=_number(
            raw, "wts_participation_weight", 0.06, lambda value: 0 <= value <= 0.5, warnings
        ),
        wts_fatigue_weight=_number(
            raw, "wts_fatigue_weight", 1.0, lambda value: 0 <= value <= 2, warnings
        ),
        neural_embedding_enabled=_bool(_get(raw, "neural_embedding_enabled", False), False),
        conversation_router_enabled=_bool(_get(raw, "conversation_router_enabled", True), True),
        topic_reranker_enabled=_bool(_get(raw, "topic_reranker_enabled", True), True),
        topic_reranker_provider=str(_get(raw, "topic_reranker_provider", "") or "").strip(),
        topic_reranker_timeout=_number(raw, "topic_reranker_timeout", 3.0, lambda value: 0.1 <= value <= 10, warnings),
        routing_neural_timeout=_number(raw, "routing_neural_timeout", 0.5, lambda value: 0 <= value <= 2, warnings),
        replay_message_limit=_integer(raw, "replay_message_limit", 500, 80, 500, warnings),
        topic_window_seconds=_number(
            raw, "topic_window_seconds", 300.0, lambda value: 60.0 <= value <= 1800.0, warnings
        ),
        topic_join_threshold=_number(
            raw, "topic_join_threshold", 0.48, lambda value: 0.30 <= value <= 0.85, warnings
        ),
        topic_commit_threshold=_number(
            raw, "topic_commit_threshold", 0.0,
            lambda value: value == 0 or 0.30 <= value <= 0.95, warnings,
        ),
        topic_ambiguity_threshold=_number(
            raw, "topic_ambiguity_threshold", 0.0,
            lambda value: value == 0 or 0.30 <= value <= 0.95, warnings,
        ),
        topic_margin_threshold=_number(
            raw, "topic_margin_threshold", 0.06, lambda value: 0 <= value <= 0.5, warnings,
        ),
        parent_window_seconds=_number(
            raw, "parent_window_seconds", 180.0, lambda value: 30.0 <= value <= 600.0, warnings
        ),
        parent_accept_threshold=_number(
            raw, "parent_accept_threshold", 0.72, lambda value: 0.50 <= value <= 0.95, warnings
        ),
        embedding_provider=str(_get(raw, "embedding_provider", "") or "").strip(),
        neural_link_threshold=_number(
            raw, "neural_link_threshold", 0.78, lambda value: 0.5 <= value <= 0.95, warnings
        ),
        embedding_cache_size=_integer(raw, "embedding_cache_size", 512, 64, 4096, warnings),
        presence_knob=_presence_knob(_get(raw, "presence_knob", "sensible"), warnings),
        social_manners_enabled=_bool(_get(raw, "social_manners_enabled", True), True),
        relay_baton_enabled=_bool(_get(raw, "relay_baton_enabled", True), True),
        private_field_enabled=_bool(_get(raw, "private_field_enabled", True), True),
        hyped_quota_enabled=_bool(_get(raw, "hyped_quota_enabled", True), True),
        mood_memory_enabled=_bool(_get(raw, "mood_memory_enabled", False), False),
        slang_trial_enabled=_bool(_get(raw, "slang_trial_enabled", False), False),
        group_memory_enabled=_bool(_get(raw, "group_memory_enabled", True), True),
        selflearning_integration=_bool(_get(raw, "selflearning_integration", True), True),
        media_image_gate_enabled=_bool(_get(raw, "media_image_gate_enabled", True), True),
        media_voice_gate_enabled=_bool(_get(raw, "media_voice_gate_enabled", True), True),
        media_understand_reply_enabled=_bool(_get(raw, "media_understand_reply_enabled", False), False),
        media_privacy_strict=_bool(_get(raw, "media_privacy_strict", True), True),
        deciding_detect_enabled=_bool(_get(raw, "deciding_detect_enabled", True), True),
        gap_fill_proactive_enabled=_bool(_get(raw, "gap_fill_proactive_enabled", True), True),
        cold_memory_nudge_enabled=_bool(_get(raw, "cold_memory_nudge_enabled", True), True),
        newcomer_caution_enabled=_bool(_get(raw, "newcomer_caution_enabled", True), True),
        pace_align_enabled=_bool(_get(raw, "pace_align_enabled", True), True),
        proactive_quota_enabled=_bool(_get(raw, "proactive_quota_enabled", True), True),
        proactive_quota_per_hour=_integer(raw, "proactive_quota_per_hour", 2, 0, 20, warnings),
        proactive_quota_per_topic=_integer(raw, "proactive_quota_per_topic", 1, 0, 10, warnings),
        daily_rhythm_enabled=_bool(_get(raw, "daily_rhythm_enabled", True), True),
        rhythm_morning_hi_enabled=_bool(_get(raw, "rhythm_morning_hi_enabled", True), True),
        rhythm_day_share_slots=_integer(raw, "rhythm_day_share_slots", 1, 0, 2, warnings),
        rhythm_goodnight_text_quota=_integer(raw, "rhythm_goodnight_text_quota", 1, 1, 2, warnings),
        rhythm_sleep_after_winddown=_bool(_get(raw, "rhythm_sleep_after_winddown", True), True),
        rhythm_allow_self_sleep=_bool(_get(raw, "rhythm_allow_self_sleep", True), True),
        rhythm_allow_wake=_bool(_get(raw, "rhythm_allow_wake", True), True),
        rhythm_insomnia_enabled=_bool(_get(raw, "rhythm_insomnia_enabled", False), False),
        rhythm_force_sleep=_bool(_get(raw, "rhythm_force_sleep", False), False),
        rhythm_skip_morning_hi_tonight=_bool(_get(raw, "rhythm_skip_morning_hi_tonight", False), False),
    )
    return config, tuple(warnings)
