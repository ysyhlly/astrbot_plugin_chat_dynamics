"""Cross-plugin hub aggregators and v2.0 series hooks for Chat Dynamics."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import copy
import hashlib
import inspect
import json
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .. import series_panels
from .dashboard import collect_session_ids

CHAT_DYNAMICS_ID = "astrbot_plugin_chat_dynamics"

CORE_SERIES_PLUGINS: tuple[tuple[str, str, str, str], ...] = (
    ("astrbot_plugin_chat_dynamics", "对话主控", "CD", "app"),
    ("astrbot_plugin_dynamics_world", "群间世界", "DW", "world"),
    ("astrbot_plugin_dynamics_learning", "群间学习", "DL", "review"),
    ("astrbot_plugin_dynamics_memory", "群间记忆", "DM", "dashboard"),
    ("astrbot_plugin_dynamics_horizons", "群间见闻", "DH", "horizons"),
    ("astrbot_plugin_dynamics_games", "群间游戏", "DG", "games"),
    ("astrbot_plugin_visual_media", "视觉工坊", "VM", "workshop"),
    ("astrbot_plugin_group_chronicle", "群文化沿革", "GC", "culture"),
)
_CORE_PLUGIN_META: dict[str, tuple[str, str, str]] = {
    pid: (label, mark, page) for pid, label, mark, page in CORE_SERIES_PLUGINS
}

_MAX_QUERY_LEN = 128
_MAX_SCOPE_LEN = 256
_MAX_SEARCH_LIMIT = 50
_PER_PLUGIN_STATUS_TIMEOUT = 0.25
_SEARCH_EXECUTOR_WORKERS = 8
_MATRIX_CACHE_TTL_SECONDS = 3.0
_MATRIX_CACHE_MAX_ENTRIES = 64

_SEARCH_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_SEARCH_EXECUTOR_LOCK = threading.Lock()
_SYNC_SEARCH_GUARD_LOCK = threading.Lock()
_SYNC_SEARCH_INFLIGHT: dict[tuple[int, str], int] = {}
_SYNC_SEARCH_COOLDOWN_UNTIL: dict[tuple[int, str], float] = {}
_SYNC_SEARCH_MAX_INFLIGHT_PER_PLUGIN = 1
_SYNC_SEARCH_TIMEOUT_COOLDOWN_SECONDS = 5.0
_SEARCH_MAX_CONCURRENT_FANOUTS = 4
_SEARCH_SEMAPHORES: dict[int, asyncio.Semaphore] = {}
_MATRIX_CACHE_LOCK = threading.Lock()
_MATRIX_CACHE: dict[tuple[int, str, str, str, int], tuple[float, dict[str, Any]]] = {}
_PRESENCE_SENTINEL_KEY = ("__series_matrix_cache_sentinel__", 0)


def invalidate_series_matrix_cache(plugin: Any = None) -> None:
    """Drop cached build_series_matrix() snapshots for one plugin or all plugins."""
    with _MATRIX_CACHE_LOCK:
        if plugin is None:
            _MATRIX_CACHE.clear()
        else:
            pid = id(plugin)
            for key in [k for k in _MATRIX_CACHE if k[0] == pid]:
                _MATRIX_CACHE.pop(key, None)


def _install_presence_invalidation_hook() -> None:
    """Keep the 3s matrix cache coherent whenever series_panels.invalidate_presence_cache() runs."""
    orig = getattr(series_panels, "invalidate_presence_cache", None)
    if not callable(orig) or getattr(orig, "_series_matrix_hooked", False):
        return

    def _wrapped(plugin_id: str | None = None, *, context=None) -> None:
        try:
            orig(plugin_id, context=context)
        except TypeError:
            orig(plugin_id)
        invalidate_series_matrix_cache()

    _wrapped._series_matrix_hooked = True  # type: ignore[attr-defined]
    series_panels.invalidate_presence_cache = _wrapped  # type: ignore[assignment]


install_presence_invalidation_hook = _install_presence_invalidation_hook
_install_presence_invalidation_hook()


def _presence_sentinel_alive() -> bool:
    cache = getattr(series_panels, "_SUMMARY_CACHE", None)
    lock = getattr(series_panels, "_SUMMARY_CACHE_LOCK", None)
    if not isinstance(cache, dict):
        return True
    if lock is not None:
        with lock:
            return _PRESENCE_SENTINEL_KEY in cache
    return _PRESENCE_SENTINEL_KEY in cache


def _mark_presence_sentinel() -> None:
    cache = getattr(series_panels, "_SUMMARY_CACHE", None)
    lock = getattr(series_panels, "_SUMMARY_CACHE_LOCK", None)
    if not isinstance(cache, dict):
        return
    if lock is not None:
        with lock:
            cache[_PRESENCE_SENTINEL_KEY] = (float("inf"), None)
    else:
        cache[_PRESENCE_SENTINEL_KEY] = (float("inf"), None)


def _get_search_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _SEARCH_EXECUTOR
    if _SEARCH_EXECUTOR is None:
        with _SEARCH_EXECUTOR_LOCK:
            if _SEARCH_EXECUTOR is None:
                _SEARCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_SEARCH_EXECUTOR_WORKERS,
                    thread_name_prefix="cd-series-search",
                )
                atexit.register(_shutdown_search_executor)
    return _SEARCH_EXECUTOR


def _shutdown_search_executor() -> None:
    global _SEARCH_EXECUTOR
    executor = _SEARCH_EXECUTOR
    _SEARCH_EXECUTOR = None
    if executor is not None:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _extract_group_id(plugin: Any, umo: str) -> str:
    clean = str(umo or "").strip()
    if not clean:
        return ""
    sessions = getattr(plugin, "_sessions", None) or {}
    runtime = sessions.get(clean)
    if runtime is not None and getattr(runtime, "group_id", None):
        return str(runtime.group_id).strip()
    if ":" in clean:
        tail = clean.rsplit(":", 1)[-1].strip()
        if tail:
            return tail
    return clean


def _format_group_label(plugin: Any, umo: str) -> str:
    clean = str(umo or "").strip()
    if not clean:
        return "未知群聊"
    sessions = getattr(plugin, "_sessions", None) or {}
    runtime = sessions.get(clean)
    if runtime is not None:
        for attr in ("group_name", "display_name", "title"):
            val = getattr(runtime, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()[:80]
    group_id = _extract_group_id(plugin, clean)
    if group_id and group_id != clean:
        return f"群 {group_id}"
    return clean[:80]


def _default_persona_id(plugin: Any) -> str:
    getter = getattr(plugin, "get_series_shared_config", None)
    if callable(getter):
        try:
            shared = getter()
            if isinstance(shared, dict):
                return str(shared.get("persona_id") or "").strip()[:128]
        except Exception:
            pass
    return ""


def _takeover_state_for_umo(plugin: Any, umo: str) -> tuple[bool, str]:
    clean = str(umo or "").strip()
    group_id = _extract_group_id(plugin, clean)
    if not bool(getattr(plugin, "enabled", True)):
        return False, group_id
    exclude_set = {
        str(x).strip() for x in (getattr(plugin, "exclude_groups", ()) or ()) if str(x).strip()
    }
    if (clean and clean in exclude_set) or (group_id and group_id in exclude_set):
        return False, group_id
    checker = getattr(plugin, "is_group_takeover_enabled", None)
    if callable(checker):
        if bool(getattr(plugin, "takeover_all", False)):
            enabled = bool(checker(group_id or clean))
        else:
            enabled = bool(checker(group_id) or (clean != group_id and checker(clean)))
    else:
        enabled = bool(
            getattr(plugin, "takeover_all", False)
            or group_id in (getattr(plugin, "takeover_groups", ()) or ())
            or clean in (getattr(plugin, "takeover_groups", ()) or ())
        )
    return enabled, group_id


def chat_dynamics_scope_version(plugin: Any) -> str:
    """Return a deterministic optimistic-lock token for Chat Dynamics' scope config."""
    takeover_all = bool(getattr(plugin, "takeover_all", False))
    enabled = bool(getattr(plugin, "enabled", True))
    takeover_groups = sorted(
        str(x).strip() for x in (getattr(plugin, "takeover_groups", ()) or ()) if str(x).strip()
    )
    exclude_groups = sorted(
        str(x).strip() for x in (getattr(plugin, "exclude_groups", ()) or ()) if str(x).strip()
    )
    payload = json.dumps(
        {
            "enabled": enabled,
            "takeover_all": takeover_all,
            "takeover_groups": takeover_groups,
            "exclude_groups": exclude_groups,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def chat_dynamics_series_summary(plugin: Any) -> dict[str, Any]:
    """Synchronous, read-only summary hook for series_presence v2."""
    enabled = bool(getattr(plugin, "enabled", True)) and not bool(
        getattr(plugin, "_shutting_down", False)
    )
    shadow_mode = bool(getattr(plugin, "shadow_mode", False))
    try:
        session_ids = collect_session_ids(plugin)
    except Exception:
        session_ids = []
    live_count = len(session_ids)
    cooling_count = 0
    arbiter = getattr(plugin, "arbiter", None)
    time_service = getattr(plugin, "time_service", None)
    now = None
    if time_service is not None and callable(getattr(time_service, "time", None)):
        try:
            now = time_service.time()
        except Exception:
            now = None
    if arbiter is not None and callable(getattr(arbiter, "cooling_remaining", None)):
        for sid in session_ids:
            try:
                if arbiter.cooling_remaining(sid, current_time=now) > 0.0:
                    cooling_count += 1
            except Exception:
                continue

    if not enabled:
        badge = {"count": 0, "tone": "muted", "label": "已停用"}
        headline = "对话主控当前已停用"
    elif cooling_count > 0:
        badge = {
            "count": live_count,
            "tone": "warm",
            "label": f"{live_count} 活跃 · {cooling_count} 冷却",
        }
        headline = f"{'观察模式' if shadow_mode else '正常运行'} · {live_count} 活跃群（{cooling_count} 冷却中）"
    elif live_count > 0:
        badge = {"count": live_count, "tone": "accent", "label": f"{live_count} 活跃群"}
        headline = f"{'观察模式' if shadow_mode else '正常运行'} · 观测 {live_count} 个活跃群聊"
    else:
        badge = {"count": 0, "tone": "muted", "label": "待命中"}
        headline = "观察模式 · 等待群聊消息" if shadow_mode else "正常运行 · 等待群聊消息"

    return {
        "badge": badge,
        "pulse": {
            "headline": headline,
            "updated_at": _now_iso(),
        },
    }


def _is_umo_excluded_by_core(plugin: Any, umo: str) -> bool:
    clean = str(umo or "").strip()
    group_id = _extract_group_id(plugin, clean)
    exclude_set = {
        str(x).strip()
        for x in (getattr(plugin, "exclude_groups", ()) or ())
        if str(x).strip()
    }
    if not exclude_set:
        cfg = getattr(plugin, "_runtime_config", None)
        exclude_set = {
            str(x).strip()
            for x in (getattr(cfg, "exclude_groups", ()) or ())
            if str(x).strip()
        }
    if not exclude_set:
        return False
    return bool((clean and clean in exclude_set) or (group_id and group_id in exclude_set))


def chat_dynamics_scope_status(plugin: Any, umo: str) -> dict[str, Any]:
    """Read-only per-UMO status hook for Chat Dynamics."""
    clean = str(umo or "").strip()
    takeover_enabled, group_id = _takeover_state_for_umo(plugin, clean)
    excluded_by_core = _is_umo_excluded_by_core(plugin, clean)
    cooling = False
    arbiter = getattr(plugin, "arbiter", None)
    time_service = getattr(plugin, "time_service", None)
    now = time_service.time() if time_service is not None and callable(getattr(time_service, "time", None)) else None
    if arbiter is not None and callable(getattr(arbiter, "cooling_remaining", None)):
        for candidate in (clean, group_id):
            if not candidate:
                continue
            try:
                if arbiter.cooling_remaining(candidate, current_time=now) > 0.0:
                    cooling = True
                    break
            except Exception:
                continue
    counts = getattr(plugin, "_vibe_msg_counts", None) or {}
    activity = int(counts.get(clean) or counts.get(group_id) or 0)
    if not bool(getattr(plugin, "enabled", True)):
        summary = "主控停用"
    elif excluded_by_core:
        summary = "主控排除"
    elif cooling:
        summary = "冷却中"
    elif takeover_enabled:
        summary = "开启"
    else:
        summary = "暂停"
    return {
        "enabled": takeover_enabled,
        "supported": True,
        "excluded_by_core": excluded_by_core,
        "persona_id": _default_persona_id(plugin),
        "activity_today": activity,
        "summary": summary,
        "version": chat_dynamics_scope_version(plugin),
    }


async def chat_dynamics_set_scope_enabled(
    plugin: Any,
    umo: str,
    enabled: bool,
    expected_version: str | int | None = None,
) -> dict[str, Any]:
    """Safely toggle takeover state for one UMO with optimistic locking."""
    clean = str(umo or "").strip()
    if not clean or len(clean) > _MAX_SCOPE_LEN:
        raise ValueError("invalid umo")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")

    current_version = chat_dynamics_scope_version(plugin)
    if expected_version is not None and str(expected_version) != current_version:
        return {
            "ok": False,
            "conflict": True,
            "version": current_version,
            "enabled": _takeover_state_for_umo(plugin, clean)[0],
            "summary": "配置版本已变化，请刷新后重试",
        }

    group_id = _extract_group_id(plugin, clean)
    candidates = {clean, group_id} - {""}
    takeover_all = bool(getattr(plugin, "takeover_all", False))
    takeover_groups = [
        str(x).strip() for x in (getattr(plugin, "takeover_groups", ()) or ()) if str(x).strip()
    ]
    exclude_groups = [
        str(x).strip() for x in (getattr(plugin, "exclude_groups", ()) or ()) if str(x).strip()
    ]

    if enabled:
        new_exclude = [x for x in exclude_groups if x not in candidates]
        new_takeover = list(takeover_groups)
        if not takeover_all and not any(x in candidates for x in new_takeover):
            # Match the existing style: if takeover_groups already stores full UMOs
            # with ':', store clean when it has ':', else store group_id so
            # is_group_takeover_enabled(group_id) matches cleanly.
            new_takeover.append(group_id or clean)
    else:
        new_takeover = [x for x in takeover_groups if x not in candidates]
        new_exclude = list(exclude_groups)
        if takeover_all and not any(x in candidates for x in new_exclude):
            new_exclude.append(group_id or clean)

    updates = {
        "takeover_groups": sorted(set(new_takeover)),
        "exclude_groups": sorted(set(new_exclude)),
    }
    saver = getattr(plugin, "save_config_values", None)
    if callable(saver):
        await saver(updates)
    else:
        plugin.takeover_groups = set(updates["takeover_groups"])
        plugin.exclude_groups = set(updates["exclude_groups"])
        if isinstance(getattr(plugin, "config", None), dict):
            plugin.config["takeover_groups"] = updates["takeover_groups"]
            plugin.config["exclude_groups"] = updates["exclude_groups"]

    series_panels.invalidate_presence_cache(CHAT_DYNAMICS_ID)
    invalidate_series_matrix_cache(plugin)
    new_version = chat_dynamics_scope_version(plugin)
    actual_enabled, _ = _takeover_state_for_umo(plugin, clean)
    excluded_now = _is_umo_excluded_by_core(plugin, clean)
    return {
        "ok": True,
        "enabled": actual_enabled,
        "version": new_version,
        "summary": "开启" if actual_enabled else ("主控排除" if excluded_now else "暂停"),
    }


def chat_dynamics_search_items(
    plugin: Any,
    query: str,
    scope: str = "",
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Fast read-only search over Chat Dynamics sessions, groups, and notebook titles."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    cap = max(1, min(int(limit or 8), 25))
    clean_scope = str(scope or "").strip()
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    umos = _collect_matrix_umos(plugin, scope_filter=clean_scope)
    for umo in umos:
        if len(results) >= cap:
            break
        label = _format_group_label(plugin, umo)
        group_id = _extract_group_id(plugin, umo)
        haystack = f"{label} {umo} {group_id}".lower()
        if q in haystack:
            key = ("group", umo)
            if key not in seen:
                seen.add(key)
                status = chat_dynamics_scope_status(plugin, umo)
                results.append(
                    {
                        "kind": "group",
                        "title": label,
                        "subtitle": f"对话主控 · {status['summary']} · {umo}",
                        "plugin_id": CHAT_DYNAMICS_ID,
                        "page": "app",
                        "deep_link": {"section": "console", "scope": umo},
                    }
                )

    # Optionally search group notebook non-private metadata (anniversaries/slang)
    store = getattr(plugin, "group_memory", None)
    if store is not None and callable(getattr(store, "list_all", None)) and len(results) < cap:
        search_umos = [clean_scope] if clean_scope else umos[:12]
        for umo in search_umos:
            if len(results) >= cap:
                break
            try:
                nb = store.list_all(umo) or {}
            except Exception:
                continue
            for item in nb.get("anniversaries") or []:
                if len(results) >= cap:
                    break
                title = str(item.get("title") or "").strip()
                if title and q in title.lower():
                    key = ("anniversary", f"{umo}:{item.get('id', title)}")
                    if key not in seen:
                        seen.add(key)
                        results.append(
                            {
                                "kind": "memory",
                                "title": f"纪念日：{title}",
                                "subtitle": f"{_format_group_label(plugin, umo)} · {item.get('month')}/{item.get('day')}",
                                "plugin_id": CHAT_DYNAMICS_ID,
                                "page": "app",
                                "deep_link": {"section": "memory", "scope": umo},
                            }
                        )
            for item in nb.get("slang_trials") or []:
                if len(results) >= cap:
                    break
                phrase = str(item.get("phrase") or "").strip()
                if phrase and q in phrase.lower():
                    key = ("slang", f"{umo}:{item.get('id', phrase)}")
                    if key not in seen:
                        seen.add(key)
                        results.append(
                            {
                                "kind": "culture",
                                "title": f"试用梗：{phrase}",
                                "subtitle": f"{_format_group_label(plugin, umo)} · 记忆小本",
                                "plugin_id": CHAT_DYNAMICS_ID,
                                "page": "app",
                                "deep_link": {"section": "memory", "scope": umo},
                            }
                        )
    return results[:cap]


def _collect_matrix_umos(plugin: Any, *, scope_filter: str = "") -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        clean = str(raw or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            found.append(clean)

    try:
        for sid in collect_session_ids(plugin):
            _add(sid)
    except Exception:
        pass
    for attr in ("takeover_groups", "exclude_groups"):
        vals = getattr(plugin, attr, None) or ()
        for item in sorted(str(x).strip() for x in vals if str(x).strip()):
            _add(item)
    cfg = getattr(plugin, "_runtime_config", None)
    if cfg is not None:
        for attr in ("takeover_groups", "exclude_groups"):
            vals = getattr(cfg, attr, None) or ()
            for item in sorted(str(x).strip() for x in vals if str(x).strip()):
                _add(item)

    if scope_filter:
        clean_filter = scope_filter.strip()
        matched = [u for u in found if u == clean_filter or _extract_group_id(plugin, u) == clean_filter]
        if matched:
            return matched
        return [clean_filter]
    return found


def _sanitize_scope_status(raw: Any, *, default_supported: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "enabled": None,
            "supported": default_supported,
            "persona_id": "",
            "activity_today": None,
            "summary": "只读",
            "version": "",
        }
    enabled_raw = raw.get("enabled")
    enabled = enabled_raw if isinstance(enabled_raw, bool) else None
    supported_raw = raw.get("supported")
    supported = supported_raw if isinstance(supported_raw, bool) else default_supported
    persona_id = str(raw.get("persona_id") or "").strip()[:128]
    act_raw = raw.get("activity_today")
    if isinstance(act_raw, bool) or act_raw is None:
        activity_today = None
    elif isinstance(act_raw, (int, float)) and math.isfinite(act_raw):
        activity_today = max(0, int(act_raw))
    else:
        activity_today = None
    summary = str(raw.get("summary") or "").strip()[:80]
    if not summary:
        summary = "开启" if enabled is True else "暂停" if enabled is False else "只读"
    ver_raw = raw.get("version")
    version = str(ver_raw).strip()[:64] if isinstance(ver_raw, (str, int)) and not isinstance(ver_raw, bool) else ""
    return {
        "enabled": enabled,
        "supported": supported,
        "persona_id": persona_id,
        "activity_today": activity_today,
        "summary": summary,
        "version": version,
    }


async def _invoke_hook(fn: Any, *args: Any, timeout: float = _PER_PLUGIN_STATUS_TIMEOUT) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await asyncio.wait_for(fn(*args), timeout=timeout)
    result = fn(*args)
    if inspect.isawaitable(result):
        return await asyncio.wait_for(result, timeout=timeout)
    return result


def _matrix_digest(groups: list[dict[str, Any]]) -> str:
    canonical = []
    for group in groups:
        cells = group.get("cells") or {}
        canonical.append(
            {
                "umo": group.get("umo", ""),
                "cells": {
                    pid: (
                        cell.get("enabled"),
                        cell.get("version", ""),
                        cell.get("state", ""),
                    )
                    for pid, cell in sorted(cells.items())
                },
            }
        )
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


async def _fetch_matrix_cell(
    plugin: Any,
    umo: str,
    pmeta: dict[str, Any],
    owner: Any,
    *,
    excluded_by_core: bool,
) -> tuple[str, dict[str, Any], str]:
    pid = pmeta["id"]
    state = pmeta["state"]
    base_cell: dict[str, Any] = {
        "enabled": None,
        "supported": False,
        "excluded_by_core": excluded_by_core,
        "state": state,
        "summary": (
            "未安装"
            if state == "missing"
            else "已停用"
            if state in {"disabled", "unloaded"}
            else "只读"
        ),
        "activity_today": None,
        "persona_id": "",
        "version": "",
        "can_open": pmeta["can_open"],
        "page": pmeta["page"],
    }
    cell_persona = ""
    if owner is not None and state not in {"missing", "disabled", "unloaded", "conflict"}:
        hook = getattr(owner, "get_series_scope_status", None)
        if callable(hook):
            try:
                raw = await _invoke_hook(hook, umo, timeout=_PER_PLUGIN_STATUS_TIMEOUT)
                sanitized = _sanitize_scope_status(
                    raw,
                    default_supported=callable(
                        getattr(owner, "set_series_scope_enabled", None)
                    ),
                )
                if not callable(getattr(owner, "set_series_scope_enabled", None)):
                    sanitized["supported"] = False
                base_cell.update(sanitized)
                cell_persona = sanitized["persona_id"]
            except Exception:
                base_cell["summary"] = "状态异常"
                base_cell["state"] = "error"
        elif pid == CHAT_DYNAMICS_ID:
            sanitized = _sanitize_scope_status(
                chat_dynamics_scope_status(plugin, umo),
                default_supported=True,
            )
            base_cell.update(sanitized)
            cell_persona = sanitized["persona_id"]

    if excluded_by_core and base_cell["state"] not in {
        "missing",
        "disabled",
        "unloaded",
        "conflict",
        "error",
    }:
        cur_summary = str(base_cell.get("summary") or "").strip()
        if "排除" not in cur_summary:
            if cur_summary in {"", "开启", "暂停", "只读", "运行中"}:
                base_cell["summary"] = "主控排除"
            else:
                base_cell["summary"] = f"{cur_summary} · 主控排除"[:80]

    return pid, base_cell, cell_persona


async def build_series_matrix(
    plugin: Any,
    *,
    scope: str = "",
    persona: str = "",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Aggregate all authorized/known groups across all 8 core series plugins concurrently."""
    clean_scope = str(scope or "").strip()
    clean_persona_filter = str(persona or "").strip().lower()
    scope_ver = chat_dynamics_scope_version(plugin)
    context = getattr(plugin, "context", None)
    epoch = (
        series_panels.get_presence_epoch(context)
        if callable(getattr(series_panels, "get_presence_epoch", None))
        else 0
    )
    cache_key = (id(plugin), clean_scope, clean_persona_filter, scope_ver, epoch)
    now_mono = time.monotonic()

    if not force_refresh and _MATRIX_CACHE_TTL_SECONDS > 0 and _presence_sentinel_alive():
        with _MATRIX_CACHE_LOCK:
            cached = _MATRIX_CACHE.get(cache_key)
            if cached is not None and cached[0] > now_mono:
                return copy.deepcopy(cached[1])

    pres = series_panels.presence(
        context, CHAT_DYNAMICS_ID, force_refresh=force_refresh
    )
    presence_by_id = {
        p["id"]: p for p in pres.get("plugins") or [] if isinstance(p, dict) and p.get("id")
    }

    owners: dict[str, Any] = {CHAT_DYNAMICS_ID: plugin}
    plugins_meta: list[dict[str, Any]] = []
    for pid, label, mark, page in CORE_SERIES_PLUGINS:
        pinfo = presence_by_id.get(pid) or {}
        owner = (
            plugin
            if pid == CHAT_DYNAMICS_ID
            else series_panels.find_plugin_owner(context, pid)
        )
        if owner is not None:
            owners[pid] = owner
        state = str(
            pinfo.get("state") or ("ready" if pid == CHAT_DYNAMICS_ID else "missing")
        )
        if pid == CHAT_DYNAMICS_ID and state in {"missing", "unloaded"}:
            state = "ready" if bool(getattr(plugin, "enabled", True)) else "inactive"
        supports_toggle = bool(
            owner is not None
            and callable(getattr(owner, "set_series_scope_enabled", None))
        )
        plugins_meta.append(
            {
                "id": pid,
                "label": label,
                "mark": mark,
                "page": page,
                "state": state,
                "can_open": bool(pinfo.get("can_open", pid == CHAT_DYNAMICS_ID)),
                "supports_toggle": supports_toggle,
                "badge": pinfo.get("badge"),
            }
        )

    umos = _collect_matrix_umos(plugin, scope_filter=clean_scope)
    default_persona = _default_persona_id(plugin)

    # Launch all (umo, plugin) cell fetches concurrently while preserving per-cell fault isolation.
    cell_tasks = []
    cell_coords: list[tuple[int, str]] = []
    excluded_flags = [_is_umo_excluded_by_core(plugin, umo) for umo in umos]
    for row_idx, umo in enumerate(umos):
        excluded = excluded_flags[row_idx]
        for pmeta in plugins_meta:
            cell_coords.append((row_idx, pmeta["id"]))
            cell_tasks.append(
                _fetch_matrix_cell(
                    plugin,
                    umo,
                    pmeta,
                    owners.get(pmeta["id"]),
                    excluded_by_core=excluded,
                )
            )

    cell_results = await asyncio.gather(*cell_tasks) if cell_tasks else []
    row_cells: list[dict[str, dict[str, Any]]] = [{} for _ in umos]
    row_personas: list[list[tuple[str, str]]] = [[] for _ in umos]
    for (row_idx, _pid), (pid, cell, cell_persona) in zip(cell_coords, cell_results):
        row_cells[row_idx][pid] = cell
        if cell_persona:
            row_personas[row_idx].append((pid, cell_persona))

    groups_out: list[dict[str, Any]] = []
    for row_idx, umo in enumerate(umos):
        cells = row_cells[row_idx]
        specific_sibling_persona = ""
        sibling_persona = ""
        chat_cell_persona = ""
        for pid, cell_persona in row_personas[row_idx]:
            if pid == CHAT_DYNAMICS_ID:
                chat_cell_persona = cell_persona
            else:
                if not sibling_persona:
                    sibling_persona = cell_persona
                if cell_persona != default_persona and not specific_sibling_persona:
                    specific_sibling_persona = cell_persona

        row_persona = (
            specific_sibling_persona
            or (
                chat_cell_persona
                if chat_cell_persona and chat_cell_persona != default_persona
                else ""
            )
            or sibling_persona
            or chat_cell_persona
            or default_persona
        )

        if clean_persona_filter and clean_persona_filter not in row_persona.lower():
            continue

        row_version = hashlib.sha256(
            json.dumps(
                {pid: (c["enabled"], c["version"]) for pid, c in sorted(cells.items())},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]

        groups_out.append(
            {
                "umo": umo,
                "group_id": _extract_group_id(plugin, umo),
                "label": _format_group_label(plugin, umo),
                "excluded_by_core": excluded_flags[row_idx],
                "persona_id": row_persona,
                "version": row_version,
                "cells": cells,
            }
        )

    matrix_version = _matrix_digest(groups_out)
    result = {
        "version": matrix_version,
        "updated_at": _now_iso(),
        "plugins": plugins_meta,
        "groups": groups_out,
    }
    if _MATRIX_CACHE_TTL_SECONDS > 0:
        with _MATRIX_CACHE_LOCK:
            now_store = time.monotonic()
            if len(_MATRIX_CACHE) >= _MATRIX_CACHE_MAX_ENTRIES:
                expired = [k for k, (exp, _v) in _MATRIX_CACHE.items() if exp <= now_store]
                for k in expired:
                    _MATRIX_CACHE.pop(k, None)
                if len(_MATRIX_CACHE) >= _MATRIX_CACHE_MAX_ENTRIES:
                    _MATRIX_CACHE.clear()
            _MATRIX_CACHE[cache_key] = (
                now_store + _MATRIX_CACHE_TTL_SECONDS,
                copy.deepcopy(result),
            )
        _mark_presence_sentinel()
    return result


async def toggle_series_matrix(
    plugin: Any,
    *,
    umo: str,
    plugin_id: str,
    enabled: bool,
    expected_version: Any,
) -> tuple[int, dict[str, Any]]:
    """Admin-only optimistic-locked toggle for one (umo, plugin_id) cell."""
    clean_umo = str(umo or "").strip()
    if not clean_umo or len(clean_umo) > _MAX_SCOPE_LEN:
        return 400, {"message": "umo required"}
    clean_pid = str(plugin_id or "").strip()
    if clean_pid not in _CORE_PLUGIN_META:
        return 400, {"message": "unsupported plugin_id"}
    if not isinstance(enabled, bool):
        return 400, {"message": "enabled must be a boolean"}
    if expected_version is None or isinstance(expected_version, bool) or not str(expected_version).strip():
        return 400, {"message": "expected_version required"}
    expected_str = str(expected_version).strip()

    snapshot = await build_series_matrix(plugin, scope=clean_umo, force_refresh=True)
    current_matrix_version = snapshot["version"]
    group_row = next((g for g in snapshot["groups"] if g["umo"] == clean_umo), None)
    cell = (group_row or {}).get("cells", {}).get(clean_pid) or {}
    cell_version = str(cell.get("version") or "").strip()
    row_version = str((group_row or {}).get("version") or "").strip()

    # Accept optimistic lock token matching cell version, row version, or matrix version.
    valid_tokens = {tok for tok in (cell_version, row_version, current_matrix_version) if tok}
    if valid_tokens and expected_str not in valid_tokens:
        return 409, {
            "ok": False,
            "conflict": True,
            "message": "状态已变更（版本冲突），请刷新后重试",
            "version": cell_version or row_version or current_matrix_version,
            "matrix_version": current_matrix_version,
        }

    context = getattr(plugin, "context", None)
    owner = plugin if clean_pid == CHAT_DYNAMICS_ID else series_panels.find_plugin_owner(context, clean_pid)
    if owner is None:
        return 409, {"message": "目标插件未启用或未加载"}

    setter = getattr(owner, "set_series_scope_enabled", None)
    if not callable(setter):
        return 501, {"message": "该插件当前为只读状态，不支持在总控矩阵直接切换"}

    pass_version = cell_version if cell_version else None
    try:
        raw = await _invoke_hook(setter, clean_umo, enabled, pass_version, timeout=5.0)
    except ValueError as exc:
        if "conflict" in str(exc).lower() or "version" in str(exc).lower():
            return 409, {
                "ok": False,
                "conflict": True,
                "message": "目标插件版本冲突，请刷新后重试",
                "matrix_version": current_matrix_version,
            }
        return 400, {"message": str(exc)}
    except asyncio.TimeoutError:
        return 504, {"message": "切换目标插件超时"}
    except Exception as exc:
        return 503, {"message": f"目标插件切换失败 ({type(exc).__name__})"}

    if not isinstance(raw, dict):
        return 502, {"message": "目标插件返回了无效结果"}
    if raw.get("conflict") is True:
        return 409, {
            "ok": False,
            "conflict": True,
            "message": str(raw.get("summary") or "目标插件版本冲突，请刷新后重试"),
            "version": raw.get("version", cell_version),
            "matrix_version": current_matrix_version,
        }
    if raw.get("ok") is False:
        return 400, {
            "ok": False,
            "message": str(raw.get("summary") or raw.get("message") or "切换未生效"),
        }

    series_panels.invalidate_presence_cache()
    invalidate_series_matrix_cache(plugin)
    refreshed = await build_series_matrix(plugin, scope=clean_umo, force_refresh=True)
    new_group = next((g for g in refreshed["groups"] if g["umo"] == clean_umo), None)
    new_cell = (new_group or {}).get("cells", {}).get(clean_pid) or {}
    new_enabled = raw.get("enabled") if isinstance(raw.get("enabled"), bool) else new_cell.get("enabled", enabled)
    new_version = str(raw.get("version") or new_cell.get("version") or (new_group or {}).get("version") or refreshed["version"])
    new_summary = str(raw.get("summary") or new_cell.get("summary") or ("开启" if new_enabled else "暂停"))
    return 200, {
        "ok": True,
        "umo": clean_umo,
        "plugin_id": clean_pid,
        "enabled": new_enabled,
        "version": new_version,
        "summary": new_summary,
        "matrix_version": refreshed["version"],
    }


async def batch_toggle_series_matrix(
    plugin: Any,
    *,
    umo: str,
    expected_version: Any = None,
    states: dict[str, Any] | None = None,
    template_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Concurrently apply a states map or template to one UMO across all installed plugins."""
    clean_umo = str(umo or "").strip()
    if not clean_umo or len(clean_umo) > _MAX_SCOPE_LEN:
        return 400, {"message": "umo required"}

    has_states = states is not None
    has_template = template_id is not None and str(template_id).strip() != ""
    if has_states == has_template:
        return 400, {"message": "provide exactly one of states or template_id"}

    resolved_template_id = ""
    target_states: dict[str, bool] = {}
    if has_template:
        from .series_templates import SeriesTemplateStore

        resolved_template_id = str(template_id or "").strip().lower()
        store = getattr(plugin, "series_templates", None)
        if store is None or not callable(getattr(store, "get", None)):
            store = SeriesTemplateStore()
        tpl = store.get(resolved_template_id)
        if tpl is None:
            return 404, {"message": f"未找到模板: {resolved_template_id}"}
        target_states = {
            pid: bool(val) for pid, val in (tpl.get("states") or {}).items()
        }
    else:
        if not isinstance(states, dict) or not states:
            return 400, {"message": "states must be a non-empty object"}
        for pid, val in states.items():
            clean_pid = str(pid or "").strip()
            if clean_pid not in _CORE_PLUGIN_META:
                return 400, {"message": f"unsupported plugin_id: {clean_pid}"}
            if not isinstance(val, bool):
                return 400, {"message": f"states[{clean_pid}] must be a boolean"}
            target_states[clean_pid] = val

    if expected_version is not None:
        if isinstance(expected_version, bool) or not str(expected_version).strip():
            return 400, {"message": "expected_version invalid"}

    snapshot = await build_series_matrix(plugin, scope=clean_umo, force_refresh=True)
    current_matrix_version = snapshot["version"]
    group_row = next((g for g in snapshot["groups"] if g["umo"] == clean_umo), None)
    row_version = str((group_row or {}).get("version") or "").strip()
    if expected_version is not None:
        expected_str = str(expected_version).strip()
        valid_tokens = {tok for tok in (row_version, current_matrix_version) if tok}
        if valid_tokens and expected_str not in valid_tokens:
            return 409, {
                "ok": False,
                "conflict": True,
                "message": "状态已变更（版本冲突），请刷新后重试",
                "version": row_version or current_matrix_version,
                "matrix_version": current_matrix_version,
            }

    context = getattr(plugin, "context", None)
    plugins_by_id = {p["id"]: p for p in snapshot.get("plugins") or []}
    cells_by_id = (group_row or {}).get("cells") or {}

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    async def _apply_one(pid: str, desired: bool) -> None:
        pmeta = plugins_by_id.get(pid) or {}
        state = str(pmeta.get("state") or "missing")
        if state == "conflict":
            skipped.append(
                {"plugin_id": pid, "reason": "conflict", "message": "插件存在重复/冲突注册"}
            )
            return
        if state in {"missing", "disabled", "unloaded"}:
            reason = "disabled" if state == "disabled" else "uninstalled"
            skipped.append(
                {
                    "plugin_id": pid,
                    "reason": reason,
                    "message": "插件已停用" if state == "disabled" else "未安装或未加载",
                }
            )
            return

        owner = (
            plugin
            if pid == CHAT_DYNAMICS_ID
            else series_panels.find_plugin_owner(context, pid)
        )
        if owner is None:
            skipped.append(
                {"plugin_id": pid, "reason": "uninstalled", "message": "未安装或未加载"}
            )
            return
        setter = getattr(owner, "set_series_scope_enabled", None)
        if not callable(setter):
            skipped.append(
                {"plugin_id": pid, "reason": "readonly", "message": "该插件为只读状态"}
            )
            return

        cell = cells_by_id.get(pid) or {}
        cell_ver = str(cell.get("version") or "").strip() or None
        try:
            raw = await _invoke_hook(setter, clean_umo, desired, cell_ver, timeout=5.0)
        except ValueError as exc:
            if "conflict" in str(exc).lower() or "version" in str(exc).lower():
                skipped.append(
                    {"plugin_id": pid, "reason": "conflict", "message": "目标插件版本冲突"}
                )
            else:
                failed.append(
                    {"plugin_id": pid, "reason": "invalid", "message": str(exc)}
                )
            return
        except asyncio.TimeoutError:
            failed.append(
                {"plugin_id": pid, "reason": "timeout", "message": "切换目标插件超时"}
            )
            return
        except Exception as exc:
            failed.append(
                {
                    "plugin_id": pid,
                    "reason": type(exc).__name__,
                    "message": f"切换失败 ({type(exc).__name__})",
                }
            )
            return

        if not isinstance(raw, dict):
            failed.append(
                {"plugin_id": pid, "reason": "invalid_response", "message": "返回结果无效"}
            )
            return
        if raw.get("conflict") is True:
            skipped.append(
                {
                    "plugin_id": pid,
                    "reason": "conflict",
                    "message": str(raw.get("summary") or "目标插件版本冲突"),
                }
            )
            return
        if raw.get("ok") is False:
            failed.append(
                {
                    "plugin_id": pid,
                    "reason": "rejected",
                    "message": str(raw.get("summary") or raw.get("message") or "切换未生效"),
                }
            )
            return

        actual_enabled = (
            raw.get("enabled") if isinstance(raw.get("enabled"), bool) else desired
        )
        applied.append(
            {
                "plugin_id": pid,
                "enabled": actual_enabled,
                "version": str(raw.get("version") or ""),
                "summary": str(
                    raw.get("summary") or ("开启" if actual_enabled else "暂停")
                ),
            }
        )

    ordered_pids = [
        pid for pid, *_ in CORE_SERIES_PLUGINS if pid in target_states
    ]
    await asyncio.gather(*(_apply_one(pid, target_states[pid]) for pid in ordered_pids))

    series_panels.invalidate_presence_cache()
    invalidate_series_matrix_cache(plugin)
    refreshed = await build_series_matrix(plugin, scope=clean_umo, force_refresh=True)
    new_row = next((g for g in refreshed["groups"] if g["umo"] == clean_umo), None)

    return 200, {
        "ok": True,
        "umo": clean_umo,
        "template_id": resolved_template_id,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "matrix_version": refreshed["version"],
        "row": new_row,
    }


def _sanitize_search_item(raw: Any, *, fallback_plugin_id: str, fallback_page: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()[:120]
    if not title:
        return None
    kind = str(raw.get("kind") or "item").strip()[:32] or "item"
    subtitle = str(raw.get("subtitle") or "").strip()[:200]
    plugin_id = str(raw.get("plugin_id") or fallback_plugin_id).strip()[:128] or fallback_plugin_id
    page = str(raw.get("page") or fallback_page).strip()[:64] or fallback_page
    deep_raw = raw.get("deep_link")
    if isinstance(deep_raw, dict):
        deep_link: Any = {
            str(k)[:32]: str(v)[:256]
            for k, v in list(deep_raw.items())[:8]
            if isinstance(k, str) and isinstance(v, (str, int, float, bool))
        }
    elif isinstance(deep_raw, str):
        deep_link = deep_raw.strip()[:256]
    else:
        deep_link = {}
    return {
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "plugin_id": plugin_id,
        "page": page,
        "deep_link": deep_link,
    }


def _search_semaphore_for_loop(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    lid = id(loop)
    sem = _SEARCH_SEMAPHORES.get(lid)
    if sem is None:
        if len(_SEARCH_SEMAPHORES) > 16:
            _SEARCH_SEMAPHORES.clear()
        sem = asyncio.Semaphore(_SEARCH_MAX_CONCURRENT_FANOUTS)
        _SEARCH_SEMAPHORES[lid] = sem
    return sem


def _mark_sync_search_timeout(guard_key: tuple[int, str]) -> None:
    now = time.monotonic()
    with _SYNC_SEARCH_GUARD_LOCK:
        if len(_SYNC_SEARCH_COOLDOWN_UNTIL) > 128:
            expired = [k for k, ts in _SYNC_SEARCH_COOLDOWN_UNTIL.items() if ts <= now]
            for k in expired:
                _SYNC_SEARCH_COOLDOWN_UNTIL.pop(k, None)
        _SYNC_SEARCH_COOLDOWN_UNTIL[guard_key] = now + _SYNC_SEARCH_TIMEOUT_COOLDOWN_SECONDS


def _try_acquire_sync_search_slot(guard_key: tuple[int, str]) -> bool:
    now = time.monotonic()
    with _SYNC_SEARCH_GUARD_LOCK:
        cooldown_until = _SYNC_SEARCH_COOLDOWN_UNTIL.get(guard_key, 0.0)
        if cooldown_until > now:
            return False
        if cooldown_until:
            _SYNC_SEARCH_COOLDOWN_UNTIL.pop(guard_key, None)
        inflight = _SYNC_SEARCH_INFLIGHT.get(guard_key, 0)
        if inflight >= _SYNC_SEARCH_MAX_INFLIGHT_PER_PLUGIN:
            return False
        _SYNC_SEARCH_INFLIGHT[guard_key] = inflight + 1
        return True


def _release_sync_search_slot(guard_key: tuple[int, str]) -> None:
    with _SYNC_SEARCH_GUARD_LOCK:
        remaining = _SYNC_SEARCH_INFLIGHT.get(guard_key, 0) - 1
        if remaining > 0:
            _SYNC_SEARCH_INFLIGHT[guard_key] = remaining
        else:
            _SYNC_SEARCH_INFLIGHT.pop(guard_key, None)


async def _run_search_hook(
    hook: Any,
    query: str,
    scope: str,
    limit: int,
    *,
    loop: asyncio.AbstractEventLoop,
    guard_key: tuple[int, str] | None = None,
) -> Any:
    if inspect.iscoroutinefunction(hook):
        return await hook(query, scope, limit)

    released = False

    def _release_once() -> None:
        nonlocal released
        if not released and guard_key is not None:
            released = True
            _release_sync_search_slot(guard_key)

    def _run_guarded() -> Any:
        try:
            return hook(query, scope, limit)
        finally:
            _release_once()

    try:
        fut = loop.run_in_executor(
            _get_search_executor(),
            _run_guarded,
        )
    except Exception:
        _release_once()
        raise
    result = await fut
    if inspect.isawaitable(result):
        return await result
    return result


async def federated_series_search(
    plugin: Any,
    *,
    query: str,
    scope: str = "",
    limit: int = 20,
    timeout_seconds: float = 0.30,
) -> dict[str, Any]:
    """Read-only fan-out search across ready series plugins within a strict timeout budget."""
    clean_query = str(query or "").strip()[:_MAX_QUERY_LEN]
    clean_scope = str(scope or "").strip()[:_MAX_SCOPE_LEN]
    cap = max(1, min(int(limit or 20), _MAX_SEARCH_LIMIT))
    if not clean_query:
        return {
            "query": "",
            "scope": clean_scope,
            "items": [],
            "timed_out_plugins": [],
            "elapsed_ms": 0,
        }

    started = time.monotonic()
    deadline = started + max(0.05, float(timeout_seconds))
    per_plugin_limit = max(3, min(10, cap))

    context = getattr(plugin, "context", None)
    loop = asyncio.get_running_loop()
    sem = _search_semaphore_for_loop(loop)
    acquired = False
    try:
        remaining_sem = max(0.01, deadline - time.monotonic())
        await asyncio.wait_for(sem.acquire(), timeout=remaining_sem)
        acquired = True
    except asyncio.TimeoutError:
        return {
            "query": clean_query,
            "scope": clean_scope,
            "items": [],
            "timed_out_plugins": [pid for pid, _, _, _ in CORE_SERIES_PLUGINS],
            "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
        }

    try:
        items: list[dict[str, Any]] = []
        timed_out: list[str] = []
        tasks: dict[asyncio.Task[Any], tuple[str, str, tuple[int, str] | None]] = {}
        plugin_order: list[tuple[asyncio.Task[Any], str, str]] = []

        for pid, _label, _mark, page in CORE_SERIES_PLUGINS:
            owner = plugin if pid == CHAT_DYNAMICS_ID else series_panels.find_plugin_owner(context, pid)
            if owner is None:
                continue
            hook = getattr(owner, "series_search_items", None)
            if not callable(hook):
                continue
            guard_key: tuple[int, str] | None = None
            if not inspect.iscoroutinefunction(hook):
                guard_key = (id(owner), pid)
                if not _try_acquire_sync_search_slot(guard_key):
                    timed_out.append(pid)
                    continue
            task = asyncio.create_task(
                _run_search_hook(
                    hook,
                    clean_query,
                    clean_scope,
                    per_plugin_limit,
                    loop=loop,
                    guard_key=guard_key,
                )
            )
            tasks[task] = (pid, page, guard_key)
            plugin_order.append((task, pid, page))

        if tasks:
            remaining = max(0.01, deadline - time.monotonic())
            done, pending = await asyncio.wait(tasks.keys(), timeout=remaining)
            for task, pid, page in plugin_order:
                if task not in done:
                    continue
                try:
                    raw_result = task.result()
                except Exception:
                    continue
                if isinstance(raw_result, (list, tuple)):
                    for entry in raw_result[:per_plugin_limit]:
                        sanitized = _sanitize_search_item(entry, fallback_plugin_id=pid, fallback_page=page)
                        if sanitized is not None:
                            items.append(sanitized)
            for task in pending:
                pid, _page, guard_key = tasks[task]
                timed_out.append(pid)
                if guard_key is not None:
                    _mark_sync_search_timeout(guard_key)
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if acquired:
            sem.release()

    # Deduplicate by (plugin_id, kind, title, subtitle) preserving order
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        key = (item["plugin_id"], item["kind"], item["title"], item["subtitle"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= cap:
                break

    elapsed_ms = int(round((time.monotonic() - started) * 1000))
    return {
        "query": clean_query,
        "scope": clean_scope,
        "items": deduped,
        "timed_out_plugins": sorted(set(timed_out)),
        "elapsed_ms": elapsed_ms,
    }


def _check_host_default_provider(context: Any) -> dict[str, Any]:
    """Inspect whether AstrBot's global default chat provider is explicitly configured."""
    if context is None or not callable(getattr(context, "get_config", None)):
        return {"status": "unknown", "provider_id": "", "detail": "host_config_unavailable"}
    try:
        cfg = context.get_config() or {}
    except Exception:
        return {"status": "unknown", "provider_id": "", "detail": "host_config_error"}
    agent_cfg = cfg.get("agent_runner") if isinstance(cfg, dict) else None
    model_cfg = (agent_cfg.get("config") or {}).get("model") if isinstance(agent_cfg, dict) else None
    pid = str((model_cfg or {}).get("provider_id") or "").strip() if isinstance(model_cfg, dict) else ""
    if not pid:
        prov_settings = cfg.get("provider_settings") if isinstance(cfg, dict) else None
        pid = str((prov_settings or {}).get("default_provider_id") or "").strip() if isinstance(prov_settings, dict) else ""
    if not pid:
        return {
            "status": "warn",
            "provider_id": "",
            "detail": "宿主默认聊天模型 provider_id 留空，将隐式回退至首个可用模型",
        }
    found = series_panels.find_provider_silently(context, pid)
    if found is None:
        return {
            "status": "error",
            "provider_id": pid,
            "detail": f"宿主默认聊天模型 {pid} 未找到或未启用",
        }
    return {"status": "ok", "provider_id": pid, "detail": "ok"}


def _detect_stray_staging_dirs(plugin: Any) -> list[str]:
    """Detect leftover temporary staging folders (e.g. .narrative-stage-*) next to plugins."""
    from pathlib import Path

    found: list[str] = []
    try:
        plugins_root = Path(__file__).resolve().parents[2]
        if plugins_root.is_dir():
            for child in sorted(plugins_root.iterdir()):
                if child.is_dir() and child.name.startswith(".narrative-stage-"):
                    found.append(child.name)
    except OSError:
        pass
    return found[:16]


def build_series_health(plugin: Any) -> dict[str, Any]:
    """Aggregate read-only health, provider bindings, and environment diagnostics across all 8 plugins."""
    context = getattr(plugin, "context", None)
    pres = series_panels.presence(context, CHAT_DYNAMICS_ID, force_refresh=True)
    presence_by_id = {
        p["id"]: p
        for p in (pres.get("plugins") or [])
        if isinstance(p, dict) and p.get("id")
    }
    shared_cfg = series_panels.get_series_shared_config(context)
    host_default = _check_host_default_provider(context)
    stray_dirs = _detect_stray_staging_dirs(plugin)

    plugins_out: list[dict[str, Any]] = []
    all_issues: list[dict[str, str]] = []
    ready_count = 0
    installed_count = 0

    for pid, label, mark, page in CORE_SERIES_PLUGINS:
        pinfo = presence_by_id.get(pid) or {}
        installed = bool(pinfo.get("installed", pid == CHAT_DYNAMICS_ID))
        state = str(pinfo.get("state") or ("ready" if pid == CHAT_DYNAMICS_ID else "missing"))
        owner = plugin if pid == CHAT_DYNAMICS_ID else series_panels.find_plugin_owner(context, pid)
        if installed:
            installed_count += 1
        health = series_panels.read_plugin_health(pid, owner) if owner is not None else None
        if health is None:
            is_ready = state in {"ready", "enabled"}
            degraded = installed and not is_ready and state != "inactive"
            health = {
                "plugin_id": pid,
                "ready": is_ready,
                "degraded": degraded,
                "providers": [],
                "issues": ([] if is_ready or not installed or state == "inactive" else [f"plugin_state:{state}"]),
                "metrics": {},
            }
        else:
            # Verify any declared provider_id against silent host lookup
            for prov_item in health.get("providers") or ():
                if not isinstance(prov_item, dict):
                    continue
                declared_id = str(prov_item.get("provider_id") or "").strip()
                cur_status = str(prov_item.get("status") or "unknown")
                if declared_id and cur_status in {"unknown", "configured"}:
                    resolved = series_panels.find_provider_silently(context, declared_id)
                    prov_item["status"] = "ok" if resolved is not None else "missing"
                    if resolved is None:
                        prov_item["detail"] = prov_item.get("detail") or f"Provider {declared_id} 未找到"
                        health["degraded"] = True
                        issue_msg = f"provider_missing:{prov_item.get('role') or 'default'}:{declared_id}"
                        if issue_msg not in health["issues"]:
                            health["issues"].append(issue_msg)

        if health["ready"] and not health["degraded"]:
            ready_count += 1
        for msg in health.get("issues") or ():
            all_issues.append({"plugin_id": pid, "label": label, "issue": str(msg)})

        plugins_out.append(
            {
                "id": pid,
                "label": label,
                "mark": mark,
                "page": page,
                "installed": installed,
                "state": state,
                "version": str(pinfo.get("version") or ""),
                "ready": bool(health["ready"]),
                "degraded": bool(health["degraded"]),
                "providers": health.get("providers") or [],
                "issues": health.get("issues") or [],
                "metrics": health.get("metrics") or {},
            }
        )

    env_warnings: list[dict[str, str]] = []
    if host_default["status"] != "ok":
        env_warnings.append(
            {
                "code": f"host_default_provider_{host_default['status']}",
                "severity": host_default["status"],
                "message": str(host_default["detail"]),
            }
        )
    for conflict in pres.get("legacy_conflicts") or ():
        env_warnings.append(
            {
                "code": "legacy_plugin_conflict",
                "severity": "warn",
                "message": f"旧插件 {conflict.get('label') or conflict.get('id')} 仍启用，建议停用并交由 {conflict.get('owner')} 接管",
            }
        )
    for dname in stray_dirs:
        env_warnings.append(
            {
                "code": "stray_staging_dir",
                "severity": "warn",
                "message": f"插件目录存在临时残留文件夹 {dname}，建议清理",
            }
        )

    overall_status = "ok"
    if any(p["installed"] and (not p["ready"] or p["degraded"]) for p in plugins_out) or host_default["status"] == "error":
        overall_status = "degraded"
    elif env_warnings:
        overall_status = "warn"

    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "status": overall_status,
        "summary": {
            "installed_count": installed_count,
            "ready_count": ready_count,
            "issue_count": len(all_issues) + len(env_warnings),
            "shared_reply_provider": str(shared_cfg.get("reply_provider") or ""),
            "shared_decision_provider": str(shared_cfg.get("decision_provider") or ""),
            "shared_timezone": str(shared_cfg.get("rhythm_timezone") or ""),
        },
        "host_default_provider": host_default,
        "environment_warnings": env_warnings,
        "plugins": plugins_out,
        "issues": all_issues,
    }
