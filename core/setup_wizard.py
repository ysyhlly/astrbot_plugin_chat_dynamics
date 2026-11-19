"""Setup wizard group discovery and opt-in provider connectivity probes."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from typing import Any

from astrbot.api import logger

from .dashboard import snapshot_sessions
from .llm_adapter import _call_session_getter, require_completion_text
from .systemone.native import ask_native, is_decision_provider

_PER_PLATFORM_TIMEOUT = 4.0
_TOTAL_DISCOVERY_TIMEOUT = 6.0
_MAX_DISCOVERED_GROUPS = 500
_PROBE_TIMEOUT = 6.0
_MAX_PROVIDER_ID_LENGTH = 128
_MAX_ERROR_MESSAGE_LENGTH = 160

_ALLOWED_PROBE_FIELDS = frozenset(
    {
        "participation_backend",
        "decision_provider",
        "decision_llm_provider",
        "reply_provider",
        "discussion_judgment_mode",
        "discussion_reranker_provider",
        "discussion_llm_provider",
        "probe_discussion",
    }
)
_STRING_PROVIDER_FIELDS = (
    "decision_provider",
    "decision_llm_provider",
    "reply_provider",
    "discussion_reranker_provider",
    "discussion_llm_provider",
)
_SOURCE_ORDER = ("platform", "session", "config")

_CREDENTIAL_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+"), "Bearer ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|authorization)\s*([:=])\s*['\"]?[^\s'\",;}{]+['\"]?"
        ),
        r"\1\2***",
    ),
    (re.compile(r"(https?://)[^:@/\s]+:[^@/\s]+@"), r"\1***:***@"),
)


def _sanitize_error_message(exc_or_text: Any, fallback: str = "连接失败") -> str:
    """Scrub potential credentials/tracebacks and cap length for panel display."""
    raw = str(exc_or_text or "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    text = lines[0] if lines else ""
    if text.startswith("Traceback (most recent call last)"):
        text = ""
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback
    if len(text) > _MAX_ERROR_MESSAGE_LENGTH:
        return text[: _MAX_ERROR_MESSAGE_LENGTH - 3] + "..."
    return text


def _platform_identity(platform: Any, index: int) -> tuple[str, str]:
    meta = None
    meta_fn = getattr(platform, "meta", None)
    if callable(meta_fn):
        try:
            meta = meta_fn()
        except Exception:
            meta = None
    if meta is None:
        meta = getattr(platform, "metadata", None)
    cfg = getattr(platform, "config", None)
    if not isinstance(cfg, dict):
        cfg = {}

    meta_dict = meta if isinstance(meta, dict) else {}
    platform_id = str(
        getattr(meta, "id", "")
        or meta_dict.get("id", "")
        or getattr(platform, "id", "")
        or cfg.get("id", "")
        or ""
    ).strip()
    platform_type = str(
        getattr(meta, "name", "")
        or getattr(meta, "type", "")
        or meta_dict.get("name", "")
        or meta_dict.get("type", "")
        or getattr(platform, "type", "")
        or cfg.get("type", "")
        or type(platform).__name__
    ).strip()
    if not platform_id:
        platform_id = platform_type or f"platform-{index}"
    return platform_id, platform_type


def _collect_platform_instances(context: Any) -> list[Any]:
    if context is None:
        return []
    manager = getattr(context, "platform_manager", None)
    if manager is not None:
        getter = getattr(manager, "get_insts", None)
        if callable(getter):
            try:
                return list(getter() or [])
            except Exception as exc:
                logger.warning(
                    "[ChatDynamics] Setup group discovery get_insts failed type=%s",
                    type(exc).__name__,
                )
        insts = getattr(manager, "platform_insts", None)
        if insts is not None:
            try:
                return list(insts)
            except Exception:
                return []
    direct = getattr(context, "platform_insts", None)
    if direct is not None:
        try:
            return list(direct)
        except Exception:
            return []
    return []


def _parse_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _is_private_session_identifier(value: str) -> bool:
    return ":FriendMessage:" in value or ":PrivateMessage:" in value


def _extract_raw_group_id(group_id: str, umo: str) -> str:
    gid = (group_id or "").strip()
    if gid and ":GroupMessage:" in gid:
        gid = gid.rsplit(":", 1)[-1].strip()
    if not gid and umo and ":GroupMessage:" in umo:
        gid = umo.rsplit(":", 1)[-1].strip()
    return gid


def _merge_group_entry(
    store: dict[str, dict[str, Any]],
    *,
    group_id: str,
    group_name: str = "",
    platform_id: str = "",
    platform_type: str = "",
    umo: str = "",
    member_count: int | None = None,
    source: str,
    last_activity: float | None = None,
) -> None:
    gid = str(group_id or "").strip()
    if not gid:
        return
    existing = store.get(gid)
    if existing is None:
        existing = {
            "group_id": gid,
            "group_name": "",
            "platform_id": "",
            "platform_type": "",
            "umo": "",
            "member_count": None,
            "sources": [],
            "last_activity": None,
            "in_takeover": False,
            "in_exclude": False,
        }
        store[gid] = existing

    if group_name and not existing["group_name"]:
        existing["group_name"] = group_name[:120]
    if platform_id and not existing["platform_id"]:
        existing["platform_id"] = platform_id
    if platform_type and not existing["platform_type"]:
        existing["platform_type"] = platform_type
    if umo and not existing["umo"]:
        existing["umo"] = umo
    elif not existing["umo"] and existing["platform_id"]:
        existing["umo"] = f"{existing['platform_id']}:GroupMessage:{gid}"
    if member_count is not None and existing["member_count"] is None:
        existing["member_count"] = member_count
    if last_activity is not None and math.isfinite(last_activity) and last_activity > 0:
        prev = existing["last_activity"]
        existing["last_activity"] = max(prev, float(last_activity)) if prev is not None else float(last_activity)
    if source not in existing["sources"]:
        existing["sources"].append(source)
        existing["sources"].sort(key=lambda item: _SOURCE_ORDER.index(item) if item in _SOURCE_ORDER else 99)


def _configured_group_sets(plugin: Any) -> tuple[set[str], set[str]]:
    effective: dict[str, Any] = {}
    getter = getattr(plugin, "get_effective_config", None)
    if callable(getter):
        try:
            raw = getter()
            if isinstance(raw, dict):
                effective = raw
        except Exception:
            effective = {}

    def _norm(value: Any) -> set[str]:
        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
        else:
            items = []
        return {str(item).strip() for item in items if str(item).strip()}

    if "takeover_groups" in effective or "exclude_groups" in effective:
        return _norm(effective.get("takeover_groups")), _norm(effective.get("exclude_groups"))

    cfg = getattr(plugin, "_runtime_config", None)
    takeover = getattr(cfg, "takeover_groups", None) if cfg is not None else getattr(plugin, "takeover_groups", ())
    exclude = getattr(cfg, "exclude_groups", None) if cfg is not None else getattr(plugin, "exclude_groups", ())
    return _norm(takeover), _norm(exclude)


async def _query_onebot_platform(
    platform_id: str,
    platform_type: str,
    call_action: Any,
    timeout: float = _PER_PLATFORM_TIMEOUT,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    async def _invoke() -> Any:
        result = call_action("get_group_list")
        if inspect.isawaitable(result):
            result = await result
        return result

    try:
        raw = await asyncio.wait_for(_invoke(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return (
            {
                "platform_id": platform_id,
                "platform_type": platform_type,
                "status": "timeout",
                "group_count": 0,
                "error": "timeout",
            },
            [],
        )
    except (AttributeError, NotImplementedError):
        return (
            {
                "platform_id": platform_id,
                "platform_type": platform_type,
                "status": "error",
                "group_count": 0,
                "error": "action_unavailable",
            },
            [],
        )
    except Exception as exc:
        logger.warning(
            "[ChatDynamics] Setup group discovery failed platform=%s type=%s",
            platform_id,
            type(exc).__name__,
        )
        return (
            {
                "platform_id": platform_id,
                "platform_type": platform_type,
                "status": "error",
                "group_count": 0,
                "error": "query_failed",
            },
            [],
        )

    rows: list[Any] | None = None
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            rows = data
        elif raw.get("status") == "failed" or raw.get("retcode") not in (None, 0):
            return (
                {
                    "platform_id": platform_id,
                    "platform_type": platform_type,
                    "status": "error",
                    "group_count": 0,
                    "error": "query_failed",
                },
                [],
            )
        elif data is None:
            rows = []
    elif raw is None:
        rows = []

    if rows is None:
        return (
            {
                "platform_id": platform_id,
                "platform_type": platform_type,
                "status": "error",
                "group_count": 0,
                "error": "query_failed",
            },
            [],
        )

    parsed: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw_gid = item.get("group_id") if item.get("group_id") is not None else item.get("id")
        gid = str(raw_gid if raw_gid is not None else "").strip()
        if not gid:
            continue
        parsed.append(
            {
                "group_id": gid,
                "group_name": str(item.get("group_name") or item.get("name") or "").strip()[:120],
                "platform_id": platform_id,
                "platform_type": platform_type,
                "umo": f"{platform_id}:GroupMessage:{gid}" if platform_id else "",
                "member_count": _parse_optional_int(item.get("member_count")),
            }
        )

    return (
        {
            "platform_id": platform_id,
            "platform_type": platform_type,
            "status": "ok",
            "group_count": len(parsed),
            "error": "",
        },
        parsed,
    )


async def discover_setup_groups(
    plugin: Any,
    *,
    per_platform_timeout: float = _PER_PLATFORM_TIMEOUT,
    total_timeout: float = _TOTAL_DISCOVERY_TIMEOUT,
) -> dict[str, Any]:
    """Best-effort group discovery across OneBot platforms, runtime sessions, and config."""
    context = getattr(plugin, "context", None)
    insts = _collect_platform_instances(context)

    platform_entries: list[dict[str, Any] | None] = [None] * len(insts)
    task_map: dict[asyncio.Task[tuple[dict[str, Any], list[dict[str, Any]]]], tuple[int, str, str]] = {}
    partial_failure = False

    for idx, platform in enumerate(insts):
        platform_id, platform_type = _platform_identity(platform, idx)
        bot = getattr(platform, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            platform_entries[idx] = {
                "platform_id": platform_id,
                "platform_type": platform_type,
                "status": "unsupported",
                "group_count": 0,
                "error": "",
            }
            continue
        task = asyncio.create_task(
            _query_onebot_platform(platform_id, platform_type, call_action, timeout=per_platform_timeout)
        )
        task_map[task] = (idx, platform_id, platform_type)

    groups_by_id: dict[str, dict[str, Any]] = {}

    if task_map:
        done, pending = await asyncio.wait(list(task_map.keys()), timeout=total_timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            partial_failure = True
            for task in pending:
                idx, platform_id, platform_type = task_map[task]
                platform_entries[idx] = {
                    "platform_id": platform_id,
                    "platform_type": platform_type,
                    "status": "timeout",
                    "group_count": 0,
                    "error": "timeout",
                }
        for task in done:
            idx, platform_id, platform_type = task_map[task]
            try:
                status_entry, items = task.result()
            except Exception:
                partial_failure = True
                platform_entries[idx] = {
                    "platform_id": platform_id,
                    "platform_type": platform_type,
                    "status": "error",
                    "group_count": 0,
                    "error": "query_failed",
                }
                continue
            platform_entries[idx] = status_entry
            if status_entry["status"] in ("timeout", "error"):
                partial_failure = True
            for item in items:
                _merge_group_entry(
                    groups_by_id,
                    group_id=item["group_id"],
                    group_name=item["group_name"],
                    platform_id=item["platform_id"],
                    platform_type=item["platform_type"],
                    umo=item["umo"],
                    member_count=item["member_count"],
                    source="platform",
                )

    platforms = [entry for entry in platform_entries if entry is not None]

    # Merge runtime sessions from plugin._sessions and snapshot_sessions.
    sessions_map = getattr(plugin, "_sessions", None)
    if not isinstance(sessions_map, dict):
        sessions_map = {}

    for session_key, runtime in list(sessions_map.items()):
        if str(getattr(runtime, "scope_kind", "group") or "").strip().lower() == "private":
            continue
        umo = str(getattr(runtime, "umo", "") or session_key or "").strip()
        if _is_private_session_identifier(umo) or _is_private_session_identifier(str(session_key or "")):
            continue
        raw_gid = _extract_raw_group_id(str(getattr(runtime, "group_id", "") or ""), umo)
        if not raw_gid or _is_private_session_identifier(raw_gid):
            continue
        platform_id = umo.split(":", 1)[0] if ":GroupMessage:" in umo else ""
        last_activity = getattr(runtime, "last_activity", None)
        _merge_group_entry(
            groups_by_id,
            group_id=raw_gid,
            platform_id=platform_id,
            umo=umo if ":GroupMessage:" in umo else "",
            source="session",
            last_activity=float(last_activity) if isinstance(last_activity, (int, float)) else None,
        )

    try:
        snaps = snapshot_sessions(plugin)
    except Exception:
        snaps = []

    for row in snaps:
        if not isinstance(row, dict):
            continue
        session_key = str(row.get("session_key") or "").strip()
        runtime = sessions_map.get(session_key)
        if runtime is not None and str(getattr(runtime, "scope_kind", "group") or "").strip().lower() == "private":
            continue
        umo = str(getattr(runtime, "umo", "") if runtime is not None else session_key).strip()
        if _is_private_session_identifier(umo) or _is_private_session_identifier(session_key):
            continue
        raw_group_field = str(row.get("group_id") or "").strip()
        if _is_private_session_identifier(raw_group_field):
            continue
        raw_gid = _extract_raw_group_id(raw_group_field, umo)
        if not raw_gid:
            continue
        platform_id = umo.split(":", 1)[0] if ":GroupMessage:" in umo else ""
        last_activity = row.get("last_activity")
        _merge_group_entry(
            groups_by_id,
            group_id=raw_gid,
            platform_id=platform_id,
            umo=umo if ":GroupMessage:" in umo else "",
            source="session",
            last_activity=float(last_activity) if isinstance(last_activity, (int, float)) else None,
        )

    # Merge configured takeover_groups & exclude_groups.
    takeover_set, exclude_set = _configured_group_sets(plugin)
    for gid in sorted(takeover_set | exclude_set):
        _merge_group_entry(groups_by_id, group_id=gid, source="config")

    discovered_count = sum(1 for row in groups_by_id.values() if "platform" in row["sources"])
    session_count = sum(1 for row in groups_by_id.values() if "session" in row["sources"])

    groups: list[dict[str, Any]] = []
    for gid, row in groups_by_id.items():
        row["in_takeover"] = gid in takeover_set
        row["in_exclude"] = gid in exclude_set
        groups.append(row)

    groups.sort(
        key=lambda row: (
            not row["in_takeover"],
            -(row["last_activity"] or 0.0),
            (row["group_name"] or row["group_id"]).lower(),
            row["group_id"],
        )
    )

    return {
        "groups": groups[:_MAX_DISCOVERED_GROUPS],
        "platforms": platforms,
        "discovered_count": discovered_count,
        "session_count": session_count,
        "partial_failure": partial_failure,
    }


def validate_setup_probe_payload(payload: Any) -> dict[str, Any]:
    """Strictly validate setup/probe payload fields and bounds."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    unknown = sorted(set(payload) - _ALLOWED_PROBE_FIELDS)
    if unknown:
        raise ValueError("unknown fields: " + ", ".join(unknown))

    normalized: dict[str, Any] = {}

    if "participation_backend" in payload:
        backend = payload["participation_backend"]
        if not isinstance(backend, str) or backend.strip() not in {"", "jev", "llm"}:
            raise ValueError("participation_backend must be jev or llm")
        normalized["participation_backend"] = backend.strip()

    if "discussion_judgment_mode" in payload:
        mode = payload["discussion_judgment_mode"]
        if not isinstance(mode, str) or mode.strip() not in {"", "rerank", "llm"}:
            raise ValueError("discussion_judgment_mode must be rerank or llm")
        normalized["discussion_judgment_mode"] = mode.strip()

    for field in _STRING_PROVIDER_FIELDS:
        if field in payload:
            raw = payload[field]
            if not isinstance(raw, str):
                raise ValueError(f"{field} must be a string")
            if len(raw) > _MAX_PROVIDER_ID_LENGTH:
                raise ValueError(f"{field} is too long")
            normalized[field] = raw.strip()

    if "probe_discussion" in payload:
        flag = payload["probe_discussion"]
        if not isinstance(flag, bool):
            raise ValueError("probe_discussion must be a boolean")
        normalized["probe_discussion"] = flag

    return normalized


def _provider_id_of(provider: Any) -> str:
    if provider is None:
        return ""
    meta_fn = getattr(provider, "meta", None)
    if callable(meta_fn):
        try:
            meta = meta_fn()
            pid = str(getattr(meta, "id", "") or "").strip()
            if pid:
                return pid
        except Exception:
            pass
    cfg = getattr(provider, "provider_config", None)
    if isinstance(cfg, dict) and cfg.get("id"):
        return str(cfg["id"]).strip()
    return str(getattr(provider, "id", "") or "").strip()


def _provider_model_name(provider: Any) -> str:
    if provider is None:
        return ""
    getter = getattr(provider, "get_model", None)
    if callable(getter):
        try:
            model = str(getter() or "").strip()
            if model:
                return model
        except Exception:
            pass
    meta_fn = getattr(provider, "meta", None)
    if callable(meta_fn):
        try:
            meta = meta_fn()
            model = str(getattr(meta, "model", "") or "").strip()
            if model:
                return model
        except Exception:
            pass
    cfg = getattr(provider, "provider_config", None)
    if isinstance(cfg, dict) and cfg.get("model"):
        return str(cfg["model"]).strip()
    return str(getattr(provider, "model_name", "") or "").strip()


def _get_provider(context: Any, provider_id: str) -> Any:
    if context is None or not provider_id:
        return None
    getter = getattr(context, "get_provider_by_id", None)
    if callable(getter):
        try:
            return getter(provider_id)
        except Exception:
            return None
    manager = getattr(context, "provider_manager", None)
    pools: list[Any] = []
    if manager is not None:
        for attr in ("provider_insts", "decision_provider_insts", "rerank_provider_insts"):
            pools.extend(list(getattr(manager, attr, ()) or ()))
    for fn_name in ("get_all_providers", "get_all_decision_providers"):
        fn = getattr(context, fn_name, None)
        if callable(fn):
            try:
                pools.extend(list(fn() or ()))
            except Exception:
                pass
    for provider in pools:
        if _provider_id_of(provider) == provider_id:
            return provider
    return None


async def _resolve_default_chat_provider(context: Any) -> tuple[str, Any]:
    if context is None:
        return "", None

    fallback_pid = ""
    get_current = getattr(context, "get_current_chat_provider_id", None)
    if callable(get_current):
        try:
            pid = _call_session_getter(get_current, "")
            if inspect.isawaitable(pid):
                pid = await pid
            pid_str = str(pid or "").strip()
            if pid_str:
                provider = _get_provider(context, pid_str)
                if provider is not None:
                    return pid_str, provider
                fallback_pid = pid_str
        except Exception:
            pass

    get_using = getattr(context, "get_using_provider", None)
    if callable(get_using):
        try:
            provider = _call_session_getter(get_using, "")
            if inspect.isawaitable(provider):
                provider = await provider
            if provider is not None:
                return _provider_id_of(provider), provider
        except Exception:
            pass

    manager = getattr(context, "provider_manager", None)
    curr = getattr(manager, "curr_provider_inst", None) if manager is not None else None
    if curr is not None:
        return _provider_id_of(curr), curr

    return fallback_pid, None


async def _probe_jev_provider(
    context: Any,
    provider_id: str,
    *,
    timeout: float = _PROBE_TIMEOUT,
) -> dict[str, Any]:
    if not provider_id:
        return {
            "status": "skipped",
            "code": "skipped",
            "backend": "jev",
            "provider_id": "",
            "model": "",
            "using_default": False,
            "latency_ms": 0,
            "message": "未选择决策模型",
        }

    started = time.perf_counter()
    provider = _get_provider(context, provider_id)
    model = _provider_model_name(provider)
    if provider is None or not is_decision_provider(provider):
        return {
            "status": "error",
            "code": "invalid_provider",
            "backend": "jev",
            "provider_id": provider_id,
            "model": model,
            "using_default": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "所选决策模型未加载或类型不匹配",
        }

    ask_fn = getattr(provider, "ask", None)
    evaluator = getattr(provider, "systemone_evaluate", None)
    if not callable(ask_fn) and not callable(evaluator):
        return {
            "status": "error",
            "code": "invalid_provider",
            "backend": "jev",
            "provider_id": provider_id,
            "model": model,
            "using_default": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "决策模型不支持连通性检测接口",
        }

    state = {"probe": "connectivity_check"}
    questions = {
        "ok": {
            "type": "noul",
            "instructions": "Connectivity probe. Return any valid probability in [0,1].",
        }
    }

    async def _invoke() -> Any:
        if callable(ask_fn):
            return await ask_native(provider, state=state, questions=questions)
        return await evaluator(state=state, questions=questions, timeout=timeout)

    try:
        await asyncio.wait_for(_invoke(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return {
            "status": "error",
            "code": "timeout",
            "backend": "jev",
            "provider_id": provider_id,
            "model": model,
            "using_default": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "连接超时",
        }
    except Exception as exc:
        logger.warning(
            "[ChatDynamics] Setup Jev probe failed provider=%s type=%s",
            provider_id,
            type(exc).__name__,
        )
        return {
            "status": "error",
            "code": "provider_error",
            "backend": "jev",
            "provider_id": provider_id,
            "model": model,
            "using_default": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": _sanitize_error_message(exc, "决策模型调用失败"),
        }

    return {
        "status": "ok",
        "code": "ok",
        "backend": "jev",
        "provider_id": provider_id,
        "model": model,
        "using_default": False,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": "连接正常",
    }


async def _probe_chat_provider(
    context: Any,
    provider_id: str,
    provider: Any,
    *,
    using_default: bool,
    timeout: float = _PROBE_TIMEOUT,
) -> dict[str, Any]:
    started = time.perf_counter()
    if using_default and not provider_id and provider is None:
        return {
            "status": "error",
            "code": "no_default_provider",
            "provider_id": "",
            "model": "",
            "using_default": True,
            "latency_ms": 0,
            "message": "未找到 AstrBot 默认聊天模型",
        }

    resolved_provider = provider if provider is not None else _get_provider(context, provider_id)
    model = _provider_model_name(resolved_provider)
    if resolved_provider is None:
        return {
            "status": "error",
            "code": "invalid_provider",
            "provider_id": provider_id,
            "model": model,
            "using_default": using_default,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "所选聊天模型未加载或不存在",
        }
    if is_decision_provider(resolved_provider) or not callable(getattr(resolved_provider, "text_chat", None)):
        return {
            "status": "error",
            "code": "invalid_provider",
            "provider_id": provider_id,
            "model": model,
            "using_default": using_default,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "所选模型不是可用的普通聊天模型",
        }

    async def _invoke() -> str:
        generate = getattr(context, "llm_generate", None) if context is not None else None
        if callable(generate) and provider_id:
            resp = generate(
                chat_provider_id=provider_id,
                prompt="Reply with OK.",
                system_prompt="Connectivity check. Reply OK only.",
            )
            if inspect.isawaitable(resp):
                resp = await resp
        else:
            resp = resolved_provider.text_chat(
                prompt="Reply with OK.",
                system_prompt="Connectivity check. Reply OK only.",
            )
            if inspect.isawaitable(resp):
                resp = await resp
        text = require_completion_text(resp, "setup_probe")
        if not text.strip():
            raise RuntimeError("empty completion")
        return text

    try:
        await asyncio.wait_for(_invoke(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return {
            "status": "error",
            "code": "timeout",
            "provider_id": provider_id,
            "model": model,
            "using_default": using_default,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "连接超时",
        }
    except Exception as exc:
        logger.warning(
            "[ChatDynamics] Setup chat probe failed provider=%s type=%s",
            provider_id or "(default)",
            type(exc).__name__,
        )
        return {
            "status": "error",
            "code": "provider_error",
            "provider_id": provider_id,
            "model": model,
            "using_default": using_default,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": _sanitize_error_message(exc, "聊天模型调用失败"),
        }

    return {
        "status": "ok",
        "code": "ok",
        "provider_id": provider_id,
        "model": model,
        "using_default": using_default,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": "连接正常",
    }


async def _probe_rerank_provider(
    context: Any,
    provider_id: str,
    *,
    timeout: float = _PROBE_TIMEOUT,
) -> dict[str, Any]:
    if not provider_id:
        return {
            "status": "skipped",
            "code": "skipped",
            "mode": "rerank",
            "provider_id": "",
            "model": "",
            "using_default": False,
            "latency_ms": 0,
            "message": "未选择重排序模型",
        }

    started = time.perf_counter()
    provider = _get_provider(context, provider_id)
    model = _provider_model_name(provider)
    rerank_fn = getattr(provider, "rerank", None) if provider is not None else None
    if provider is None or not callable(rerank_fn):
        return {
            "status": "error",
            "code": "invalid_provider",
            "mode": "rerank",
            "provider_id": provider_id,
            "model": model,
            "using_default": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "所选重排序模型未加载或类型不匹配",
        }

    async def _invoke() -> Any:
        res = rerank_fn(query="ping", documents=["pong"], top_n=1)
        if inspect.isawaitable(res):
            res = await res
        if res is None:
            raise RuntimeError("empty rerank response")
        return res

    try:
        await asyncio.wait_for(_invoke(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return {
            "status": "error",
            "code": "timeout",
            "mode": "rerank",
            "provider_id": provider_id,
            "model": model,
            "using_default": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "连接超时",
        }
    except Exception as exc:
        logger.warning(
            "[ChatDynamics] Setup rerank probe failed provider=%s type=%s",
            provider_id,
            type(exc).__name__,
        )
        return {
            "status": "error",
            "code": "provider_error",
            "mode": "rerank",
            "provider_id": provider_id,
            "model": model,
            "using_default": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": _sanitize_error_message(exc, "重排序模型调用失败"),
        }

    return {
        "status": "ok",
        "code": "ok",
        "mode": "rerank",
        "provider_id": provider_id,
        "model": model,
        "using_default": False,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": "连接正常",
    }


async def probe_setup_providers(
    plugin: Any,
    payload: dict[str, Any],
    *,
    timeout: float = _PROBE_TIMEOUT,
) -> dict[str, Any]:
    """Execute opt-in lightweight connectivity probes for wizard-selected providers."""
    clean = validate_setup_probe_payload(payload)
    context = getattr(plugin, "context", None)

    cfg = getattr(plugin, "_runtime_config", None)
    default_backend = str(getattr(cfg, "decision_backend", "jev") or "jev")
    default_disc_mode = str(getattr(cfg, "discussion_judgment_mode", "rerank") or "rerank")

    backend = clean.get("participation_backend") or default_backend
    if backend not in {"jev", "llm"}:
        backend = "jev"
    decision_provider = clean.get("decision_provider", "")
    decision_llm_provider = clean.get("decision_llm_provider", "")
    reply_provider = clean.get("reply_provider", "")
    probe_discussion = bool(clean.get("probe_discussion", False))
    disc_mode = clean.get("discussion_judgment_mode") or default_disc_mode
    if disc_mode not in {"rerank", "llm"}:
        disc_mode = "rerank"
    disc_reranker = clean.get("discussion_reranker_provider", "")
    disc_llm = clean.get("discussion_llm_provider", "")

    default_chat_resolved = False
    default_chat_pid = ""
    default_chat_prov: Any = None

    async def _get_default_chat() -> tuple[str, Any]:
        nonlocal default_chat_resolved, default_chat_pid, default_chat_prov
        if not default_chat_resolved:
            default_chat_pid, default_chat_prov = await _resolve_default_chat_provider(context)
            default_chat_resolved = True
        return default_chat_pid, default_chat_prov

    async def _resolve_chat_target(explicit_id: str) -> tuple[str, Any, bool]:
        if explicit_id:
            return explicit_id, _get_provider(context, explicit_id), False
        pid, prov = await _get_default_chat()
        return pid, prov, True

    # Build deduplicated chat probe specs keyed by (provider_id, id(provider))
    chat_targets: dict[str, tuple[str, Any, bool]] = {}
    chat_keys: dict[str, tuple[str, int]] = {}

    if backend == "llm":
        target = await _resolve_chat_target(decision_llm_provider or reply_provider)
        chat_targets["decision"] = target
        chat_keys["decision"] = (target[0], id(target[1]))

    reply_target = await _resolve_chat_target(reply_provider)
    chat_targets["reply"] = reply_target
    chat_keys["reply"] = (reply_target[0], id(reply_target[1]))

    if probe_discussion and disc_mode == "llm":
        disc_target = await _resolve_chat_target(disc_llm or reply_provider)
        chat_targets["discussion"] = disc_target
        chat_keys["discussion"] = (disc_target[0], id(disc_target[1]))

    # Schedule unique tasks concurrently.
    unique_chat_tasks: dict[tuple[str, int], asyncio.Task[dict[str, Any]]] = {}
    for slot, key in chat_keys.items():
        if key not in unique_chat_tasks:
            pid, prov, using_default = chat_targets[slot]
            unique_chat_tasks[key] = asyncio.create_task(
                _probe_chat_provider(context, pid, prov, using_default=using_default, timeout=timeout)
            )

    jev_task: asyncio.Task[dict[str, Any]] | None = None
    if backend == "jev":
        jev_task = asyncio.create_task(_probe_jev_provider(context, decision_provider, timeout=timeout))

    rerank_task: asyncio.Task[dict[str, Any]] | None = None
    if probe_discussion and disc_mode == "rerank":
        rerank_task = asyncio.create_task(_probe_rerank_provider(context, disc_reranker, timeout=timeout))

    all_tasks: list[asyncio.Task[dict[str, Any]]] = list(unique_chat_tasks.values())
    if jev_task is not None:
        all_tasks.append(jev_task)
    if rerank_task is not None:
        all_tasks.append(rerank_task)

    if all_tasks:
        await asyncio.gather(*all_tasks)

    chat_results = {key: task.result() for key, task in unique_chat_tasks.items()}

    if backend == "jev":
        decision_result = jev_task.result() if jev_task is not None else {
            "status": "skipped",
            "code": "skipped",
            "backend": "jev",
            "provider_id": "",
            "model": "",
            "using_default": False,
            "latency_ms": 0,
            "message": "未选择决策模型",
        }
    else:
        base = dict(chat_results[chat_keys["decision"]])
        base["backend"] = "llm"
        base["using_default"] = chat_targets["decision"][2]
        decision_result = base

    reply_result = dict(chat_results[chat_keys["reply"]])
    reply_result["using_default"] = chat_targets["reply"][2]

    discussion_result: dict[str, Any] | None = None
    if probe_discussion:
        if disc_mode == "rerank" and rerank_task is not None:
            discussion_result = rerank_task.result()
        elif disc_mode == "llm":
            base = dict(chat_results[chat_keys["discussion"]])
            base["mode"] = "llm"
            base["using_default"] = chat_targets["discussion"][2]
            discussion_result = base

    results = {
        "decision": decision_result,
        "reply": reply_result,
        "discussion": discussion_result,
    }
    overall_ok = all(
        slot["status"] != "error"
        for slot in results.values()
        if slot is not None
    )
    return {
        "ok": overall_ok,
        "results": results,
    }
