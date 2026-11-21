"""Cross-plugin hub aggregators and v2.0 series hooks for Chat Dynamics."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
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

_SEARCH_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_SEARCH_EXECUTOR_LOCK = threading.Lock()


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


def chat_dynamics_scope_status(plugin: Any, umo: str) -> dict[str, Any]:
    """Read-only per-UMO status hook for Chat Dynamics."""
    clean = str(umo or "").strip()
    takeover_enabled, group_id = _takeover_state_for_umo(plugin, clean)
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
    elif cooling:
        summary = "冷却中"
    elif takeover_enabled:
        summary = "开启"
    else:
        summary = "暂停"
    return {
        "enabled": takeover_enabled,
        "supported": True,
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
    new_version = chat_dynamics_scope_version(plugin)
    actual_enabled, _ = _takeover_state_for_umo(plugin, clean)
    return {
        "ok": True,
        "enabled": actual_enabled,
        "version": new_version,
        "summary": "开启" if actual_enabled else "暂停",
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


async def build_series_matrix(
    plugin: Any,
    *,
    scope: str = "",
    persona: str = "",
) -> dict[str, Any]:
    """Aggregate all authorized/known groups across all 8 core series plugins."""
    context = getattr(plugin, "context", None)
    pres = series_panels.presence(context, CHAT_DYNAMICS_ID)
    presence_by_id = {p["id"]: p for p in pres.get("plugins") or [] if isinstance(p, dict) and p.get("id")}

    owners: dict[str, Any] = {CHAT_DYNAMICS_ID: plugin}
    plugins_meta: list[dict[str, Any]] = []
    for pid, label, mark, page in CORE_SERIES_PLUGINS:
        pinfo = presence_by_id.get(pid) or {}
        owner = plugin if pid == CHAT_DYNAMICS_ID else series_panels.find_plugin_owner(context, pid)
        if owner is not None:
            owners[pid] = owner
        state = str(pinfo.get("state") or ("ready" if pid == CHAT_DYNAMICS_ID else "missing"))
        if pid == CHAT_DYNAMICS_ID and state in {"missing", "unloaded"}:
            state = "ready" if bool(getattr(plugin, "enabled", True)) else "inactive"
        supports_toggle = bool(
            owner is not None and callable(getattr(owner, "set_series_scope_enabled", None))
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

    umos = _collect_matrix_umos(plugin, scope_filter=scope)
    default_persona = _default_persona_id(plugin)
    clean_persona_filter = str(persona or "").strip().lower()
    groups_out: list[dict[str, Any]] = []

    for umo in umos:
        cells: dict[str, dict[str, Any]] = {}
        specific_sibling_persona = ""
        sibling_persona = ""
        chat_cell_persona = ""
        for pmeta in plugins_meta:
            pid = pmeta["id"]
            owner = owners.get(pid)
            state = pmeta["state"]
            base_cell = {
                "enabled": None,
                "supported": False,
                "state": state,
                "summary": "未安装" if state == "missing" else "已停用" if state in {"disabled", "unloaded"} else "只读",
                "activity_today": None,
                "persona_id": "",
                "version": "",
                "can_open": pmeta["can_open"],
                "page": pmeta["page"],
            }
            if owner is not None and state not in {"missing", "disabled", "unloaded", "conflict"}:
                hook = getattr(owner, "get_series_scope_status", None)
                if callable(hook):
                    try:
                        raw = await _invoke_hook(hook, umo, timeout=_PER_PLUGIN_STATUS_TIMEOUT)
                        sanitized = _sanitize_scope_status(
                            raw,
                            default_supported=callable(getattr(owner, "set_series_scope_enabled", None)),
                        )
                        if not callable(getattr(owner, "set_series_scope_enabled", None)):
                            sanitized["supported"] = False
                        base_cell.update(sanitized)
                        cell_persona = sanitized["persona_id"]
                        if cell_persona:
                            if pid == CHAT_DYNAMICS_ID:
                                chat_cell_persona = cell_persona
                            else:
                                if not sibling_persona:
                                    sibling_persona = cell_persona
                                if cell_persona != default_persona and not specific_sibling_persona:
                                    specific_sibling_persona = cell_persona
                    except Exception:
                        base_cell["summary"] = "状态异常"
                        base_cell["state"] = "error"
                elif pid == CHAT_DYNAMICS_ID:
                    sanitized = _sanitize_scope_status(
                        chat_dynamics_scope_status(plugin, umo),
                        default_supported=True,
                    )
                    base_cell.update(sanitized)
                    if sanitized["persona_id"]:
                        chat_cell_persona = sanitized["persona_id"]
            cells[pid] = base_cell

        row_persona = (
            specific_sibling_persona
            or (chat_cell_persona if chat_cell_persona and chat_cell_persona != default_persona else "")
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
                "persona_id": row_persona,
                "version": row_version,
                "cells": cells,
            }
        )

    matrix_version = _matrix_digest(groups_out)
    return {
        "version": matrix_version,
        "updated_at": _now_iso(),
        "plugins": plugins_meta,
        "groups": groups_out,
    }


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

    snapshot = await build_series_matrix(plugin, scope=clean_umo)
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
    refreshed = await build_series_matrix(plugin, scope=clean_umo)
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


async def _run_search_hook(
    hook: Any,
    query: str,
    scope: str,
    limit: int,
    *,
    loop: asyncio.AbstractEventLoop,
) -> Any:
    if inspect.iscoroutinefunction(hook):
        return await hook(query, scope, limit)
    result = await loop.run_in_executor(
        _get_search_executor(),
        hook,
        query,
        scope,
        limit,
    )
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
    items: list[dict[str, Any]] = []
    timed_out: list[str] = []
    tasks: dict[asyncio.Task[Any], tuple[str, str]] = {}
    plugin_order: list[tuple[asyncio.Task[Any], str, str]] = []

    for pid, _label, _mark, page in CORE_SERIES_PLUGINS:
        owner = plugin if pid == CHAT_DYNAMICS_ID else series_panels.find_plugin_owner(context, pid)
        if owner is None:
            continue
        hook = getattr(owner, "series_search_items", None)
        if not callable(hook):
            continue
        task = asyncio.create_task(
            _run_search_hook(hook, clean_query, clean_scope, per_plugin_limit, loop=loop)
        )
        tasks[task] = (pid, page)
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
            pid, _page = tasks[task]
            timed_out.append(pid)
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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
