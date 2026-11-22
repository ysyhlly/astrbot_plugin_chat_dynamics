"""Read-only companion presence and series defaults. Panels can also cooperate inside the stock view."""

from __future__ import annotations

import inspect
import math
import re
import subprocess
import sys
import threading
import time
from datetime import timedelta, timezone
from importlib import import_module
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api.web import json_response, request

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

# These four IDs and titles mirror Dynamics Games' runtime.LEGACY_PLUGINS.
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
)
LEGACY_GAME_IDS = frozenset(
    identity
    for identity, _label, owner in LEGACY_CONFLICTS
    if owner == "astrbot_plugin_dynamics_games"
)

CHAT_DYNAMICS_ID = "astrbot_plugin_chat_dynamics"
_SHARED_BOOL_KEYS = ("takeover_all_groups",)
_SHARED_LIST_KEYS = ("takeover_groups", "exclude_groups", "bot_names")
_SHARED_STR_KEYS = (
    "reply_provider",
    "decision_provider",
    "persona_id",
    "rhythm_timezone",
    "ui_language",
)
_SUMMARY_TTL_SECONDS = 10.0
_VALID_BADGE_TONES = frozenset({"accent", "warm", "info", "danger", "muted"})
_SUMMARY_CACHE_LOCK = threading.Lock()
_SUMMARY_CACHE: dict[tuple[str, int], tuple[float, dict[str, object] | None]] = {}


def invalidate_presence_cache(plugin_id: str | None = None) -> None:
    """Drop cached get_series_summary() entries for one plugin or all plugins."""
    with _SUMMARY_CACHE_LOCK:
        if plugin_id is None:
            _SUMMARY_CACHE.clear()
        else:
            for key in [k for k in _SUMMARY_CACHE if k[0] == plugin_id]:
                _SUMMARY_CACHE.pop(key, None)


invalidate_summary_cache = invalidate_presence_cache
clear_summary_cache = invalidate_presence_cache
invalidate_series_summary_cache = invalidate_presence_cache


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
) -> dict[str, object] | None:
    if owner is None:
        return None
    getter = getattr(owner, "get_series_summary", None)
    if not callable(getter) or inspect.iscoroutinefunction(getter):
        return None
    now = time.monotonic()
    cache_key = (identity, id(owner))
    if not force_refresh and ttl > 0:
        with _SUMMARY_CACHE_LOCK:
            cached = _SUMMARY_CACHE.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]
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
                expired = [k for k, (exp, _val) in _SUMMARY_CACHE.items() if exp <= now]
                for k in expired:
                    _SUMMARY_CACHE.pop(k, None)
                if len(_SUMMARY_CACHE) > 64:
                    _SUMMARY_CACHE.clear()
            _SUMMARY_CACHE[cache_key] = (now + ttl, summary)
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
    return str(defaults.get("reply_provider") or "")


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
    ) -> None:
        super().__init__(message)
        self.provider = str(provider or "")
        self.purpose = str(purpose or "")
        self.reason = str(reason or "busy")


def is_provider_budget_busy_error(exc: BaseException | None) -> bool:
    """Identify a ProviderBudgetBusyError even across plugin module boundaries."""
    if exc is None:
        return False
    return bool(
        isinstance(exc, ProviderBudgetBusyError)
        or getattr(exc, "is_provider_budget_busy", False)
        or type(exc).__name__ == "ProviderBudgetBusyError"
    )


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
) -> bool:
    """Report one LLM call's usage & timing to chat_dynamics telemetry if present."""
    if context is None:
        return False
    telemetry = getattr(context, "_chat_dynamics_series_telemetry", None)
    if telemetry is None:
        budget = getattr(context, "_chat_dynamics_provider_budget", None)
        telemetry = getattr(budget, "telemetry", None) if budget is not None else None
    recorder = getattr(telemetry, "record", None) if telemetry is not None else None
    if not callable(recorder):
        return False
    try:
        recorder(
            plugin_id=str(plugin_id or "unknown").strip() or "unknown",
            provider_id=str(provider_id or "default").strip() or "default",
            purpose=str(purpose or "background").strip() or "background",
            response=response,
            queue_seconds=max(0.0, float(queue_seconds or 0.0)),
            generation_seconds=max(0.0, float(generation_seconds or 0.0)),
            ok=bool(ok),
            error_type=str(error_type or "").strip()[:64],
        )
        return True
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
):
    """Run an LLM call via shared ProviderBudget when present, else directly.

    Translates cross-plugin ``ProviderBudgetBusyError`` into this module's
    ``ProviderBudgetBusyError`` so companion plugins can catch it without
    importing ``astrbot_plugin_chat_dynamics``.
    """
    clean_provider = str(provider_id or "").strip() or "default"
    clean_plugin = str(plugin_id or "").strip() or "unknown"
    clean_purpose = str(purpose or "").strip() or "background"
    budget = (
        getattr(context, "_chat_dynamics_provider_budget", None)
        if context is not None
        else None
    )
    runner = getattr(budget, "run", None) if budget is not None else None
    if callable(runner) and inspect.iscoroutinefunction(runner):
        if getattr(budget, "supports_series_telemetry", False) is True:
            try:
                return await runner(
                    clean_provider,
                    clean_purpose,
                    invoke,
                    plugin_id=clean_plugin,
                    acquire_timeout=acquire_timeout,
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
                )
                if isinstance(exc, ProviderBudgetBusyError):
                    raise
                raise ProviderBudgetBusyError(
                    str(exc),
                    provider=getattr(exc, "provider", clean_provider),
                    purpose=getattr(exc, "purpose", clean_purpose),
                    reason=getattr(exc, "reason", "busy"),
                ) from exc
            record_series_llm_usage(
                context,
                plugin_id=clean_plugin,
                provider_id=clean_provider,
                purpose=clean_purpose,
                generation_seconds=elapsed,
                ok=False,
                error_type=type(exc).__name__,
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
    )
    return result


def presence(context, current, root=None, *, force_refresh: bool = False):
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
                    identity, owner, force_refresh=force_refresh
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
        {"id": identity, "label": label, "owner": owner}
        for identity, label, owner in LEGACY_CONFLICTS
        if identity in active_ids and owner in active_ids
    ]

    return {
        "schema_version": 1,
        "current": current,
        "plugins": output,
        "legacy_conflict": bool(conflicts),
        "legacy_conflicts": conflicts,
        "series_defaults": get_series_defaults(context),
    }


def _installed_commit(root: Path) -> str:
    try:
        metadata = (root / "metadata.yaml").read_text(encoding="utf-8-sig")
        match = re.search(
            r"^# source-commit: ([0-9a-f]{40}|[0-9a-f]{64})$", metadata, re.MULTILINE
        )
        if match:
            return match.group(1)[:12]
        if (root / ".git").exists():
            revision = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=2,
                text=True,
            ).strip()
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
                return revision[:12]
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    return ""


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
            from astrbot.dashboard.api.auth import require_dashboard_user

            raw = getattr(request, "_request", None)
            if raw is None:
                raise PermissionError()
            username = await require_dashboard_user(raw)
            expected = (
                self.owner.context.get_config().get("dashboard", {}).get("username")
            )
            if not username or expected and username != expected:
                raise PermissionError()
            query = getattr(request, "query", None) or {}
            refresh_flag = str(query.get("refresh") or "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            if refresh_flag:
                invalidate_presence_cache()
            return json_response(
                {
                    "status": "ok",
                    "data": presence(
                        self.owner.context, self.identity, force_refresh=refresh_flag
                    ),
                }
            )
        except PermissionError:
            return json_response(
                {"status": "error", "message": "需要 Dashboard 管理员登录"},
                status_code=403,
            )
        except Exception:
            return json_response(
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
    old = getattr(owner, "_series_panels", None)
    if old:
        old.close()
    panel = SeriesPanels(owner, identity)
    owner._series_panels = panel
    owner.context.register_web_api(
        panel.path, panel.status, ["GET"], "群间系列插件状态"
    )
    panel.workspace.register()
