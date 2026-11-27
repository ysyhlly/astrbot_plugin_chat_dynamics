"""Read-only companion presence and series defaults. Panels can also cooperate inside the stock view."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import hmac
import inspect
import math
import os
import re
import sys
import threading
import time
import weakref
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Optional test overrides; runtime resolves astrbot.api.web lazily so offline
# unit tests and standalone CI checks can import series_panels without AstrBot.
json_response: Any = None
request: Any = None

CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CATALOG = (
    ("astrbot_plugin_chat_dynamics", "对话主控", "app", "对话调度与回复"),
    ("astrbot_plugin_dynamics_learning", "群间学习", "review", "学习与审查"),
    ("astrbot_plugin_dynamics_world", "群间世界", "world", "角色与生活状态"),
    ("astrbot_plugin_dynamics_memory", "群间记忆", "dashboard", "记忆与证据"),
    ("astrbot_plugin_dynamics_horizons", "群间见闻", "horizons", "信息源与兴趣见闻"),
    (
        "astrbot_plugin_dynamics_games",
        "群间游戏",
        "games",
        "共读、约局、接龙、挑战与共创",
    ),
    ("astrbot_plugin_visual_media", "视觉工坊", "workshop", "表情、图片与视觉表达"),
    (
        "astrbot_plugin_group_chronicle",
        "群文化沿革",
        "culture",
        "收录有价值的结果与群史传记",
    ),
    ("astrbot_plugin_autonomous_blocking", "成员边界", None, "回复与资料边界"),
    (
        "astrbot_plugin_self_learning",
        "旧 SelfLearning",
        None,
        "兼容检查；已由群间学习接管",
    ),
)

# Canonical unified brand names for the 8 core series plugins.
SERIES_BRAND_NAMES: dict[str, str] = {
    "astrbot_plugin_chat_dynamics": "对话主控",
    "astrbot_plugin_dynamics_world": "群间世界",
    "astrbot_plugin_dynamics_learning": "群间学习",
    "astrbot_plugin_dynamics_memory": "群间记忆",
    "astrbot_plugin_dynamics_horizons": "群间见闻",
    "astrbot_plugin_dynamics_games": "群间游戏",
    "astrbot_plugin_visual_media": "视觉工坊",
    "astrbot_plugin_group_chronicle": "群文化沿革",
}

# Legacy standalone plugins superseded by consolidated series plugins (strong conflicts).
LEGACY_CONFLICTS = (
    (
        "astrbot_plugin_self_learning",
        "旧 SelfLearning",
        "astrbot_plugin_dynamics_learning",
    ),
    ("astrbot_plugin_truth_game", "真心话", "astrbot_plugin_dynamics_games"),
    ("astrbot_plugin_blackjack21", "21点", "astrbot_plugin_dynamics_games"),
    ("astrbot_plugin_rollpig", "今日小猪", "astrbot_plugin_dynamics_games"),
    ("astrbot_plugin_what_to_eat", "是啊，吃什么", "astrbot_plugin_dynamics_games"),
    ("steam_status_monitor_V3", "Steam 状态监控", "astrbot_plugin_dynamics_games"),
    (
        "astrbot_plugin_livingmemory",
        "旧 LivingMemory",
        "astrbot_plugin_dynamics_memory",
    ),
    ("astrbot_plugin_worldbook", "旧世界书", "astrbot_plugin_dynamics_world"),
    (
        "astrbot_plugin_dynamic_life_state",
        "旧动态生活",
        "astrbot_plugin_dynamics_world",
    ),
    ("astrbot_plugin_stealer", "旧表情偷图", "astrbot_plugin_visual_media"),
    (
        "astrbot_plugin_gemini_image_generation",
        "旧生图插件",
        "astrbot_plugin_visual_media",
    ),
    ("astrbot_plugin_meme_manager", "旧表情管理", "astrbot_plugin_visual_media"),
)

# Third-party / ecosystem plugins with gentle feature overlap (non-blocking, coexist cleanly).
OVERLAP_CONFLICTS = (
    (
        "astrbot_plugin_qq_group_daily_analysis",
        "QQ 群日常分析",
        "astrbot_plugin_group_chronicle",
        "与群文化沿革存在部分群聊分析重叠，两者可共存，按需调整定时总结频率即可",
    ),
    (
        "astrbot_plugin_group_relation_analyzer",
        "群关系分析",
        "astrbot_plugin_dynamics_learning",
        "与群间学习的关系图存在部分重叠，两者可共存，不建议强行停用",
    ),
    (
        "astrbot_plugin_portrayal",
        "群友画像",
        "astrbot_plugin_dynamics_memory",
        "与群间记忆的成员画像存在部分重叠，可和平共存，互不阻断",
    ),
    (
        "astrbot_plugin_bili_push",
        "B 站动态推送",
        "astrbot_plugin_dynamics_horizons",
        "与群间见闻的 B 站信息源存在部分重叠，避免同账号双推即可共存",
    ),
    (
        "astrbot_plugin_weather_query",
        "天气查询",
        "astrbot_plugin_dynamics_world",
        "与群间世界的天气感知略有重叠，查询指令完全兼容，可自由共存",
    ),
)

CONFLICT_TIERS = {
    "strong": LEGACY_CONFLICTS,
    "overlap": OVERLAP_CONFLICTS,
}

LEGACY_GAME_IDS = frozenset(
    identity
    for identity, _label, owner in LEGACY_CONFLICTS
    if owner == "astrbot_plugin_dynamics_games"
)

CHAT_DYNAMICS_ID = "astrbot_plugin_chat_dynamics"
_SHARED_BOOL_KEYS = ("takeover_all_groups",)
_SHARED_LIST_KEYS = (
    "takeover_groups",
    "exclude_groups",
    "bot_names",
    "private_shared_group_sources",
)
_SHARED_STR_KEYS = (
    "reply_provider",
    "decision_provider",
    "light_analysis_provider",
    "vision_provider",
    "embedding_provider",
    "rerank_provider",
    "persona_id",
    "rhythm_timezone",
    "ui_language",
    "resource_profile",
)
_SUMMARY_TTL_SECONDS = 10.0
_VALID_BADGE_TONES = frozenset({"accent", "warm", "info", "danger", "muted"})
_SUMMARY_CACHE_LOCK = threading.Lock()
_SUMMARY_CACHE: dict[tuple[str, int], tuple[float, int, dict[str, object] | None]] = {}
_PRESENCE_EPOCH = 0
_LAST_CONTEXT_REF: weakref.ref | None = None


def _remember_context(context) -> None:
    global _LAST_CONTEXT_REF
    if context is None:
        return
    try:
        _LAST_CONTEXT_REF = weakref.ref(context)
    except TypeError:
        pass


def get_presence_epoch(context=None) -> int:
    """Return a cross-plugin cache epoch shared on ``context`` when available."""
    ctx = context
    if ctx is None and _LAST_CONTEXT_REF is not None:
        ctx = _LAST_CONTEXT_REF()
    ctx_epoch = 0
    if ctx is not None:
        raw = getattr(ctx, "_chat_dynamics_presence_epoch", 0)
        if isinstance(raw, int):
            ctx_epoch = raw
    with _SUMMARY_CACHE_LOCK:
        return max(_PRESENCE_EPOCH, ctx_epoch)


def bump_presence_epoch(context=None) -> int:
    """Increment the shared presence/matrix cache epoch across all series plugins."""
    global _PRESENCE_EPOCH
    ctx = context
    if ctx is None and _LAST_CONTEXT_REF is not None:
        ctx = _LAST_CONTEXT_REF()
    with _SUMMARY_CACHE_LOCK:
        ctx_epoch = 0
        if ctx is not None:
            raw = getattr(ctx, "_chat_dynamics_presence_epoch", 0)
            if isinstance(raw, int):
                ctx_epoch = raw
        next_epoch = max(_PRESENCE_EPOCH, ctx_epoch) + 1
        _PRESENCE_EPOCH = next_epoch
    if ctx is not None:
        try:
            setattr(ctx, "_chat_dynamics_presence_epoch", next_epoch)
        except (AttributeError, TypeError):
            pass
    return next_epoch


_PRESENCE_RESPONSE_TTL_SECONDS = 5.0
_PRESENCE_RESPONSE_CACHE: dict[tuple[int, str, int], tuple[float, dict[str, object]]] = {}


def invalidate_presence_cache(plugin_id: str | None = None, *, context=None) -> None:
    """Drop cached get_series_summary() entries and bump the shared cross-plugin epoch."""
    with _SUMMARY_CACHE_LOCK:
        if plugin_id is None:
            _SUMMARY_CACHE.clear()
        else:
            for key in [k for k in _SUMMARY_CACHE if k[0] == plugin_id]:
                _SUMMARY_CACHE.pop(key, None)
        _PRESENCE_RESPONSE_CACHE.clear()
    bump_presence_epoch(context)


invalidate_summary_cache = invalidate_presence_cache
clear_summary_cache = invalidate_presence_cache
invalidate_series_summary_cache = invalidate_presence_cache


def _provider_id_of(provider) -> str:
    if provider is None:
        return ""
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            meta = meta()
        except Exception:
            meta = None
    if meta is not None:
        for attr in ("id", "provider_id"):
            val = getattr(meta, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
    cfg = getattr(provider, "provider_config", None)
    if isinstance(cfg, dict):
        val = cfg.get("id")
        if isinstance(val, str) and val.strip():
            return val.strip()
    for attr in ("id", "provider_id"):
        val = getattr(provider, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def find_provider_silently(context, provider_id: str, *, allow_fallback: bool = False):
    """Look up a provider ID without triggering AstrBot's missing-provider WARN during cold start.

    Inspects ``provider_manager.inst_map`` and provider listing methods first.
    Falls back to ``context.get_provider_by_id()`` only when ``allow_fallback=True``
    or when the host/test-double does not expose ``provider_manager``.
    """
    clean_id = str(provider_id or "").strip()
    if context is None or not clean_id:
        return None
    pm = getattr(context, "provider_manager", None)
    has_pm_maps = False
    if pm is not None:
        for map_attr in (
            "inst_map",
            "embedding_provider_inst_map",
            "rerank_provider_inst_map",
            "stt_provider_inst_map",
            "tts_provider_inst_map",
        ):
            inst_map = getattr(pm, map_attr, None)
            if isinstance(inst_map, dict):
                has_pm_maps = True
                found = inst_map.get(clean_id)
                if found is not None:
                    return found
        for list_attr in (
            "provider_insts",
            "embedding_provider_insts",
            "rerank_provider_insts",
            "stt_provider_insts",
            "tts_provider_insts",
        ):
            insts = getattr(pm, list_attr, None)
            if isinstance(insts, (list, tuple)):
                has_pm_maps = True
                for prov in insts:
                    if _provider_id_of(prov) == clean_id:
                        return prov
    for list_method in (
        "get_all_providers",
        "get_all_embedding_providers",
        "get_all_rerank_providers",
        "get_all_stt_providers",
    ):
        getter = getattr(context, list_method, None)
        if callable(getter) and not inspect.iscoroutinefunction(getter):
            try:
                items = getter() or ()
            except Exception:
                items = ()
            if inspect.isawaitable(items):
                if inspect.iscoroutine(items):
                    items.close()
                items = ()
            for prov in items:
                if _provider_id_of(prov) == clean_id:
                    return prov
    if allow_fallback or not has_pm_maps:
        getter = getattr(context, "get_provider_by_id", None)
        if callable(getter) and not inspect.iscoroutinefunction(getter):
            try:
                found = getter(clean_id)
                if inspect.isawaitable(found):
                    if inspect.iscoroutine(found):
                        found.close()
                    return None
                return found
            except Exception:
                return None
    return None


def read_plugin_health(identity: str, owner) -> dict[str, object] | None:
    """Safely read optional synchronous ``owner.get_series_health()`` diagnostics."""
    if owner is None:
        return None
    getter = getattr(owner, "get_series_health", None)
    if not callable(getter) or inspect.iscoroutinefunction(getter):
        return None
    try:
        raw = getter()
        if inspect.isawaitable(raw):
            if inspect.iscoroutine(raw):
                raw.close()
            return None
    except Exception as exc:
        return {
            "plugin_id": identity,
            "ready": False,
            "degraded": True,
            "providers": [],
            "issues": [f"health_hook_error:{type(exc).__name__}"],
            "metrics": {},
        }
    if not isinstance(raw, dict):
        return None
    providers_out: list[dict[str, object]] = []
    for item in (raw.get("providers") or ())[:16]:
        if not isinstance(item, dict):
            continue
        providers_out.append(
            {
                "role": str(item.get("role") or "")[:48],
                "provider_id": str(item.get("provider_id") or "")[:128],
                "status": str(item.get("status") or "unknown")[:32],
                "detail": str(item.get("detail") or "")[:160],
            }
        )
    issues_out = [
        str(x).strip()[:200]
        for x in (raw.get("issues") or ())[:20]
        if str(x).strip()
    ]
    metrics_raw = raw.get("metrics")
    metrics_out = _safe_json_value(metrics_raw, depth=2) if isinstance(metrics_raw, dict) else {}
    return {
        "plugin_id": identity,
        "ready": bool(raw.get("ready", True)),
        "degraded": bool(raw.get("degraded", bool(issues_out))),
        "providers": providers_out,
        "issues": issues_out,
        "metrics": metrics_out if isinstance(metrics_out, dict) else {},
    }


def _sanitize_summary(raw) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    badge = None
    badge_raw = raw.get("badge")
    if isinstance(badge_raw, dict):
        count_raw = badge_raw.get("count", 0)
        if (
            isinstance(count_raw, bool)
            or not isinstance(count_raw, (int, float))
            or not math.isfinite(count_raw)
        ):
            count = 0
        else:
            count = max(0, int(count_raw))
        tone_raw = badge_raw.get("tone")
        tone = (
            tone_raw.strip().lower()
            if isinstance(tone_raw, str)
            and tone_raw.strip().lower() in _VALID_BADGE_TONES
            else "accent"
        )
        label_raw = badge_raw.get("label")
        label = label_raw.strip()[:48] if isinstance(label_raw, str) else ""
        if label or count > 0 or isinstance(tone_raw, str):
            badge = {"count": count, "tone": tone, "label": label}

    pulse = None
    pulse_raw = raw.get("pulse")
    if isinstance(pulse_raw, dict):
        headline_raw = pulse_raw.get("headline")
        updated_raw = pulse_raw.get("updated_at")
        headline = headline_raw.strip()[:160] if isinstance(headline_raw, str) else ""
        updated_at = updated_raw.strip()[:64] if isinstance(updated_raw, str) else ""
        if headline or updated_at:
            pulse = {"headline": headline, "updated_at": updated_at}

    if badge is None and pulse is None:
        return None
    return {"badge": badge, "pulse": pulse}


def _read_plugin_summary(
    identity: str,
    owner,
    *,
    ttl: float = _SUMMARY_TTL_SECONDS,
    force_refresh: bool = False,
    context=None,
) -> dict[str, object] | None:
    if owner is None:
        return None
    getter = getattr(owner, "get_series_summary", None)
    if not callable(getter) or inspect.iscoroutinefunction(getter):
        return None
    now = time.monotonic()
    epoch = get_presence_epoch(context)
    cache_key = (identity, id(owner))
    if not force_refresh and ttl > 0:
        with _SUMMARY_CACHE_LOCK:
            cached = _SUMMARY_CACHE.get(cache_key)
            if cached is not None and cached[0] > now and cached[1] == epoch:
                return cached[2]
    summary = None
    try:
        raw = getter()
        if inspect.isawaitable(raw):
            if inspect.iscoroutine(raw):
                raw.close()
        else:
            summary = _sanitize_summary(raw)
    except Exception:
        summary = None
    if ttl > 0:
        with _SUMMARY_CACHE_LOCK:
            if len(_SUMMARY_CACHE) > 64:
                expired = [
                    k for k, entry in _SUMMARY_CACHE.items()
                    if entry[0] <= now or (len(entry) >= 3 and entry[1] != epoch)
                ]
                for k in expired:
                    _SUMMARY_CACHE.pop(k, None)
                if len(_SUMMARY_CACHE) > 64:
                    _SUMMARY_CACHE.clear()
            _SUMMARY_CACHE[cache_key] = (now + ttl, epoch, summary)
    return summary


def find_plugin_owner(context, identity: str):
    """Return the active, non-conflicting Star instance for ``identity``, or ``None``."""
    if context is None or not identity:
        return None
    registered_entries = _iter_registered_entries(context)
    if registered_entries:
        matches = []
        for names, row in registered_entries:
            if identity in names and all(existing is not row for existing in matches):
                matches.append(row)
        if len(matches) == 1:
            row = matches[0]
            if value(row, "activated", False) is not True:
                return None
            return _extract_owner(row)
        if len(matches) > 1:
            return None
    get_star = getattr(context, "get_registered_star", None)
    if callable(get_star) and not inspect.iscoroutinefunction(get_star):
        try:
            star = get_star(identity)
        except Exception:
            return None
        if inspect.isawaitable(star):
            if inspect.iscoroutine(star):
                star.close()
            return None
        if star is None or value(star, "activated", True) is False:
            return None
        return _extract_owner(star)
    return None


def value(row, key, default=None):
    return (
        row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)
    )


def registered_names(key, row):
    names = set()
    for item in (
        key,
        value(row, "name"),
        value(row, "root_dir_name"),
        value(row, "module_path"),
    ):
        if isinstance(item, str):
            names.update(item.split("."))
    return names


def _extract_owner(row):
    return next(
        (
            value(row, k)
            for k in ("star_cls", "star", "plugin")
            if value(row, k) is not None and not isinstance(value(row, k), type)
        ),
        None,
    )


def _safe_dir_segment(name) -> str | None:
    if not isinstance(name, str):
        return None
    clean = name.strip()
    if not clean or clean in {".", ".."} or "/" in clean or "\\" in clean:
        return None
    return clean


def _owner_plugin_dirs(owner) -> tuple[Path, ...]:
    if owner is None:
        return ()
    mod_name = getattr(type(owner), "__module__", "") or ""
    if not mod_name or mod_name == "builtins":
        return ()
    top_pkg = mod_name.split(".", 1)[0]
    if top_pkg in sys.stdlib_module_names:
        return ()
    candidates: list[Path] = []
    seen: set[Path] = set()
    for mod in (sys.modules.get(top_pkg), sys.modules.get(mod_name)):
        probe = getattr(mod, "__file__", None)
        if not probe:
            continue
        try:
            resolved = Path(probe).resolve()
        except OSError:
            continue
        parent = resolved.parent if resolved.is_file() else resolved
        for ancestor in (parent, parent.parent, parent.parent.parent):
            if ancestor in seen or ancestor == ancestor.parent:
                continue
            if (
                (ancestor / "pages").is_dir()
                or (ancestor / "views").is_dir()
                or (ancestor / "metadata.yaml").is_file()
            ):
                seen.add(ancestor)
                candidates.append(ancestor)
                break
    return tuple(candidates)


def _normalize_str_list(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("\r", "\n").replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, (str, int)):
            clean = str(item).strip()
            if clean and clean not in seen:
                seen.add(clean)
                out.append(clean)
    return out


def _safe_json_value(raw, *, depth: int = 2):
    if raw is None or isinstance(raw, (bool, int, float, str)):
        return raw.strip() if isinstance(raw, str) else raw
    if depth <= 0:
        return None
    if isinstance(raw, (list, tuple)):
        items = [_safe_json_value(v, depth=depth - 1) for v in raw]
        return [item for item in items if item is not None]
    if isinstance(raw, dict):
        cleaned: dict[str, object] = {}
        for k, v in raw.items():
            if isinstance(k, str):
                safe_v = _safe_json_value(v, depth=depth - 1)
                if safe_v is not None:
                    cleaned[k] = safe_v
        return cleaned
    return None


def _sanitize_shared_config(raw: dict) -> dict:
    cleaned: dict[str, object] = {}
    for key in _SHARED_BOOL_KEYS:
        val = raw.get(key)
        cleaned[key] = val if isinstance(val, bool) else False
    for key in _SHARED_LIST_KEYS:
        cleaned[key] = _normalize_str_list(raw.get(key))
    for key in _SHARED_STR_KEYS:
        val = raw.get(key)
        cleaned[key] = val.strip() if isinstance(val, str) else ""
    quiet = _safe_json_value(raw.get("quiet_hours"))
    cleaned["quiet_hours"] = quiet if quiet is not None else ""
    profiles = _safe_json_value(raw.get("group_profiles"), depth=3)
    cleaned["group_profiles"] = profiles if isinstance(profiles, dict) else {}
    return cleaned


def _iter_registered_entries(context) -> list[tuple[set[str], object]]:
    if context is None:
        return []
    get_all_stars = getattr(context, "get_all_stars", None)
    if not callable(get_all_stars) or inspect.iscoroutinefunction(get_all_stars):
        return []
    try:
        raw = get_all_stars()
    except Exception:
        return []
    if inspect.isawaitable(raw):
        if inspect.iscoroutine(raw):
            raw.close()
        return []
    if not raw or isinstance(raw, (str, bytes)):
        return []
    if isinstance(raw, dict):
        entries = list(raw.items())
    else:
        try:
            entries = [("", row) for row in raw]
        except TypeError:
            return []
    return [(registered_names(key, row), row) for key, row in entries]


def _find_chat_dynamics_owner(context):
    if context is None:
        return None
    registered_entries = _iter_registered_entries(context)
    if registered_entries:
        matches = []
        for names, row in registered_entries:
            if CHAT_DYNAMICS_ID in names and all(
                existing is not row for existing in matches
            ):
                matches.append(row)
        if len(matches) == 1:
            row = matches[0]
            if value(row, "activated", False) is not True:
                return None
            return _extract_owner(row)
        if len(matches) > 1:
            return None

    get_star = getattr(context, "get_registered_star", None)
    if callable(get_star) and not inspect.iscoroutinefunction(get_star):
        try:
            star = get_star(CHAT_DYNAMICS_ID)
        except Exception:
            return None
        if inspect.isawaitable(star):
            if inspect.iscoroutine(star):
                star.close()
            return None
        if star is None:
            return None
        if value(star, "activated", True) is False:
            return None
        owner = _extract_owner(star)
        if owner is not None:
            return owner
        if callable(getattr(star, "get_series_shared_config", None)):
            return star
    return None


def get_series_defaults(context) -> dict:
    """Read allowlisted non-secret shared defaults published by chat_dynamics.

    Strictly synchronous, read-only, and side-effect free. Returns ``{}`` when
    ``astrbot_plugin_chat_dynamics`` is absent, unloaded, disabled, or fails.
    """
    owner = _find_chat_dynamics_owner(context)
    if owner is None:
        return {}
    getter = getattr(owner, "get_series_shared_config", None)
    if not callable(getter) or inspect.iscoroutinefunction(getter):
        return {}
    try:
        raw = getter()
    except Exception:
        return {}
    if inspect.isawaitable(raw):
        if inspect.iscoroutine(raw):
            raw.close()
        return {}
    if not isinstance(raw, dict) or not raw:
        return {}
    if raw.get("enabled") is False:
        return {}
    return _sanitize_shared_config(raw)


def inherit_umos(explicit, context) -> list[str]:
    """Return normalized explicit UMOs, or fall back to chat_dynamics shared groups."""
    own = _normalize_str_list(explicit)
    if own:
        return own
    defaults = get_series_defaults(context)
    groups = defaults.get("takeover_groups")
    return list(groups) if isinstance(groups, list) else []


def inherit_provider(explicit, context, role: str = "reply") -> str:
    """Return explicit provider ID if configured, else chat_dynamics default provider."""
    own = explicit.strip() if isinstance(explicit, str) else ""
    if own:
        return own
    defaults = get_series_defaults(context)
    if not defaults:
        return ""
    if role == "decision":
        return str(
            defaults.get("decision_provider") or defaults.get("reply_provider") or ""
        )
    if role == "light_analysis":
        return str(
            defaults.get("light_analysis_provider")
            or defaults.get("decision_provider")
            or defaults.get("reply_provider")
            or ""
        )
    if role == "vision":
        return str(
            defaults.get("vision_provider") or defaults.get("reply_provider") or ""
        )
    if role == "embedding":
        return str(defaults.get("embedding_provider") or "")
    if role == "rerank":
        return str(defaults.get("rerank_provider") or "")
    return str(defaults.get("reply_provider") or "")


def get_member_privacy_muted(
    context,
    umo: str = "",
    member_id: str = "",
    scope: str = "",
) -> dict:
    """Read-only lookup into chat_dynamics's cross-plugin privacy mute registry."""
    owner = _find_chat_dynamics_owner(context)
    if owner is None:
        return {"muted": False, "scopes": []}
    getter = getattr(owner, "get_series_privacy_muted", None)
    if not callable(getter) or inspect.iscoroutinefunction(getter):
        return {"muted": False, "scopes": []}
    try:
        raw = getter(
            umo=str(umo or "").strip(),
            member_id=str(member_id or "").strip(),
            scope=str(scope or "").strip(),
        )
    except TypeError:
        try:
            raw = getter(str(umo or "").strip(), str(member_id or "").strip())
        except Exception:
            return {"muted": False, "scopes": []}
    except Exception:
        return {"muted": False, "scopes": []}
    if inspect.isawaitable(raw):
        if inspect.iscoroutine(raw):
            raw.close()
        return {"muted": False, "scopes": []}
    if isinstance(raw, dict):
        return raw
    return {"muted": bool(raw), "scopes": []}


def is_member_privacy_muted(
    context,
    umo: str,
    member_id: str,
    scope: str = "",
) -> bool:
    """Lightweight read-only helper: True if ``member_id`` opted out of ``scope`` in ``umo``."""
    clean_umo = str(umo or "").strip()
    clean_member = str(member_id or "").strip()
    if not clean_umo or not clean_member:
        return False
    info = get_member_privacy_muted(
        context,
        umo=clean_umo,
        member_id=clean_member,
        scope=str(scope or "").strip(),
    )
    return bool(info.get("muted", False))


class ProviderBudgetBusyError(RuntimeError):
    """Raised when the shared ProviderBudget queue is full or acquire_timeout elapses."""

    is_provider_budget_busy = True

    def __init__(
        self,
        message: str = "Provider admission queue is busy",
        *,
        provider: str = "",
        purpose: str = "",
        reason: str = "busy",
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.provider = str(provider or "")
        self.purpose = str(purpose or "")
        self.reason = str(reason or "busy")
        try:
            retry_val = float(retry_after or 0.0)
        except (TypeError, ValueError):
            retry_val = 0.0
        self.retry_after = max(0.0, retry_val) if math.isfinite(retry_val) else 0.0


def is_provider_budget_busy_error(exc: BaseException | None) -> bool:
    """Identify a ProviderBudgetBusyError even across plugin module boundaries."""
    if exc is None:
        return False
    return bool(
        isinstance(exc, ProviderBudgetBusyError)
        or getattr(exc, "is_provider_budget_busy", False)
        or type(exc).__name__ == "ProviderBudgetBusyError"
    )


def provider_circuit_remaining(context, provider_id: str) -> float:
    """Read-only pre-flight check of remaining circuit cooldown seconds for a provider.

    Reads ``context._chat_dynamics_provider_budget`` silently without logging or
    calling ``context.get_provider_by_id``. Returns ``0.0`` when unavailable or closed.
    """
    clean_id = str(provider_id or "").strip()
    if context is None or not clean_id:
        return 0.0
    budget = getattr(context, "_chat_dynamics_provider_budget", None)
    if budget is None:
        return 0.0
    try:
        public_fn = getattr(budget, "circuit_remaining", None)
        if callable(public_fn) and not inspect.iscoroutinefunction(public_fn):
            val = float(public_fn(clean_id) or 0.0)
            return max(0.0, val) if math.isfinite(val) else 0.0
        private_fn = getattr(budget, "_circuit_remaining", None)
        if callable(private_fn) and not inspect.iscoroutinefunction(private_fn):
            res = private_fn(clean_id)
            rem = res[0] if isinstance(res, tuple) and res else res
            val = float(rem or 0.0)
            return max(0.0, val) if math.isfinite(val) else 0.0
    except Exception:
        return 0.0
    return 0.0


def get_series_shared_config(context) -> dict:
    """Return sanitized shared defaults from chat_dynamics, or ``{}`` when absent."""
    return get_series_defaults(context)


def _valid_zoneinfo(name: str):
    clean = str(name or "").strip()
    if not clean:
        return None
    try:
        return ZoneInfo(clean)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None


def resolve_series_timezone_name(
    context, explicit_tz: str = "", default_tz: str = "Asia/Shanghai"
) -> str:
    """Resolve normalized IANA timezone name: explicit > chat_dynamics > default > Asia/Shanghai."""
    shared = get_series_shared_config(context)
    candidates = (
        explicit_tz,
        shared.get("rhythm_timezone", "") if isinstance(shared, dict) else "",
        default_tz,
        "Asia/Shanghai",
        "UTC",
    )
    for candidate in candidates:
        clean = str(candidate or "").strip()
        if not clean:
            continue
        if _valid_zoneinfo(clean) is not None:
            return clean
    return "UTC"


def resolve_series_timezone(
    context, explicit_tz: str = "", default_tz: str = "Asia/Shanghai"
):
    """Resolve a tzinfo (ZoneInfo when available): explicit > chat_dynamics > default."""
    name = resolve_series_timezone_name(
        context, explicit_tz=explicit_tz, default_tz=default_tz
    )
    tz = _valid_zoneinfo(name)
    if tz is not None:
        return tz
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8))
    return timezone.utc


def resolve_series_provider(
    context,
    explicit_provider: str = "",
    fallback_provider: str = "",
    *,
    role: str = "reply",
) -> str:
    """Resolve provider ID: explicit > chat_dynamics shared provider > fallback > ''."""
    own = explicit_provider.strip() if isinstance(explicit_provider, str) else ""
    if own:
        return own
    inherited = inherit_provider("", context, role=role)
    if inherited:
        return inherited
    return fallback_provider.strip() if isinstance(fallback_provider, str) else ""


def is_series_group_excluded(context, umo: str = "", group_id: str = "") -> bool:
    """Return True if the group/UMO is listed in chat_dynamics exclude_groups."""
    shared = get_series_shared_config(context)
    raw_excluded = shared.get("exclude_groups") if isinstance(shared, dict) else None
    if not raw_excluded:
        return False
    excluded = _normalize_str_list(raw_excluded)
    if not excluded:
        return False
    clean_umo = str(umo or "").strip()
    clean_gid = str(group_id or "").strip()
    if not clean_gid and ":" in clean_umo:
        clean_gid = clean_umo.rsplit(":", 1)[-1].strip()
    if not clean_umo and not clean_gid:
        return False
    for entry in excluded:
        if clean_umo and entry == clean_umo:
            return True
        if clean_gid and entry == clean_gid:
            return True
        if not clean_umo and clean_gid and ":" in entry:
            if entry.rsplit(":", 1)[-1].strip() == clean_gid:
                return True
    return False


def is_series_in_quiet_hours(context) -> bool:
    """Return True when chat_dynamics reports active quiet/sleep hours."""
    shared = get_series_shared_config(context)
    quiet = shared.get("quiet_hours") if isinstance(shared, dict) else None
    if isinstance(quiet, dict):
        return bool(quiet.get("in_quiet_hours", False))
    return False


def _is_error_llm_response(response) -> bool:
    if response is None:
        return False
    return str(getattr(response, "role", "") or "").strip().lower() == "err"


def record_series_llm_usage(
    context,
    *,
    plugin_id: str,
    provider_id: str,
    purpose: str,
    response=None,
    queue_seconds: float = 0.0,
    generation_seconds: float = 0.0,
    ok: bool = True,
    error_type: str = "",
    model: str = "",
    group_id: str = "",
    image_count: int = 0,
) -> bool:
    """Report one LLM/image call's usage & timing to chat_dynamics telemetry if present."""
    if context is None:
        return False
    telemetry = getattr(context, "_chat_dynamics_series_telemetry", None)
    if telemetry is None:
        budget = getattr(context, "_chat_dynamics_provider_budget", None)
        telemetry = getattr(budget, "telemetry", None) if budget is not None else None
    recorder = getattr(telemetry, "record", None) if telemetry is not None else None
    if not callable(recorder):
        return False
    base_kwargs = {
        "plugin_id": str(plugin_id or "unknown").strip() or "unknown",
        "provider_id": str(provider_id or "default").strip() or "default",
        "purpose": str(purpose or "background").strip() or "background",
        "response": response,
        "queue_seconds": max(0.0, float(queue_seconds or 0.0)),
        "generation_seconds": max(0.0, float(generation_seconds or 0.0)),
        "ok": bool(ok),
        "error_type": str(error_type or "").strip()[:64],
    }
    ext_kwargs = {
        **base_kwargs,
        "model": str(model or "").strip()[:128],
        "group_id": str(group_id or "").strip()[:128],
        "image_count": max(0, int(image_count or 0)),
    }
    try:
        recorder(**ext_kwargs)
        return True
    except TypeError:
        try:
            recorder(**base_kwargs)
            return True
        except Exception:
            return False
    except Exception:
        return False


async def series_llm_call(
    context,
    provider_id: str,
    plugin_id: str,
    purpose: str,
    invoke,
    *,
    acquire_timeout: float | None = 15.0,
    model: str = "",
    group_id: str = "",
):
    """Run an LLM call via shared ProviderBudget when present, else directly.

    Translates cross-plugin ``ProviderBudgetBusyError`` into this module's
    ``ProviderBudgetBusyError`` so companion plugins can catch it without
    importing ``astrbot_plugin_chat_dynamics``.
    """
    clean_provider = str(provider_id or "").strip() or "default"
    clean_plugin = str(plugin_id or "").strip() or "unknown"
    clean_purpose = str(purpose or "").strip() or "background"
    clean_model = str(model or "").strip()[:128]
    clean_group = str(group_id or "").strip()[:128]
    budget = (
        getattr(context, "_chat_dynamics_provider_budget", None)
        if context is not None
        else None
    )
    runner = getattr(budget, "run", None) if budget is not None else None
    if callable(runner) and inspect.iscoroutinefunction(runner):
        if getattr(budget, "supports_series_telemetry", False) is True:
            try:
                run_kwargs: dict[str, object] = {
                    "plugin_id": clean_plugin,
                    "acquire_timeout": acquire_timeout,
                }
                if clean_model or clean_group:
                    try:
                        sig = inspect.signature(runner)
                        if "model" in sig.parameters:
                            run_kwargs["model"] = clean_model
                        if "group_id" in sig.parameters:
                            run_kwargs["group_id"] = clean_group
                    except (TypeError, ValueError):
                        pass
                return await runner(
                    clean_provider,
                    clean_purpose,
                    invoke,
                    **run_kwargs,
                )
            except BaseException as exc:
                if is_provider_budget_busy_error(exc):
                    if isinstance(exc, ProviderBudgetBusyError):
                        raise
                    raise ProviderBudgetBusyError(
                        str(exc),
                        provider=getattr(exc, "provider", clean_provider),
                        purpose=getattr(exc, "purpose", clean_purpose),
                        reason=getattr(exc, "reason", "busy"),
                        retry_after=float(getattr(exc, "retry_after", 0.0) or 0.0),
                    ) from exc
                raise
        started = time.monotonic()
        try:
            result = await runner(clean_provider, clean_purpose, invoke)
        except BaseException as exc:
            elapsed = max(0.0, time.monotonic() - started)
            if is_provider_budget_busy_error(exc):
                record_series_llm_usage(
                    context,
                    plugin_id=clean_plugin,
                    provider_id=clean_provider,
                    purpose=clean_purpose,
                    queue_seconds=elapsed,
                    ok=False,
                    error_type=f"budget_{getattr(exc, 'reason', 'busy')}",
                    model=clean_model,
                    group_id=clean_group,
                )
                if isinstance(exc, ProviderBudgetBusyError):
                    raise
                raise ProviderBudgetBusyError(
                    str(exc),
                    provider=getattr(exc, "provider", clean_provider),
                    purpose=getattr(exc, "purpose", clean_purpose),
                    reason=getattr(exc, "reason", "busy"),
                    retry_after=float(getattr(exc, "retry_after", 0.0) or 0.0),
                ) from exc
            record_series_llm_usage(
                context,
                plugin_id=clean_plugin,
                provider_id=clean_provider,
                purpose=clean_purpose,
                generation_seconds=elapsed,
                ok=False,
                error_type=type(exc).__name__,
                model=clean_model,
                group_id=clean_group,
            )
            raise
        elapsed = max(0.0, time.monotonic() - started)
        is_err = _is_error_llm_response(result)
        record_series_llm_usage(
            context,
            plugin_id=clean_plugin,
            provider_id=clean_provider,
            purpose=clean_purpose,
            response=result,
            generation_seconds=elapsed,
            ok=not is_err,
            error_type="host_error_response" if is_err else "",
            model=clean_model,
            group_id=clean_group,
        )
        return result

    started = time.monotonic()
    try:
        value = invoke()
        result = await value if inspect.isawaitable(value) else value
    except BaseException as exc:
        record_series_llm_usage(
            context,
            plugin_id=clean_plugin,
            provider_id=clean_provider,
            purpose=clean_purpose,
            generation_seconds=max(0.0, time.monotonic() - started),
            ok=False,
            error_type=type(exc).__name__,
            model=clean_model,
            group_id=clean_group,
        )
        raise
    elapsed = max(0.0, time.monotonic() - started)
    is_err = _is_error_llm_response(result)
    record_series_llm_usage(
        context,
        plugin_id=clean_plugin,
        provider_id=clean_provider,
        purpose=clean_purpose,
        response=result,
        generation_seconds=elapsed,
        ok=not is_err,
        error_type="host_error_response" if is_err else "",
        model=clean_model,
        group_id=clean_group,
    )
    return result


def presence(context, current, root=None, *, force_refresh: bool = False):
    _remember_context(context)
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    registered_entries = _iter_registered_entries(context)
    output = []
    for identity, label, page, role in CATALOG:
        matches = []
        for names, row in registered_entries:
            if identity in names and all(existing is not row for existing in matches):
                matches.append(row)
        installed = bool(matches) or (root / identity).is_dir()
        data = {
            "id": identity,
            "label": label,
            "brand_name": SERIES_BRAND_NAMES.get(identity, label),
            "role": role,
            "page": page,
            "association": identity == "astrbot_plugin_group_chronicle",
            "current": identity == current,
            "installed": installed,
            "activated": False,
            "loaded": False,
            "ready": None,
            "runtime_enabled": None,
            "version": "",
            "can_open": False,
            "badge": None,
            "pulse": None,
        }
        if not matches:
            data["state"] = "unloaded" if installed else "missing"
        elif len(matches) > 1:
            data["state"] = "conflict"
        else:
            row = matches[0]
            data["activated"] = value(row, "activated", False) is True
            data["version"] = str(value(row, "version", "") or "")[:128]
            owner = _extract_owner(row)
            revision = getattr(
                getattr(owner, "_series_panels", None), "source_commit", ""
            )
            data["source_commit"] = (
                revision
                if isinstance(revision, str)
                and re.fullmatch(r"[0-9a-f]{7,12}", revision)
                else ""
            )
            data["loaded"] = owner is not None
            candidate_dirs = [root / identity]
            custom_dir = _safe_dir_segment(value(row, "root_dir_name"))
            if custom_dir and custom_dir != identity:
                candidate_dirs.append(root / custom_dir)
            candidate_dirs.extend(_owner_plugin_dirs(owner))
            data["can_open"] = bool(
                data["activated"]
                and data["loaded"]
                and page
                and any(
                    (base_dir / folder / page / "index.html").is_file()
                    for base_dir in candidate_dirs
                    for folder in ("pages", "views")
                )
            )
            if not data["activated"]:
                data["state"] = "disabled"
            elif not data["loaded"]:
                data["state"] = "unloaded"
            else:
                data["state"] = "enabled"
                getter = getattr(owner, "get_dynamics_capabilities", None)
                if callable(getter) and not inspect.iscoroutinefunction(getter):
                    try:
                        caps = getter()
                        if inspect.isawaitable(caps):
                            if inspect.iscoroutine(caps):
                                caps.close()
                        elif (
                            isinstance(caps, dict)
                            and type(caps.get("schema_version")) is int
                            and caps["schema_version"] == 1
                        ):
                            data["ready"] = (
                                caps.get("ready")
                                if type(caps.get("ready")) is bool
                                else None
                            )
                            data["runtime_enabled"] = (
                                caps.get("enabled")
                                if type(caps.get("enabled")) is bool
                                else None
                            )
                            data["state"] = (
                                "inactive"
                                if data["runtime_enabled"] is False
                                else "ready"
                                if data["ready"] is True
                                else "not_ready"
                                if data["ready"] is False
                                else "enabled"
                            )
                    except Exception as exc:
                        data.update(state="error", error=type(exc).__name__)
                summary = _read_plugin_summary(
                    identity, owner, force_refresh=force_refresh, context=context
                )
                if summary:
                    data["badge"] = summary.get("badge")
                    data["pulse"] = summary.get("pulse")
        output.append(data)

    active_ids = {
        identity
        for names, row in registered_entries
        if value(row, "activated", False) is True
        for identity in names
    }
    conflicts = [
        {"id": identity, "label": label, "owner": owner, "severity": "strong"}
        for identity, label, owner in LEGACY_CONFLICTS
        if identity in active_ids and owner in active_ids
    ]
    overlaps = []
    for identity, label, owner, hint in OVERLAP_CONFLICTS:
        short_id = (
            identity[len("astrbot_plugin_") :]
            if identity.startswith("astrbot_plugin_")
            else identity
        )
        matched_id = (
            identity
            if identity in active_ids
            else short_id
            if short_id in active_ids
            else ""
        )
        if matched_id and owner in active_ids:
            overlaps.append(
                {
                    "id": matched_id,
                    "label": label,
                    "owner": owner,
                    "severity": "overlap",
                    "hint": hint,
                }
            )

    return {
        "schema_version": 1,
        "current": current,
        "plugins": output,
        "legacy_conflict": bool(conflicts),
        "legacy_conflicts": conflicts,
        "has_overlap_plugins": bool(overlaps),
        "overlap_plugins": overlaps,
        "series_defaults": get_series_defaults(context),
    }


def _resolve_git_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        first_line = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
        if first_line and first_line[0].startswith("gitdir:"):
            raw = first_line[0].split(":", 1)[1].strip()
            if raw:
                target = Path(raw)
                return target if target.is_absolute() else (root / target).resolve()
    return None


def _read_git_head_commit(root: Path) -> str:
    """Read HEAD commit SHA directly from .git files without spawning a git subprocess."""
    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return ""
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        return ""
    head = head_file.read_text(encoding="utf-8", errors="replace").strip()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        return head[:12]
    if not head.startswith("ref:"):
        return ""
    ref_name = head.split(":", 1)[1].strip()
    if not ref_name or ".." in Path(ref_name).parts:
        return ""
    search_dirs = [git_dir]
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        raw_common = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
        if raw_common:
            common_path = Path(raw_common)
            search_dirs.append(
                common_path if common_path.is_absolute() else (git_dir / common_path).resolve()
            )
    for base in search_dirs:
        ref_path = base / ref_name
        if ref_path.is_file():
            revision = ref_path.read_text(encoding="utf-8", errors="replace").strip()
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
                return revision[:12]
    for base in search_dirs:
        packed = base / "packed-refs"
        if not packed.is_file():
            continue
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref_name:
                revision = parts[0].strip()
                if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
                    return revision[:12]
    return ""


def _installed_commit(root: Path) -> str:
    try:
        metadata_path = root / "metadata.yaml"
        if metadata_path.is_file():
            metadata = metadata_path.read_text(encoding="utf-8-sig")
            match = re.search(
                r"^# source-commit: ([0-9a-f]{40}|[0-9a-f]{64})$", metadata, re.MULTILINE
            )
            if match:
                return match.group(1)[:12]
        return _read_git_head_commit(root)
    except (OSError, UnicodeError):
        pass
    return ""


def _resolve_web_request(req: Any = None) -> Any:
    """Resolve the active AstrBot web request lazily (honoring test overrides)."""
    if req is not None:
        return req
    if request is not None:
        return request
    try:
        from astrbot.api.web import request as web_request

        return web_request
    except ImportError:
        return None


def _web_json_response(data: Any, *, status_code: int = 200) -> Any:
    """Build a JSON response lazily so module import works without astrbot.api.web."""
    responder = json_response
    if responder is None:
        from astrbot.api.web import json_response as responder
    return responder(data, status_code=status_code)


def _mapping_get_ci(mapping: Any, name: str) -> str:
    if mapping is None:
        return ""
    target = name.lower()
    getter = getattr(mapping, "get", None)
    if callable(getter):
        try:
            direct = getter(name)
            if direct is None:
                direct = getter(target)
            if direct is not None:
                return str(direct)
        except Exception:
            pass
    items = getattr(mapping, "items", None)
    if callable(items):
        try:
            for key, val in items():
                if str(key).lower() == target and val is not None:
                    return str(val)
        except Exception:
            pass
    return ""


def _header_get(req: Any, name: str) -> str:
    value = _mapping_get_ci(getattr(req, "headers", None), name)
    if value:
        return value
    raw = getattr(req, "_request", None)
    if raw is not None:
        return _mapping_get_ci(getattr(raw, "headers", None), name)
    return ""


def _cookie_get(req: Any, name: str) -> str:
    value = _mapping_get_ci(getattr(req, "cookies", None), name)
    if value:
        return value
    raw = getattr(req, "_request", None)
    if raw is not None:
        return _mapping_get_ci(getattr(raw, "cookies", None), name)
    return ""


def _normalize_host_for_scheme(host: str, scheme: str) -> str:
    clean = host.strip().lower()
    if not clean:
        return ""
    if scheme == "http" and clean.endswith(":80"):
        return clean[:-3]
    if scheme == "https" and clean.endswith(":443"):
        return clean[:-4]
    return clean


def _extract_expected_hosts(req: Any) -> set[str]:
    hosts: set[str] = set()
    for header_name in ("Host", "X-Forwarded-Host"):
        raw_val = _header_get(req, header_name).strip()
        if raw_val:
            first = raw_val.split(",", 1)[0].strip().lower()
            if first:
                hosts.add(first)
    if not hosts:
        raw = getattr(req, "_request", None)
        url_obj = getattr(raw, "url", None) if raw is not None else None
        netloc = str(getattr(url_obj, "netloc", "") or "").strip().lower()
        if netloc:
            hosts.add(netloc)
    return hosts


def _origin_matches_hosts(origin_or_referer: str, expected_hosts: set[str]) -> bool:
    parsed = urlsplit(origin_or_referer.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        return False
    origin_netloc = _normalize_host_for_scheme(parsed.netloc, scheme)
    if not origin_netloc:
        return False
    for expected in expected_hosts:
        if origin_netloc == _normalize_host_for_scheme(expected, scheme):
            return True
    return False


def verify_series_csrf(
    request: Any = None,
    *,
    methods: frozenset[str] | set[str] | tuple[str, ...] = CSRF_PROTECTED_METHODS,
) -> None:
    """Enforce fail-closed same-origin / Fetch-Metadata / CSRF checks on write requests.

    Safe methods (GET/HEAD/OPTIONS) pass without CSRF checks.
    State-changing methods (POST/PUT/PATCH/DELETE or unknown/empty method) must:
    - Not carry a cross-site ``Sec-Fetch-Site`` value (only ``same-origin``/``none`` allowed).
    - Match ``Host`` when ``Origin`` (or fallback ``Referer``) is present (blocks ``Origin: null``).
    - Match ``X-CSRF-Token``/``X-XSRF-Token`` against ``csrf_token``/``XSRF-TOKEN`` cookie if cookie is set.
    - When both ``Origin`` and ``Referer`` are absent, fail closed unless an explicit
      non-ambient proof is present (``Sec-Fetch-Site: same-origin|none``,
      ``Authorization: Bearer ...``, ``X-CSRF-Token``/``X-XSRF-Token``, or ``X-Requested-With``).
    """
    req = _resolve_web_request(request)
    if req is None:
        raise PermissionError("CSRF 校验失败：缺少请求上下文")
    raw_method = getattr(req, "method", None)
    if not raw_method:
        raw = getattr(req, "_request", None)
        raw_method = getattr(raw, "method", None) if raw is not None else None
    method = str(raw_method or "").strip().upper()
    if method in CSRF_SAFE_METHODS:
        return
    protected = {str(m).strip().upper() for m in methods}
    if method and method not in protected:
        return

    sec_fetch_site = _header_get(req, "Sec-Fetch-Site").strip().lower()
    if sec_fetch_site and sec_fetch_site not in {"same-origin", "none"}:
        raise PermissionError("CSRF 校验失败：拒绝跨站请求")

    origin = _header_get(req, "Origin").strip()
    referer = _header_get(req, "Referer").strip()
    expected_hosts = _extract_expected_hosts(req)

    if origin:
        if not expected_hosts or not _origin_matches_hosts(origin, expected_hosts):
            raise PermissionError("CSRF 校验失败：请求来源与宿主不一致")
    elif referer:
        if not expected_hosts or not _origin_matches_hosts(referer, expected_hosts):
            raise PermissionError("CSRF 校验失败：Referer 来源与宿主不一致")

    csrf_header = (
        _header_get(req, "X-CSRF-Token") or _header_get(req, "X-XSRF-Token")
    ).strip()
    csrf_cookie = (
        _cookie_get(req, "csrf_token")
        or _cookie_get(req, "csrftoken")
        or _cookie_get(req, "XSRF-TOKEN")
    ).strip()
    if csrf_cookie:
        if not csrf_header or not hmac.compare_digest(csrf_header, csrf_cookie):
            raise PermissionError("CSRF 校验失败：CSRF Token 不匹配")

    if not origin and not referer:
        auth_header = _header_get(req, "Authorization").strip()
        has_bearer = (
            auth_header.lower().startswith("bearer ") and len(auth_header[7:].strip()) > 0
        )
        has_xhr = bool(_header_get(req, "X-Requested-With").strip())
        has_same_origin_fetch = sec_fetch_site in {"same-origin", "none"}
        if not (has_same_origin_fetch or has_bearer or csrf_header or has_xhr):
            raise PermissionError("CSRF 校验失败：缺少同源来源头或 CSRF 凭据")


async def require_series_admin(
    context: Any,
    request: Any = None,
    *,
    check_csrf: bool = False,
) -> str:
    """Verify the current HTTP request is authenticated as the configured Dashboard admin.

    - Fails closed if request/raw request is missing or ``astrbot.dashboard.api.auth`` is unavailable.
    - Rejects unauthenticated users, ``api_key:`` principals, and mismatched ``request.username``.
    - Fails closed when ``dashboard.username`` is missing or empty in host config.
    - When ``check_csrf=True``, enforces :func:`verify_series_csrf` on write HTTP methods
      (``POST``/``PUT``/``PATCH``/``DELETE``).
    - Returns the verified Dashboard admin username string; raises ``PermissionError`` otherwise.
    """
    req = _resolve_web_request(request)
    if req is None:
        raise PermissionError("需要 Dashboard 管理员登录")
    raw = getattr(req, "_request", None)
    if raw is None:
        raise PermissionError("需要 Dashboard 管理员登录")

    req_username = getattr(req, "username", None)
    if req_username is not None:
        if not isinstance(req_username, str):
            raise PermissionError("需要 Dashboard 管理员登录")
        req_actor = req_username.strip()
        if not req_actor or req_actor.startswith("api_key:"):
            raise PermissionError("需要 Dashboard 管理员登录")
    else:
        req_actor = None

    try:
        from astrbot.dashboard.api.auth import require_dashboard_user
    except ImportError as exc:
        raise PermissionError("需要 Dashboard 管理员登录") from exc

    try:
        username = await require_dashboard_user(raw)
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError("需要 Dashboard 管理员登录") from exc

    if not isinstance(username, str):
        raise PermissionError("需要 Dashboard 管理员登录")
    actor = username.strip()
    if not actor or actor.startswith("api_key:"):
        raise PermissionError("需要 Dashboard 管理员登录")
    if req_actor is not None and req_actor != actor:
        raise PermissionError("需要 Dashboard 管理员登录")

    get_config = getattr(context, "get_config", None)
    if not callable(get_config):
        raise PermissionError("需要 Dashboard 管理员登录")
    try:
        cfg = get_config()
    except Exception as exc:
        raise PermissionError("需要 Dashboard 管理员登录") from exc
    dashboard_cfg = cfg.get("dashboard") if isinstance(cfg, dict) else None
    expected_raw = (
        dashboard_cfg.get("username") if isinstance(dashboard_cfg, dict) else None
    )
    if not isinstance(expected_raw, str) or not expected_raw.strip():
        raise PermissionError("需要 Dashboard 管理员登录")
    if actor != expected_raw.strip():
        raise PermissionError("需要 Dashboard 管理员登录")

    if check_csrf:
        verify_series_csrf(req)

    return actor


class SeriesPanels:
    def __init__(self, owner, identity):
        self.owner = owner
        self.identity = identity
        self.source_commit = _installed_commit(Path(__file__).resolve().parent)
        module = ".".join(filter(None, (__package__, "series_workspace")))
        self.workspace = import_module(module).PanelWorkspace(self)
        self.path = "/" + identity + "/series_presence"

    async def status(self):
        try:
            req = _resolve_web_request()
            await require_series_admin(self.owner.context, req, check_csrf=False)
            query = getattr(req, "query", None) or {}
            refresh_flag = str(query.get("refresh") or "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            if refresh_flag:
                invalidate_presence_cache(context=self.owner.context)
            ctx = self.owner.context
            epoch = get_presence_epoch(ctx)
            cache_key = (id(ctx), self.identity, epoch)
            now = time.monotonic()
            data = None
            if not refresh_flag and _PRESENCE_RESPONSE_TTL_SECONDS > 0:
                with _SUMMARY_CACHE_LOCK:
                    cached = _PRESENCE_RESPONSE_CACHE.get(cache_key)
                    if cached is not None and cached[0] > now:
                        data = cached[1]
            if data is None:
                data = await asyncio.to_thread(
                    presence, ctx, self.identity, force_refresh=refresh_flag
                )
                if _PRESENCE_RESPONSE_TTL_SECONDS > 0:
                    post_epoch = get_presence_epoch(ctx)
                    if post_epoch == epoch:
                        with _SUMMARY_CACHE_LOCK:
                            if len(_PRESENCE_RESPONSE_CACHE) > 32:
                                expired = [
                                    k
                                    for k, entry in _PRESENCE_RESPONSE_CACHE.items()
                                    if entry[0] <= now
                                ]
                                for k in expired:
                                    _PRESENCE_RESPONSE_CACHE.pop(k, None)
                                if len(_PRESENCE_RESPONSE_CACHE) > 32:
                                    _PRESENCE_RESPONSE_CACHE.clear()
                            _PRESENCE_RESPONSE_CACHE[(id(ctx), self.identity, post_epoch)] = (
                                time.monotonic() + _PRESENCE_RESPONSE_TTL_SECONDS,
                                data,
                            )
            return _web_json_response({"status": "ok", "data": data})
        except PermissionError:
            return _web_json_response(
                {"status": "error", "message": "需要 Dashboard 管理员登录"},
                status_code=403,
            )
        except Exception:
            return _web_json_response(
                {"status": "error", "message": "互通状态暂不可用"}, status_code=503
            )

    def close(self):
        self.workspace.close()
        registered = getattr(self.owner.context, "registered_web_apis", None)
        if isinstance(registered, list):
            registered[:] = [
                r
                for r in registered
                if not (
                    len(r) >= 2
                    and r[0] == self.path
                    and getattr(r[1], "__self__", None) is self
                )
            ]


def register_series_panels(owner, identity):
    _remember_context(getattr(owner, "context", None))
    old = getattr(owner, "_series_panels", None)
    if old:
        old.close()
    panel = SeriesPanels(owner, identity)
    owner._series_panels = panel
    owner.context.register_web_api(
        panel.path, panel.status, ["GET"], "群间系列插件状态"
    )
    panel.workspace.register()


# ---------------------------------------------------------------------------
# Wave 0: Unified 2GB Resource Profile, Memory Watermark & Heavy Subprocess Gate
# ---------------------------------------------------------------------------

RESOURCE_PROFILES: tuple[str, ...] = (
    "auto",
    "standard",
    "low_memory",
    "low_memory_aggressive",
)
_EXPLICIT_RESOURCE_PROFILES = frozenset(
    {"standard", "low_memory", "low_memory_aggressive"}
)
_RESOURCE_STATE_LOCK = threading.Lock()
_HOST_MEMORY_MB_CACHE: int | None = None
_WATERMARK_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
_WATERMARK_TTL_SECONDS = 3.0
_HYSTERESIS_STATE: dict[int, dict[str, Any]] = {}
_HYSTERESIS_HOLD_SECONDS = 1800.0
_HYSTERESIS_STREAK_THRESHOLD = 3
_LAST_MALLOC_TRIM_MONO = 0.0
_MALLOC_TRIM_LOCK = threading.Lock()
_DEFAULT_HEAVY_GATE: HeavySubprocessGate | None = None


def _parse_positive_int(raw: str) -> int:
    text = str(raw or "").strip().lower()
    if not text or text == "max":
        return 0
    try:
        val = int(text)
    except ValueError:
        return 0
    if val <= 0 or val >= (1 << 50):
        return 0
    return val


def detect_host_memory_mb(*, force_refresh: bool = False) -> int:
    """Detect effective host/container memory ceiling in MiB (cached once per process)."""
    global _HOST_MEMORY_MB_CACHE
    override = os.environ.get("ASTRBOT_RESOURCE_MEMORY_MB", "").strip()
    if override:
        parsed = _parse_positive_int(override)
        if parsed > 0:
            return parsed
    if not force_refresh and _HOST_MEMORY_MB_CACHE is not None:
        return _HOST_MEMORY_MB_CACHE

    candidates_mb: list[int] = []
    # Linux cgroup v2 & v1 container limits
    for cgroup_path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            if os.path.isfile(cgroup_path):
                with open(cgroup_path, encoding="utf-8") as fh:
                    raw_bytes = _parse_positive_int(fh.read())
                if raw_bytes >= 64 * 1024 * 1024:
                    candidates_mb.append(int(raw_bytes // (1024 * 1024)))
        except OSError:
            pass

    # Linux /proc/meminfo MemTotal
    try:
        if os.path.isfile("/proc/meminfo"):
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            candidates_mb.append(int(int(parts[1]) // 1024))
                        break
    except OSError:
        pass

    # POSIX sysconf fallback
    if not candidates_mb and hasattr(os, "sysconf"):
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            phys_pages = int(os.sysconf("SC_PHYS_PAGES"))
            if page_size > 0 and phys_pages > 0:
                candidates_mb.append(int((page_size * phys_pages) // (1024 * 1024)))
        except (OSError, ValueError, AttributeError):
            pass

    detected = min(candidates_mb) if candidates_mb else 0
    _HOST_MEMORY_MB_CACHE = detected
    return detected


def apply_native_thread_caps(profile: str | None = None) -> dict[str, str]:
    """Cap OpenMP/OpenBLAS/MKL worker thread pools before native libs initialize."""
    eff = str(profile or "").strip().lower()
    if eff not in _EXPLICIT_RESOURCE_PROFILES:
        host_mb = detect_host_memory_mb()
        eff = "low_memory" if (0 < host_mb <= 2560) else "standard"
    target = "1" if eff != "standard" else "2"
    applied: dict[str, str] = {}
    for env_key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        os.environ.setdefault(env_key, target)
        applied[env_key] = os.environ[env_key]
    return applied


def read_process_memory_mb() -> dict[str, float]:
    """Read current process RSS/HWM in MiB without external dependencies."""
    override = os.environ.get("ASTRBOT_TEST_RSS_MB", "").strip()
    if override:
        try:
            val = max(0.0, float(override))
            return {"rss_mb": round(val, 1), "hwm_mb": round(val, 1)}
        except ValueError:
            pass

    rss_mb = 0.0
    hwm_mb = 0.0
    try:
        if os.path.isfile("/proc/self/status"):
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            rss_mb = int(parts[1]) / 1024.0
                    elif line.startswith("VmHWM:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            hwm_mb = int(parts[1]) / 1024.0
            if rss_mb > 0.0 or hwm_mb > 0.0:
                return {"rss_mb": round(rss_mb, 1), "hwm_mb": round(max(hwm_mb, rss_mb), 1)}
    except OSError:
        pass

    try:
        import resource as _resource

        maxrss = float(_resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss or 0.0)
        if maxrss > 0.0:
            # Linux reports KiB; macOS reports bytes.
            mb = maxrss / (1024.0 * 1024.0) if sys.platform == "darwin" else maxrss / 1024.0
            return {"rss_mb": round(mb, 1), "hwm_mb": round(mb, 1)}
    except Exception:
        pass
    return {"rss_mb": 0.0, "hwm_mb": 0.0}


def try_malloc_trim(*, min_interval_seconds: float = 30.0, force: bool = False) -> bool:
    """Throttled gc.collect() + glibc malloc_trim(0) to return freed heap pages on Linux."""
    global _LAST_MALLOC_TRIM_MONO
    now = time.monotonic()
    with _MALLOC_TRIM_LOCK:
        if not force and (now - _LAST_MALLOC_TRIM_MONO) < max(0.0, float(min_interval_seconds)):
            return False
        _LAST_MALLOC_TRIM_MONO = now
    try:
        gc.collect()
    except Exception:
        pass
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim_fn = getattr(libc, "malloc_trim", None)
        if callable(trim_fn):
            return bool(trim_fn(0))
    except Exception:
        pass
    return False


def _configured_resource_profile(context=None) -> tuple[str, int]:
    shared = get_series_shared_config(context) if context is not None else {}
    raw = str(shared.get("resource_profile") or "").strip().lower()
    if raw not in RESOURCE_PROFILES:
        env_raw = os.environ.get("ASTRBOT_RESOURCE_PROFILE", "").strip().lower()
        raw = env_raw if env_raw in RESOURCE_PROFILES else "auto"
    groups = shared.get("takeover_groups")
    group_count = len(groups) if isinstance(groups, list) else 0
    return raw, group_count


def _watermark_thresholds(profile: str) -> tuple[float, float]:
    if profile == "low_memory_aggressive":
        return 560.0, 700.0
    if profile == "low_memory":
        return 680.0, 820.0
    return 1800.0, 2400.0


def get_series_resource_state(
    context=None,
    *,
    active_groups: int | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Resolve effective resource profile (with auto hysteresis) and recommendations."""
    configured, shared_groups = _configured_resource_profile(context)
    groups_count = int(active_groups) if active_groups is not None else shared_groups
    host_mb = detect_host_memory_mb(force_refresh=force_refresh)
    mem = read_process_memory_mb()
    rss_mb = float(mem["rss_mb"])
    ctx_key = id(context) if context is not None else 0

    if configured in _EXPLICIT_RESOURCE_PROFILES:
        effective = configured
        reason = "explicit"
    elif host_mb <= 0 or host_mb > 2560:
        effective = "standard"
        reason = "host_memory_gt_2560mb" if host_mb > 2560 else "host_memory_unknown"
    elif host_mb <= 1536:
        effective = "low_memory_aggressive"
        reason = "host_memory_le_1536mb"
    else:
        now = time.monotonic()
        with _RESOURCE_STATE_LOCK:
            st = _HYSTERESIS_STATE.setdefault(
                ctx_key,
                {
                    "effective": "low_memory",
                    "high_streak": 0,
                    "low_streak": 0,
                    "hold_until": 0.0,
                    "reason": "host_memory_le_2560mb",
                },
            )
            is_high = groups_count >= 4 or rss_mb >= 680.0
            is_calm = groups_count < 4 and rss_mb < 580.0
            if is_high:
                st["high_streak"] = int(st.get("high_streak", 0)) + 1
                st["low_streak"] = 0
                if (
                    groups_count >= 6
                    or st["high_streak"] >= _HYSTERESIS_STREAK_THRESHOLD
                ):
                    st["effective"] = "low_memory_aggressive"
                    st["hold_until"] = now + _HYSTERESIS_HOLD_SECONDS
                    st["reason"] = (
                        "auto_hysteresis_rss"
                        if rss_mb >= 680.0
                        else "auto_hysteresis_multi_group"
                    )
            elif st.get("effective") == "low_memory_aggressive" and is_calm:
                st["high_streak"] = 0
                if now >= float(st.get("hold_until", 0.0)):
                    st["low_streak"] = int(st.get("low_streak", 0)) + 1
                    if st["low_streak"] >= _HYSTERESIS_STREAK_THRESHOLD:
                        st["effective"] = "low_memory"
                        st["low_streak"] = 0
                        st["reason"] = "auto_hysteresis_recovered"
            else:
                st["high_streak"] = 0
                st["low_streak"] = 0
            effective = str(st["effective"])
            reason = str(st["reason"])

    recommendations: list[dict[str, str]] = []
    if effective == "low_memory_aggressive":
        recommendations.append(
            {
                "code": "dynamics_memory_builtin_tokenizer",
                "plugin_id": "astrbot_plugin_dynamics_memory",
                "title": "建议将群间记忆分词器切换为 builtin（可省 ~55 MiB 常驻内存）",
                "detail": "切换为不可逆索引变更，需在群间记忆面板确认并执行 /lmem rebuild-index。",
            }
        )
        recommendations.append(
            {
                "code": "dynamics_memory_doc_atom_without_graph",
                "plugin_id": "astrbot_plugin_dynamics_memory",
                "title": "2G 多群激进模式建议保留 Doc+Atom 双向量、关闭重型 Graph 路",
                "detail": "可大幅释放 10 万级图谱边/表与 4 条读连接线程，同时保持高召回精度。",
            }
        )

    return {
        "configured": configured,
        "effective": effective,
        "reason": reason,
        "host_memory_mb": host_mb,
        "rss_mb": rss_mb,
        "hwm_mb": float(mem["hwm_mb"]),
        "active_groups": groups_count,
        "recommendations": recommendations,
    }


def resolve_series_resource_profile(
    context=None, *, active_groups: int | None = None
) -> str:
    """Return effective resource profile: standard | low_memory | low_memory_aggressive."""
    return str(
        get_series_resource_state(context, active_groups=active_groups)["effective"]
    )


def get_series_memory_watermark(
    context=None, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Return cached process memory watermark (ok | soft | hard)."""
    ctx_key = id(context) if context is not None else 0
    now = time.monotonic()
    if not force_refresh and not os.environ.get("ASTRBOT_TEST_RSS_MB"):
        with _RESOURCE_STATE_LOCK:
            cached = _WATERMARK_CACHE.get(ctx_key)
            if cached is not None and cached[0] > now:
                return dict(cached[1])

    state = get_series_resource_state(context, force_refresh=force_refresh)
    profile = str(state["effective"])
    soft_mb, hard_mb = _watermark_thresholds(profile)
    rss_mb = float(state["rss_mb"])
    if rss_mb >= hard_mb:
        level = "hard"
    elif rss_mb >= soft_mb:
        level = "soft"
    else:
        level = "ok"
    payload = {
        "profile": profile,
        "rss_mb": rss_mb,
        "hwm_mb": float(state["hwm_mb"]),
        "host_mb": int(state["host_memory_mb"]),
        "soft_mb": soft_mb,
        "hard_mb": hard_mb,
        "state": level,
    }
    with _RESOURCE_STATE_LOCK:
        if len(_WATERMARK_CACHE) > 32:
            _WATERMARK_CACHE.clear()
        _WATERMARK_CACHE[ctx_key] = (now + _WATERMARK_TTL_SECONDS, payload)
    return dict(payload)


_PROFILE_LIMITS: dict[str, dict[str, Any]] = {
    "standard": {
        "profile": "standard",
        "native_threads": 2,
        "heavy_subprocess_concurrency": 2,
        "sqlite_cache_kb": 2000,
        "sqlite_ro_cache_kb": 1000,
        "sqlite_temp_store": "FILE",
        "sqlite_mmap_size": 0,
        "sqlite_journal_limit_bytes": 16 * 1024 * 1024,
        "memory_read_pool_size": 2,
        "memory_executor_workers": 2,
        "memory_max_sessions": 60,
        "memory_search_cache_size": 32,
        "memory_rerank_candidates_local": 40,
        "memory_rerank_candidates_cloud": 20,
        "memory_rerank_cache_size": 128,
        "memory_faiss_debounce_s": 10.0,
        "memory_graph_max_nodes": 3000,
        "memory_graph_max_edges": 12000,
        "visual_lru_scale": 0.5,
        "visual_gif_cache_max_size": 20,
        "visual_gif_cache_max_bytes": 8 * 1024 * 1024,
        "visual_gif_max_frames": 24,
        "visual_gif_max_dim": 720,
        "visual_sticker_max_dim": 2048,
        "visual_sticker_subprocess": False,
        "visual_embedding_query_cache": 128,
        "visual_max_image_bytes": 20 * 1024 * 1024,
        "learning_social_max_groups": 32,
        "learning_social_top_limit": 1000,
        "learning_social_refresh_s": 60.0,
        "learning_expression_sample_limit": 4000,
        "learning_relation_max_recent": 80,
        "learning_events_retention_days": 7,
        "learning_profile_retention_days": 30,
        "learning_audit_retention_days": 90,
        "learning_directed_retention_days": 60,
        "chat_max_sessions": 256,
        "chat_dag_max_nodes": 350,
        "chat_max_bundles": 64,
        "chat_search_workers": 4,
        "chat_persist_interval_s": 60.0,
        "chat_persist_max_bytes": 3 * 1024 * 1024,
        "chat_max_archive_rows": 15000,
        "chat_archive_retention_days": 14,
        "chat_exact_memo_max_bytes": 3 * 1024 * 1024,
        "world_postcard_idle_timeout_s": 180.0,
        "world_prefer_3d": True,
        "world_loop_idle_s": 60.0,
        "world_snapshot_workers": 2,
        "world_retention_days": 60,
        "world_vacuum_pages": 512,
        "horizons_ffmpeg_threads": 2,
        "horizons_checkpoint_max": 4,
        "horizons_ebook_cache_max": 6,
        "horizons_ebook_max_mb": 20,
        "horizons_prefer_videoshot": False,
        "horizons_maintenance_interval_s": 60.0,
        "chronicle_pool_size": 4,
        "chronicle_playwright_fallback": True,
        "games_steam_notify_image": True,
        "games_steam_poll_s": 60,
        "games_steam_achievement_cache": 64,
        "provider_capacity": 4,
        "provider_bg_capacity": 2,
        "provider_max_queue": 128,
        "memory_soft_mb": 1800.0,
        "memory_hard_mb": 2400.0,
    },
    "low_memory": {
        "profile": "low_memory",
        "native_threads": 1,
        "heavy_subprocess_concurrency": 1,
        "sqlite_cache_kb": 1024,
        "sqlite_ro_cache_kb": 512,
        "sqlite_temp_store": "FILE",
        "sqlite_mmap_size": 0,
        "sqlite_journal_limit_bytes": 8 * 1024 * 1024,
        "memory_read_pool_size": 2,
        "memory_executor_workers": 2,
        "memory_max_sessions": 30,
        "memory_search_cache_size": 32,
        "memory_rerank_candidates_local": 20,
        "memory_rerank_candidates_cloud": 20,
        "memory_rerank_cache_size": 48,
        "memory_faiss_debounce_s": 20.0,
        "memory_graph_max_nodes": 1500,
        "memory_graph_max_edges": 5000,
        "visual_lru_scale": 0.125,
        "visual_gif_cache_max_size": 10,
        "visual_gif_cache_max_bytes": 4 * 1024 * 1024,
        "visual_gif_max_frames": 16,
        "visual_gif_max_dim": 512,
        "visual_sticker_max_dim": 1600,
        "visual_sticker_subprocess": False,
        "visual_embedding_query_cache": 64,
        "visual_max_image_bytes": 16 * 1024 * 1024,
        "learning_social_max_groups": 16,
        "learning_social_top_limit": 500,
        "learning_social_refresh_s": 120.0,
        "learning_expression_sample_limit": 2000,
        "learning_relation_max_recent": 50,
        "learning_events_retention_days": 5,
        "learning_profile_retention_days": 14,
        "learning_audit_retention_days": 30,
        "learning_directed_retention_days": 30,
        "chat_max_sessions": 128,
        "chat_dag_max_nodes": 200,
        "chat_max_bundles": 32,
        "chat_search_workers": 2,
        "chat_persist_interval_s": 120.0,
        "chat_persist_max_bytes": 1536 * 1024,
        "chat_max_archive_rows": 5000,
        "chat_archive_retention_days": 7,
        "chat_exact_memo_max_bytes": 1536 * 1024,
        "world_postcard_idle_timeout_s": 0.0,
        "world_prefer_3d": True,
        "world_loop_idle_s": 180.0,
        "world_snapshot_workers": 1,
        "world_retention_days": 30,
        "world_vacuum_pages": 512,
        "horizons_ffmpeg_threads": 1,
        "horizons_checkpoint_max": 2,
        "horizons_ebook_cache_max": 3,
        "horizons_ebook_max_mb": 12,
        "horizons_prefer_videoshot": False,
        "horizons_maintenance_interval_s": 60.0,
        "chronicle_pool_size": 2,
        "chronicle_playwright_fallback": True,
        "games_steam_notify_image": True,
        "games_steam_poll_s": 60,
        "games_steam_achievement_cache": 32,
        "provider_capacity": 3,
        "provider_bg_capacity": 1,
        "provider_max_queue": 48,
        "memory_soft_mb": 680.0,
        "memory_hard_mb": 820.0,
    },
    "low_memory_aggressive": {
        "profile": "low_memory_aggressive",
        "native_threads": 1,
        "heavy_subprocess_concurrency": 1,
        "sqlite_cache_kb": 512,
        "sqlite_ro_cache_kb": 256,
        "sqlite_temp_store": "FILE",
        "sqlite_mmap_size": 0,
        "sqlite_journal_limit_bytes": 4 * 1024 * 1024,
        "memory_read_pool_size": 1,
        "memory_executor_workers": 1,
        "memory_max_sessions": 16,
        "memory_search_cache_size": 16,
        "memory_rerank_candidates_local": 15,
        "memory_rerank_candidates_cloud": 15,
        "memory_rerank_cache_size": 24,
        "memory_faiss_debounce_s": 30.0,
        "memory_graph_max_nodes": 800,
        "memory_graph_max_edges": 2500,
        "visual_lru_scale": 0.0625,
        "visual_gif_cache_max_size": 6,
        "visual_gif_cache_max_bytes": 2 * 1024 * 1024,
        "visual_gif_max_frames": 12,
        "visual_gif_max_dim": 420,
        "visual_sticker_max_dim": 1280,
        "visual_sticker_subprocess": True,
        "visual_embedding_query_cache": 32,
        "visual_max_image_bytes": 8 * 1024 * 1024,
        "learning_social_max_groups": 4,
        "learning_social_top_limit": 200,
        "learning_social_refresh_s": 600.0,
        "learning_expression_sample_limit": 1000,
        "learning_relation_max_recent": 25,
        "learning_events_retention_days": 2,
        "learning_profile_retention_days": 7,
        "learning_audit_retention_days": 14,
        "learning_directed_retention_days": 14,
        "chat_max_sessions": 48,
        "chat_dag_max_nodes": 120,
        "chat_max_bundles": 8,
        "chat_search_workers": 1,
        "chat_persist_interval_s": 180.0,
        "chat_persist_max_bytes": 768 * 1024,
        "chat_max_archive_rows": 500,
        "chat_archive_retention_days": 2,
        "chat_exact_memo_max_bytes": 512 * 1024,
        "world_postcard_idle_timeout_s": 0.0,
        "world_prefer_3d": False,
        "world_loop_idle_s": 300.0,
        "world_snapshot_workers": 1,
        "world_retention_days": 14,
        "world_vacuum_pages": 1024,
        "horizons_ffmpeg_threads": 1,
        "horizons_checkpoint_max": 1,
        "horizons_ebook_cache_max": 1,
        "horizons_ebook_max_mb": 5,
        "horizons_prefer_videoshot": True,
        "horizons_maintenance_interval_s": 120.0,
        "chronicle_pool_size": 1,
        "chronicle_playwright_fallback": False,
        "games_steam_notify_image": False,
        "games_steam_poll_s": 120,
        "games_steam_achievement_cache": 16,
        "provider_capacity": 2,
        "provider_bg_capacity": 1,
        "provider_max_queue": 24,
        "memory_soft_mb": 560.0,
        "memory_hard_mb": 700.0,
    },
}


def get_series_resource_limits(
    context=None, *, profile: str | None = None
) -> dict[str, Any]:
    """Return standardized resource limit dictionary for the effective profile."""
    eff = (
        str(profile).strip().lower()
        if profile and str(profile).strip().lower() in _PROFILE_LIMITS
        else resolve_series_resource_profile(context)
    )
    return dict(_PROFILE_LIMITS.get(eff, _PROFILE_LIMITS["standard"]))


def apply_series_sqlite_pragmas(
    conn: Any,
    context=None,
    *,
    readonly: bool = False,
    journal_limit_bytes: int | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Apply safe 2GB-friendly SQLite memory/WAL PRAGMAs on a synchronous connection."""
    limits = get_series_resource_limits(context, profile=profile)
    cache_kb = int(
        limits["sqlite_ro_cache_kb"] if readonly else limits["sqlite_cache_kb"]
    )
    jlimit = (
        int(journal_limit_bytes)
        if journal_limit_bytes is not None
        else int(limits["sqlite_journal_limit_bytes"])
    )
    stmts = [
        f"PRAGMA cache_size = -{max(128, cache_kb)}",
        f"PRAGMA temp_store = {limits['sqlite_temp_store']}",
        f"PRAGMA mmap_size = {int(limits['sqlite_mmap_size'])}",
    ]
    if not readonly and jlimit > 0:
        stmts.append(f"PRAGMA journal_size_limit = {jlimit}")
    for sql in stmts:
        try:
            conn.execute(sql)
        except Exception:
            pass
    return limits


async def apply_series_aiosqlite_pragmas(
    conn: Any,
    context=None,
    *,
    readonly: bool = False,
    journal_limit_bytes: int | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Apply safe 2GB-friendly SQLite memory/WAL PRAGMAs on an aiosqlite connection."""
    limits = get_series_resource_limits(context, profile=profile)
    cache_kb = int(
        limits["sqlite_ro_cache_kb"] if readonly else limits["sqlite_cache_kb"]
    )
    jlimit = (
        int(journal_limit_bytes)
        if journal_limit_bytes is not None
        else int(limits["sqlite_journal_limit_bytes"])
    )
    stmts = [
        f"PRAGMA cache_size = -{max(128, cache_kb)}",
        f"PRAGMA temp_store = {limits['sqlite_temp_store']}",
        f"PRAGMA mmap_size = {int(limits['sqlite_mmap_size'])}",
    ]
    if not readonly and jlimit > 0:
        stmts.append(f"PRAGMA journal_size_limit = {jlimit}")
    for sql in stmts:
        try:
            cur = await conn.execute(sql)
            close_fn = getattr(cur, "close", None)
            if callable(close_fn):
                res = close_fn()
                if inspect.isawaitable(res):
                    await res
        except Exception:
            pass
    return limits


class HeavySubprocessGate:
    """Cross-plugin semaphore serializing Chromium, FFmpeg, VACUUM, and heavy image jobs."""

    def __init__(self, context=None) -> None:
        self._context_ref = None
        if context is not None:
            try:
                self._context_ref = weakref.ref(context)
            except TypeError:
                self._context_ref = None
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.Future[bool], dict[str, Any]]] = []
        self._active: list[dict[str, Any]] = []
        self._total_acquired = 0

    def _capacity(self) -> int:
        ctx = self._context_ref() if self._context_ref is not None else None
        limits = get_series_resource_limits(ctx)
        return max(1, int(limits.get("heavy_subprocess_concurrency", 1)))

    def _dispatch_locked(self) -> None:
        cap = self._capacity()
        while self._waiters and len(self._active) < cap:
            fut, entry = self._waiters.pop(0)
            if fut.done():
                continue
            entry["started_at"] = time.monotonic()
            self._active.append(entry)
            self._total_acquired += 1
            fut.set_result(True)

    @asynccontextmanager
    async def slot(
        self,
        purpose: str,
        *,
        plugin_id: str = "",
        timeout: float | None = None,
    ):
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] | None = None
        entry: dict[str, Any] = {
            "purpose": str(purpose or "heavy"),
            "plugin_id": str(plugin_id or ""),
            "started_at": time.monotonic(),
        }
        with self._lock:
            cap = self._capacity()
            if len(self._active) < cap and not self._waiters:
                self._active.append(entry)
                self._total_acquired += 1
            else:
                fut = loop.create_future()
                self._waiters.append((fut, entry))
        if fut is not None:
            try:
                if timeout is not None:
                    await asyncio.wait_for(fut, timeout=max(0.05, float(timeout)))
                else:
                    await fut
            except BaseException:
                with self._lock:
                    self._waiters = [w for w in self._waiters if w[0] is not fut]
                    if entry in self._active:
                        self._active.remove(entry)
                        self._dispatch_locked()
                raise
        try:
            yield entry
        finally:
            with self._lock:
                if entry in self._active:
                    self._active.remove(entry)
                self._dispatch_locked()
            ctx = self._context_ref() if self._context_ref is not None else None
            if resolve_series_resource_profile(ctx) != "standard":
                try_malloc_trim(min_interval_seconds=15.0)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            active = [
                {
                    "purpose": item["purpose"],
                    "plugin_id": item["plugin_id"],
                    "elapsed_s": round(max(0.0, now - float(item["started_at"])), 1),
                }
                for item in self._active
            ]
            queued = sum(1 for f, _e in self._waiters if not f.done())
            total = self._total_acquired
        return {
            "capacity": self._capacity(),
            "active_count": len(active),
            "queued_count": queued,
            "total_acquired": total,
            "active": active,
        }


def get_heavy_subprocess_gate(context=None) -> HeavySubprocessGate:
    """Return the shared HeavySubprocessGate bound to context (or module fallback)."""
    global _DEFAULT_HEAVY_GATE
    ctx = context
    if ctx is None and _LAST_CONTEXT_REF is not None:
        ctx = _LAST_CONTEXT_REF()
    if ctx is not None:
        existing = getattr(ctx, "_chat_dynamics_heavy_subprocess_gate", None)
        if existing is not None and callable(getattr(existing, "slot", None)):
            return existing
        gate = HeavySubprocessGate(ctx)
        try:
            setattr(ctx, "_chat_dynamics_heavy_subprocess_gate", gate)
            return gate
        except Exception:
            pass
    if _DEFAULT_HEAVY_GATE is None:
        _DEFAULT_HEAVY_GATE = HeavySubprocessGate(None)
    return _DEFAULT_HEAVY_GATE


@asynccontextmanager
async def heavy_subprocess_slot(
    context,
    purpose: str,
    *,
    plugin_id: str = "",
    timeout: float | None = None,
):
    """Acquire the shared cross-series heavy subprocess gate (Chromium/FFmpeg/VACUUM/etc.)."""
    gate = get_heavy_subprocess_gate(context)
    async with gate.slot(purpose, plugin_id=plugin_id, timeout=timeout) as entry:
        yield entry


def get_heavy_subprocess_snapshot(context=None) -> dict[str, Any]:
    """Return live diagnostics for the cross-series heavy subprocess gate."""
    gate = get_heavy_subprocess_gate(context)
    snap_fn = getattr(gate, "snapshot", None)
    if callable(snap_fn):
        try:
            return snap_fn()
        except Exception:
            pass
    return {"capacity": 1, "active_count": 0, "queued_count": 0, "total_acquired": 0, "active": []}


try:
    apply_native_thread_caps()
except Exception:
    pass
