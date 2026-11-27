"""Read-only context readers for dynamics_games and group_chronicle companions.

Discovers companion plugins dynamically via AstrBot's star registry with strict
timeouts and bounded payloads; never imports companion modules directly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from .catalog import find_companion

_CALL_TIMEOUT = 0.35
_MAX_GAME_SUMMARY_CHARS = 360
_MAX_CULTURE_SUMMARY_CHARS = 480

GAMES_DECISION_GUIDANCE = (
    "character_state.active_game 表示本群正在进行群游戏或协作活动：此时处于陪玩态，"
    "请降低无关闲聊抢话、避免误打断游戏指令和玩家轮次；仅在被明确呼唤或游戏间隙自然捧场时简短接话。"
)

GAMES_REPLY_GUIDANCE = (
    "group_game 是本群当前进行中的游戏或协作活动状态，仅作背景参考；"
    "切勿替玩家擅自出牌、提交或编造未发生的游戏结果，保持轻松陪玩态度即可。"
)

CULTURE_REPLY_GUIDANCE = (
    "group_culture 是本群已审核通过的群梗与词条释义，用于听懂群友黑话并自然接梗；"
    "不要生硬背诵词典释义，也不要强行塞梗。"
)


async def _invoke_bounded(fn: Any, *args: Any, timeout: float = _CALL_TIMEOUT, **kwargs: Any) -> Any:
    if not callable(fn):
        return None
    try:
        if inspect.iscoroutinefunction(fn):
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
        res = fn(*args, **kwargs)
        if inspect.isawaitable(res):
            return await asyncio.wait_for(res, timeout=timeout)
        return res
    except (asyncio.TimeoutError, TimeoutError, Exception):
        return None


def _call_with_compatible_args(fn: Any, umo: str, *, query: str = "", limit: int = 6):
    """Invoke a companion getter adapting cleanly to varying parameter signatures."""
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs: dict[str, Any] = {}
        if has_var_kw or "query" in params:
            kwargs["query"] = query
        if has_var_kw or "limit" in params:
            kwargs["limit"] = limit
        if "umo" in params:
            kwargs["umo"] = umo
            return _invoke_bounded(fn, **kwargs)
        if "group_id" in params:
            gid = umo.rsplit(":", 1)[-1] if ":" in umo else umo
            kwargs["group_id"] = gid
            return _invoke_bounded(fn, **kwargs)
        return _invoke_bounded(fn, umo, **kwargs)
    except (TypeError, ValueError):
        return _invoke_bounded(fn, umo)


def _normalize_game_state(active_raw: Any, activity_raw: Any) -> dict[str, Any]:
    active = False
    engines: list[str] = []
    activities: list[dict[str, Any]] = []
    companion_mode_hint = ""
    summary_parts: list[str] = []

    if isinstance(active_raw, dict):
        active = bool(active_raw.get("active", False))
        for eng in (active_raw.get("engines") or ())[:8]:
            clean = str(eng or "").strip()[:48]
            if clean and clean not in engines:
                engines.append(clean)
        companion_mode_hint = str(active_raw.get("companion_mode_hint") or "").strip()[:80]
        for act in (active_raw.get("activities") or ())[:4]:
            if isinstance(act, dict):
                title = str(act.get("title") or act.get("name") or "").strip()[:80]
                if title:
                    activities.append(
                        {
                            "title": title,
                            "category": str(act.get("category") or "")[:32],
                            "phase": str(act.get("phase") or act.get("status") or "")[:40],
                        }
                    )
        if active_raw.get("summary"):
            summary_parts.append(str(active_raw["summary"]).strip()[:200])

    if isinstance(activity_raw, dict):
        if activity_raw.get("active") is True:
            active = True
        raw_acts = activity_raw.get("activities") or activity_raw.get("items") or ()
        if isinstance(raw_acts, (list, tuple)):
            existing_titles = {a["title"] for a in activities}
            for act in raw_acts[:4]:
                if not isinstance(act, dict):
                    continue
                title = str(act.get("title") or act.get("name") or "").strip()[:80]
                status = str(act.get("status") or act.get("phase") or "").strip()[:40]
                if status.lower() in {"active", "ongoing", "running", "in_progress", "playing"}:
                    active = True
                if title and title not in existing_titles:
                    existing_titles.add(title)
                    activities.append(
                        {
                            "title": title,
                            "category": str(act.get("category") or "")[:32],
                            "phase": status,
                        }
                    )
        text_summary = str(activity_raw.get("summary") or activity_raw.get("context") or "").strip()
        if text_summary and text_summary not in summary_parts:
            summary_parts.append(text_summary[:240])
    elif isinstance(activity_raw, str) and activity_raw.strip():
        summary_parts.append(activity_raw.strip()[:240])

    if engines and not active:
        active = True

    if not summary_parts:
        if engines:
            summary_parts.append(f"进行中游戏引擎：{', '.join(engines)}")
        if activities:
            summary_parts.append(
                "协作活动：" + "、".join(a["title"] for a in activities[:3])
            )

    summary = "；".join(p for p in summary_parts if p)[:_MAX_GAME_SUMMARY_CHARS]
    return {
        "active": bool(active),
        "engines": engines,
        "activities": activities[:4],
        "companion_mode_hint": companion_mode_hint or ("accompany_quietly" if active else ""),
        "summary": summary,
    }


def _normalize_culture_context(raw: Any, *, limit: int = 6) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    summary = ""

    items: list[Any] = []
    if isinstance(raw, dict):
        summary = str(raw.get("summary") or raw.get("context") or "").strip()[:_MAX_CULTURE_SUMMARY_CHARS]
        for key in ("entries", "items", "terms", "memes", "glossary"):
            val = raw.get(key)
            if isinstance(val, (list, tuple)):
                items = list(val)
                break
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str) and raw.strip():
        summary = raw.strip()[:_MAX_CULTURE_SUMMARY_CHARS]

    for item in items:
        if len(entries) >= max(1, min(limit, 8)):
            break
        if isinstance(item, str):
            clean = item.strip()[:120]
            if clean:
                entries.append({"term": clean, "meaning": ""})
            continue
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status and status not in {"approved", "active", "published"}:
            continue
        term = str(item.get("term") or item.get("title") or item.get("name") or item.get("phrase") or "").strip()[:60]
        meaning = str(
            item.get("meaning") or item.get("summary") or item.get("definition") or item.get("description") or ""
        ).strip()[:160]
        if not term and not meaning:
            continue
        row = {"term": term, "meaning": meaning}
        usage = str(item.get("usage") or item.get("example") or "").strip()[:100]
        if usage:
            row["usage"] = usage
        entries.append(row)

    if not summary and entries:
        summary = "；".join(
            f"{e['term']}（{e['meaning']}）" if e.get("meaning") else e["term"]
            for e in entries[:4]
        )[:_MAX_CULTURE_SUMMARY_CHARS]

    return {
        "entries": entries,
        "summary": summary,
    }


async def read_game_context(host: Any, umo: str, *, query: str = "") -> dict[str, Any]:
    """Read bounded active game & group activity state from dynamics_games."""
    clean_umo = str(umo or "").strip()
    if host is None or not clean_umo:
        return {"active": False, "engines": [], "activities": [], "companion_mode_hint": "", "summary": ""}
    context = getattr(host, "context", host)
    companion = find_companion(context, "dynamics_games")
    if companion is None:
        return {"active": False, "engines": [], "activities": [], "companion_mode_hint": "", "summary": ""}

    active_fn = getattr(companion, "get_active_game_state", None)
    activity_fn = getattr(companion, "get_group_activity_context", None)
    active_raw = await _call_with_compatible_args(active_fn, clean_umo, query=query, limit=4) if callable(active_fn) else None
    activity_raw = await _call_with_compatible_args(activity_fn, clean_umo, query=query, limit=4) if callable(activity_fn) else None
    return _normalize_game_state(active_raw, activity_raw)


async def read_culture_context(host: Any, umo: str, *, query: str = "", limit: int = 6) -> dict[str, Any]:
    """Read approved group culture/meme glossary entries from group_chronicle."""
    clean_umo = str(umo or "").strip()
    if host is None or not clean_umo:
        return {"entries": [], "summary": ""}
    context = getattr(host, "context", host)
    companion = find_companion(context, "group_chronicle")
    if companion is None:
        return {"entries": [], "summary": ""}

    culture_fn = getattr(companion, "get_culture_context", None)
    if not callable(culture_fn):
        return {"entries": [], "summary": ""}
    raw = await _call_with_compatible_args(culture_fn, clean_umo, query=query, limit=limit)
    return _normalize_culture_context(raw, limit=limit)


def augment_reply_prompt(
    prompt: str,
    *,
    game_context: dict[str, Any] | None = None,
    culture_context: dict[str, Any] | None = None,
) -> str:
    """Attach bounded group_game and group_culture sections to a JSON reply prompt."""
    has_game = bool(
        isinstance(game_context, dict)
        and (game_context.get("active") or game_context.get("activities") or game_context.get("summary"))
    )
    has_culture = bool(
        isinstance(culture_context, dict)
        and (culture_context.get("entries") or culture_context.get("summary"))
    )
    if not has_game and not has_culture:
        return prompt
    try:
        payload = json.loads(prompt)
    except (ValueError, TypeError):
        return prompt
    if not isinstance(payload, dict):
        return prompt
    if has_game and isinstance(game_context, dict):
        payload["group_game"] = {
            "active": bool(game_context.get("active")),
            "engines": list(game_context.get("engines") or [])[:6],
            "activities": list(game_context.get("activities") or [])[:4],
            "companion_mode_hint": str(game_context.get("companion_mode_hint") or "")[:80],
            "summary": str(game_context.get("summary") or "")[:_MAX_GAME_SUMMARY_CHARS],
            "guidance": GAMES_REPLY_GUIDANCE,
        }
    if has_culture and isinstance(culture_context, dict):
        payload["group_culture"] = {
            "entries": list(culture_context.get("entries") or [])[:6],
            "summary": str(culture_context.get("summary") or "")[:_MAX_CULTURE_SUMMARY_CHARS],
            "guidance": CULTURE_REPLY_GUIDANCE,
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
