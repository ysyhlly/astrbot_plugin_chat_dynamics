"""Versioned Dynamics Horizons boundary. No disk/network/model work on the reply path."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import weakref
from copy import deepcopy
from typing import Any

from .catalog import find_companion
from ..private_original_input import is_private_message_event, private_message_recipient
from ..social_opportunity import social_boundary_allows

DECISION_SLOT_MAX_CHARS = 500
REPLY_PROJECTION_MAX_CHARS = 1800
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_FORBIDDEN_URL_KEYS = frozenset({"raw_url", "url", "href", "source_url", "link"})
_ALLOWED_CITATION_KEYS = ("title", "creator", "bvid", "chapter_label")

HORIZONS_DECISION_GUIDANCE = (
    "character_state.horizons 表示角色最近自主阅读或观看过的外部见闻话题与可选待聊问题（open_question）；"
    "当本轮发言确实聊到相关话题，或群友主动询问角色最近在看什么、读什么、有什么有趣或有意思的话题时，可自然倾向接话分享；"
    "若当前聊天与 horizons 无关，绝不强行把话题往见闻上引或借此抢话。"
)

HORIZONS_REPLY_GUIDANCE = (
    'horizons（fact_kind="external_media"）是角色看过或读过的外部作品笔记与待讨论问题库（questions），感悟只供参考。'
    "先回应当前对话。平时契合时可用角色自己的话融入一个想法，别照抄笔记，也无需以‘刚看了’起头或强行追问。"
    "当群友主动询问最近有什么有趣/有意思的话题、想找点事聊、或在看什么读什么时，可直接从 horizons.questions 或近期见闻中挑 1-2 个自然展开；若当前上下文未附带见闻且可用 query_horizons_topics 工具，可主动调用查询问题库。"
    "保留角色原有性格。笔记里的类比、想象或打算不能当作角色已做过的事、既定承诺或群友的真实经历。"
    "转述作品观点或情节时交代其来自外部作品。digest 和 reflection 是摘要和感悟，不能当作作品原句引用。"
    "有人明确问读了什么或出处时，按阅读记录和 citation 中已核验的信息回答。"
    "平时不主动报完整标题、书名章节或 BV 号，不编造出处，不发 http:// 或 https:// 原始链接。"
)

_PENDING_FEEDBACK_TASKS: set[asyncio.Task[Any]] = set()


def _strip_urls_text(value: Any, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _URL_RE.sub("", value)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned[:limit]


def _sanitize_value(value: Any, *, str_limit: int = 600) -> Any:
    """Recursively strip raw HTTP/HTTPS URLs and drop URL-bearing keys."""
    if isinstance(value, str):
        return _strip_urls_text(value, limit=str_limit)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in _FORBIDDEN_URL_KEYS:
                continue
            if key == "citation":
                if isinstance(v, dict):
                    cite = {
                        ck: _strip_urls_text(v.get(ck, ""), limit=160)
                        for ck in _ALLOWED_CITATION_KEYS
                        if isinstance(v.get(ck), str) and _strip_urls_text(v.get(ck, ""), limit=160)
                    }
                    if cite:
                        out["citation"] = cite
                continue
            sanitized = _sanitize_value(v, str_limit=str_limit)
            if sanitized is not None and sanitized != "" and sanitized != {} and sanitized != []:
                out[key] = sanitized
            elif isinstance(v, bool):
                out[key] = v
        return out
    if isinstance(value, (list, tuple)):
        items = [_sanitize_value(item, str_limit=str_limit) for item in value[:16]]
        return [item for item in items if item is not None and item != "" and item != {} and item != []]
    if isinstance(value, (int, float, bool)) and not (isinstance(value, float) and (value != value)):
        return value
    return None


def _json_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def _ref(obj: Any):
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda _obj=obj: _obj


def _bound_decision_slot(raw_slot: Any) -> dict[str, Any] | None:
    if not isinstance(raw_slot, dict) or not raw_slot:
        return None
    slot = _sanitize_value(raw_slot, str_limit=280)
    if not isinstance(slot, dict) or not slot:
        return None
    slot.setdefault("fact_kind", "external_media")
    if _json_len(slot) <= DECISION_SLOT_MAX_CHARS:
        return slot
    # Gracefully trim optional fields so valid slots fit the 500-char wire contract.
    if isinstance(slot.get("topics"), list):
        while len(slot["topics"]) > 2 and _json_len(slot) > DECISION_SLOT_MAX_CHARS:
            slot["topics"].pop()
    for list_key in ("recent_readings", "interests"):
        if isinstance(slot.get(list_key), list):
            while len(slot[list_key]) > 1 and _json_len(slot) > DECISION_SLOT_MAX_CHARS:
                slot[list_key].pop()
            for entry in slot[list_key]:
                if isinstance(entry, dict):
                    for k in ("summary", "reflection", "digest"):
                        if isinstance(entry.get(k), str) and len(entry[k]) > 80 and _json_len(slot) > DECISION_SLOT_MAX_CHARS:
                            entry[k] = entry[k][:80].rstrip()
    for text_key in ("open_question", "interest", "recent_reading", "summary", "digest", "reflection", "hint"):
        if isinstance(slot.get(text_key), str) and _json_len(slot) > DECISION_SLOT_MAX_CHARS:
            slot[text_key] = slot[text_key][:100].rstrip()
    if _json_len(slot) > DECISION_SLOT_MAX_CHARS and "recent_readings" in slot:
        slot.pop("recent_readings", None)
    if _json_len(slot) > DECISION_SLOT_MAX_CHARS and "recent_reading" in slot:
        slot.pop("recent_reading", None)
    if _json_len(slot) > DECISION_SLOT_MAX_CHARS and "open_question" in slot:
        slot.pop("open_question", None)
    for text_key in ("interest", "summary", "digest", "reflection", "hint"):
        while isinstance(slot.get(text_key), str) and len(slot[text_key]) > 24 and _json_len(slot) > DECISION_SLOT_MAX_CHARS:
            slot[text_key] = slot[text_key][:-24].rstrip()
    if isinstance(slot.get("interests"), list) and _json_len(slot) > DECISION_SLOT_MAX_CHARS:
        for entry in slot["interests"]:
            if isinstance(entry, dict) and isinstance(entry.get("summary"), str):
                entry["summary"] = entry["summary"][:48].rstrip()
    return slot if _json_len(slot) <= DECISION_SLOT_MAX_CHARS else None


def _bound_reply_projection(raw_proj: Any) -> dict[str, Any] | None:
    if not isinstance(raw_proj, dict) or not raw_proj:
        return None
    proj = _sanitize_value(raw_proj, str_limit=500)
    if not isinstance(proj, dict) or not proj:
        return None
    proj["fact_kind"] = "external_media"
    for list_key in ("items", "readings"):
        if isinstance(proj.get(list_key), list):
            for entry in proj[list_key]:
                if isinstance(entry, dict):
                    entry["fact_kind"] = "external_media"
    if _json_len(proj) <= REPLY_PROJECTION_MAX_CHARS:
        return proj
    # Trim excess questions/readings/items first, then clip long reflections/digests.
    if isinstance(proj.get("questions"), list):
        while len(proj["questions"]) > 1 and _json_len(proj) > REPLY_PROJECTION_MAX_CHARS:
            proj["questions"].pop()
    for list_key in ("items", "readings", "interests"):
        if isinstance(proj.get(list_key), list):
            while len(proj[list_key]) > 1 and _json_len(proj) > REPLY_PROJECTION_MAX_CHARS:
                proj[list_key].pop()
            for entry in proj[list_key]:
                if not isinstance(entry, dict):
                    continue
                for field in ("reflection", "digest", "summary", "text"):
                    if isinstance(entry.get(field), str) and _json_len(proj) > REPLY_PROJECTION_MAX_CHARS:
                        entry[field] = entry[field][:200].rstrip()
    if _json_len(proj) > REPLY_PROJECTION_MAX_CHARS and isinstance(proj.get("questions"), list):
        proj.pop("questions", None)
    for list_key in ("items", "readings", "interests"):
        if isinstance(proj.get(list_key), list):
            for entry in proj[list_key]:
                if not isinstance(entry, dict):
                    continue
                for field in ("reflection", "digest", "summary", "text"):
                    while isinstance(entry.get(field), str) and len(entry[field]) > 32 and _json_len(proj) > REPLY_PROJECTION_MAX_CHARS:
                        entry[field] = entry[field][:-32].rstrip()
    return proj if _json_len(proj) <= REPLY_PROJECTION_MAX_CHARS else None


def _empty_selection() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "matched": False,
        "match_reason": "",
        "decision_slot": None,
        "reply_projection": None,
        "versions": [],
        "question_ids": [],
        "topics": [],
    }


def provider(host: Any) -> Any:
    """Resolve active, ready Dynamics Horizons companion synchronously."""
    try:
        context = getattr(host, "context", None) if host is not None else None
        current = find_companion(context, "dynamics_horizons")
        if current is None:
            return None
        caps_fn = getattr(current, "horizons_capabilities", None)
        if not callable(caps_fn) or inspect.iscoroutinefunction(caps_fn):
            return None
        caps = caps_fn()
        if (
            not isinstance(caps, dict)
            or type(caps.get("schema_version")) is not int
            or caps.get("schema_version") != 1
            or caps.get("ready") is not True
            or caps.get("enabled", True) is False
        ):
            return None
        if not callable(getattr(current, "get_horizons_context", None)):
            return None
        return current
    except Exception:
        return None


def validate_horizons_packet(result: Any) -> dict[str, Any]:
    """Validate synchronous get_horizons_context wire response and strip any raw URLs."""
    if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result.get("schema_version") != 1:
        raise ValueError("invalid horizons schema_version")
    matched = result.get("matched")
    if not isinstance(matched, bool):
        raise ValueError("invalid horizons matched flag")
    if not matched:
        return _empty_selection()
    raw_versions = result.get("versions", [])
    versions = [
        str(v)[:128]
        for v in (raw_versions if isinstance(raw_versions, (list, tuple)) else ())[:32]
        if isinstance(v, (str, int)) and str(v)
    ]
    d_slot = _bound_decision_slot(result.get("decision_slot"))
    r_proj = _bound_reply_projection(result.get("reply_projection"))
    if d_slot is None and r_proj is None:
        return _empty_selection()
    question_ids: list[str] = []
    topics: list[str] = []
    if isinstance(d_slot, dict):
        qid = d_slot.get("question_id")
        if isinstance(qid, str) and qid.strip():
            question_ids.append(qid.strip()[:128])
        open_q = d_slot.get("open_question")
        if isinstance(open_q, dict):
            oq_id = open_q.get("question_id")
            if isinstance(oq_id, str) and oq_id.strip() and oq_id.strip()[:128] not in question_ids:
                question_ids.append(oq_id.strip()[:128])
            oq_t = open_q.get("topic")
            if isinstance(oq_t, str) and oq_t.strip() and oq_t.strip()[:80] not in topics:
                topics.append(oq_t.strip()[:80])
        for t in d_slot.get("topics", ()) if isinstance(d_slot.get("topics"), list) else ():
            if isinstance(t, str) and t.strip() and t.strip()[:80] not in topics:
                topics.append(t.strip()[:80])
        for entry in d_slot.get("interests", ()) if isinstance(d_slot.get("interests"), list) else ():
            if isinstance(entry, dict) and isinstance(entry.get("topic"), str) and entry["topic"].strip():
                if entry["topic"].strip()[:80] not in topics:
                    topics.append(entry["topic"].strip()[:80])
    if isinstance(r_proj, dict):
        for qid in r_proj.get("question_ids", ()) if isinstance(r_proj.get("question_ids"), list) else ():
            if isinstance(qid, str) and qid.strip() and qid.strip()[:128] not in question_ids:
                question_ids.append(qid.strip()[:128])
        for t in r_proj.get("topics", ()) if isinstance(r_proj.get("topics"), list) else ():
            if isinstance(t, str) and t.strip() and t.strip()[:80] not in topics:
                topics.append(t.strip()[:80])
        for q in r_proj.get("questions", ()) if isinstance(r_proj.get("questions"), list) else ():
            if isinstance(q, dict):
                qid = q.get("question_id") or q.get("id")
                if isinstance(qid, str) and qid.strip() and qid.strip()[:128] not in question_ids:
                    question_ids.append(qid.strip()[:128])
                q_topic = q.get("topic")
                if isinstance(q_topic, str) and q_topic.strip() and q_topic.strip()[:80] not in topics:
                    topics.append(q_topic.strip()[:80])
        for list_key in ("items", "readings", "interests"):
            for entry in r_proj.get(list_key, ()) if isinstance(r_proj.get(list_key), list) else ():
                if isinstance(entry, dict):
                    for t in entry.get("topics", ()) if isinstance(entry.get("topics"), list) else ():
                        if isinstance(t, str) and t.strip() and t.strip()[:80] not in topics:
                            topics.append(t.strip()[:80])
                    e_topic = entry.get("topic")
                    if isinstance(e_topic, str) and e_topic.strip() and e_topic.strip()[:80] not in topics:
                        topics.append(e_topic.strip()[:80])
    return {
        "schema_version": 1,
        "matched": True,
        "match_reason": _strip_urls_text(result.get("match_reason", ""), limit=80),
        "decision_slot": d_slot,
        "reply_projection": r_proj,
        "versions": versions,
        "log_ids": [r['log_id'] for r in (r_proj or {}).get('readings',[]) if isinstance(r,dict) and str(r.get('log_id','')).startswith('log_')],
        "question_ids": question_ids[:8],
        "topics": topics[:12],
    }


def _build_nearby_text(turn: Any, limit: int = 600) -> str:
    parts: list[str] = []
    for msg in getattr(turn, "nearby_context", ()) or ():
        txt = str(getattr(msg, "text", "") or "").strip()
        if txt:
            parts.append(txt)
    for msg in getattr(turn, "background", ()) or ():
        txt = str(getattr(msg, "text", "") or "").strip()
        if txt:
            parts.append(txt)
    if not parts:
        return ""
    joined = "\n".join(parts[-6:])
    return joined[-limit:]


def _resolve_character_scope(host: Any, turn: Any, persona: Any, item: Any = None) -> dict[str, str]:
    bundle = getattr(item, "narrative_bundle", None) if item is not None else None
    if bundle is not None:
        try:
            wire_scope = getattr(bundle, "scope", None)
            if not isinstance(wire_scope, dict) and callable(getattr(bundle, "wire", None)):
                wire_scope = bundle.wire().get("scope")
            if isinstance(wire_scope, dict) and wire_scope.get("umo") == turn.session_key:
                return {
                    k: str(wire_scope.get(k) or "")
                    for k in ("platform", "umo", "persona_id", "instance_id")
                }
        except Exception:
            pass
    umo = str(getattr(turn, "session_key", "") or "")
    platform = umo.rsplit(":", 2)[0] if ":" in umo else ""
    persona_id = str(getattr(persona, "persona_id", "") or "")
    instance_id = persona_id or str(getattr(host, "instance_id", "") or "default")
    return {
        "platform": platform,
        "umo": umo,
        "persona_id": persona_id,
        "instance_id": instance_id,
    }


def read(
    current: Any,
    *,
    scope: str,
    persona: str = "",
    member: str = "",
    query: str = "",
    nearby_text: str = "",
    character_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke synchronous in-memory get_horizons_context with strict wire validation."""
    if current is None or not scope:
        return _empty_selection()
    try:
        fn = getattr(current, "get_horizons_context", None)
        if not callable(fn) or inspect.iscoroutinefunction(fn):
            return _empty_selection()
        kwargs: dict[str, Any] = {
            "scope": str(scope),
            "persona": str(persona or ""),
            "member": str(member or ""),
            "query": str(query or ""),
            "nearby_text": str(nearby_text or ""),
            "character_scope": dict(character_scope) if isinstance(character_scope, dict) else None,
        }
        try:
            sig = inspect.signature(fn)
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            return _empty_selection()
        return validate_horizons_packet(result)
    except Exception:
        return _empty_selection()


class HorizonsTurnCapture:
    """Per-turn immutable capture for Dynamics Horizons projection and delivery feedback."""

    def __init__(self, host: Any, runtime: Any, item: Any, persona: Any, current: Any):
        self.host = _ref(host)
        self.runtime = _ref(runtime)
        self.source = _ref(current)
        turn = item.context
        self.epoch = getattr(runtime, "epoch", getattr(turn, "epoch", 0))
        self.umo = str(getattr(turn, "session_key", "") or "")
        self.member = str(getattr(turn, "author", "") or "")
        self.persona_id = str(getattr(persona, "persona_id", "") or "")
        self.text = str(
            turn.source_text if getattr(turn, "source_text", None) is not None else getattr(turn, "text", "") or ""
        )[:2000]
        self.nearby_text = _build_nearby_text(turn)
        self.character_scope = _resolve_character_scope(host, turn, persona, item)
        last_msg = turn.messages[-1] if getattr(turn, "messages", ()) else None
        msg_id = str(getattr(last_msg, "message_id", "") or "turn")
        self.turn_id = f"{self.epoch}:{msg_id}"
        self.selected = read(
            current,
            scope=self.umo,
            persona=self.persona_id,
            member=self.member,
            query=str(getattr(turn, "text", "") or ""),
            nearby_text=self.nearby_text,
            character_scope=self.character_scope,
        )
        self._feedback_sent = False

    def is_current(self) -> bool:
        host, current = self.host(), self.source()
        if host is None or current is None:
            return False
        return provider(host) is current


def _key(item: Any) -> tuple[Any, ...]:
    turn = item.context
    last_id = turn.messages[-1].message_id if getattr(turn, "messages", ()) else ""
    return (turn.epoch, turn.author, turn.revision, last_id)


def _store(runtime: Any) -> dict[tuple[Any, ...], HorizonsTurnCapture]:
    if runtime is None:
        return {}
    table = getattr(runtime, "_dynamics_horizons_captures", None)
    if table is None:
        table = {}
        runtime._dynamics_horizons_captures = table
    return table


def begin(host: Any, runtime: Any, item: Any, persona: Any) -> HorizonsTurnCapture | None:
    """Capture synchronous in-memory horizons context at turn start."""
    if host is None or runtime is None or item is None:
        return None
    current = provider(host)
    table = _store(runtime)
    if current is None:
        table.pop(_key(item), None)
        return None
    capture = HorizonsTurnCapture(host, runtime, item, persona, current)
    table[_key(item)] = capture
    while len(table) > 64:
        table.pop(next(iter(table)))
    return capture


def capture_for(runtime: Any, item: Any) -> HorizonsTurnCapture | None:
    if runtime is None or item is None:
        return None
    return _store(runtime).get(_key(item))


def selected(runtime: Any, item: Any) -> dict[str, Any]:
    capture = capture_for(runtime, item)
    if capture is None or not capture.is_current():
        return _empty_selection()
    return deepcopy(capture.selected)


def decision_slot(runtime: Any, item: Any) -> dict[str, Any] | None:
    sel = selected(runtime, item)
    if not sel.get("matched"):
        return None
    slot = sel.get("decision_slot")
    return deepcopy(slot) if isinstance(slot, dict) and slot else None


def reply_projection(runtime: Any, item: Any) -> dict[str, Any] | None:
    sel = selected(runtime, item)
    if not sel.get("matched"):
        return None
    proj = sel.get("reply_projection")
    return deepcopy(proj) if isinstance(proj, dict) and proj else None


def augment_reply(prompt: str, runtime: Any, item: Any) -> str:
    """Attach matched horizons reply_projection to reply JSON prompt; zero-cost no-op when unmatched."""
    proj = reply_projection(runtime, item)
    if not proj:
        return prompt
    try:
        payload = json.loads(prompt)
    except Exception:
        return prompt
    if not isinstance(payload, dict):
        return prompt
    payload["_horizons_v1"] = proj
    return json.dumps(payload, ensure_ascii=False)


async def _invoke_ingest_async(target: Any, event_payload: dict[str, Any]) -> None:
    try:
        fn = getattr(target, "ingest_dynamics_event", None)
        if not callable(fn):
            return
        if inspect.iscoroutinefunction(fn):
            await fn(deepcopy(event_payload))
            return
        result = await asyncio.to_thread(fn, deepcopy(event_payload))
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


def _schedule_ingest(host: Any, target: Any, event_payload: dict[str, Any]) -> None:
    fn = getattr(target, "ingest_dynamics_event", None)
    if not callable(fn):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        coro = _invoke_ingest_async(target, event_payload)
        creator = getattr(host, "_create_background_task", None)
        task = creator(coro) if callable(creator) else loop.create_task(coro)
        if isinstance(task, asyncio.Task):
            _PENDING_FEEDBACK_TASKS.add(task)
            task.add_done_callback(_PENDING_FEEDBACK_TASKS.discard)
        return
    # Synchronous fallback when invoked outside an active event loop (e.g. sync unit tests).
    try:
        res = fn(deepcopy(event_payload))
        if inspect.iscoroutine(res):
            res.close()
    except Exception:
        return


async def flush_pending_feedback() -> None:
    """Await any in-flight asynchronous feedback tasks (used by tests/shutdown)."""
    while _PENDING_FEEDBACK_TASKS:
        batch = tuple(_PENDING_FEEDBACK_TASKS)
        await asyncio.gather(*batch, return_exceptions=True)


def delivered(
    host: Any,
    runtime: Any,
    item: Any,
    reply_text: str,
    *,
    message_id: str = "",
    message_ids: list[str] | tuple[str, ...] = (),
) -> None:
    """Forward non-blocking delivery.success feedback once per completed turn."""
    if host is None or runtime is None or item is None or getattr(host, "_shutting_down", False):
        return
    capture = capture_for(runtime, item)
    current = provider(host)
    if current is None:
        return
    if capture is not None:
        if capture._feedback_sent or getattr(runtime, "epoch", capture.epoch) != capture.epoch:
            return
        if capture.source() is not current:
            return
        capture._feedback_sent = True
        sel = capture.selected
        umo = capture.umo
        member = capture.member
        persona_id = capture.persona_id
        text = capture.text
        turn_id = capture.turn_id
        character_scope = dict(capture.character_scope)
    else:
        turn = item.context
        umo = str(getattr(turn, "session_key", "") or "")
        member = str(getattr(turn, "author", "") or "")
        persona_id = ""
        text = str(getattr(turn, "text", "") or "")[:2000]
        last_id = turn.messages[-1].message_id if getattr(turn, "messages", ()) else "turn"
        turn_id = f"{getattr(runtime, 'epoch', 0)}:{last_id}"
        character_scope = _resolve_character_scope(host, turn, None, item)
        sel = _empty_selection()
    turn = item.context
    turn_messages = getattr(turn, "messages", ())
    source_message_id = (
        str(getattr(turn_messages[-1], "message_id", "") or "")[:128]
        if turn_messages
        else ""
    )
    if not umo:
        return
    clean_reply = str(reply_text or "")[:2000]
    reply_message_ids: list[str] = []
    for raw_id in (*message_ids, message_id):
        clean_id = str(raw_id or "").strip()[:128]
        if clean_id and clean_id not in reply_message_ids:
            reply_message_ids.append(clean_id)
        if len(reply_message_ids) >= 64:
            break
    merged_log_ids = [str(v)[:128] for v in sel.get("log_ids", ()) if str(v or "").strip()][:4]
    merged_qids = [str(v)[:128] for v in sel.get("question_ids", ()) if str(v or "").strip()][:8]
    merged_topics = [str(v)[:80] for v in sel.get("topics", ()) if str(v or "").strip()][:12]
    tool_matched = False
    for ev in getattr(item, "events", ()) or ():
        tool_ctx = getattr(ev, "_horizons_tool_context_v1", None)
        if not isinstance(tool_ctx, dict):
            continue
        for val in tool_ctx.get("log_ids", ()) or ():
            s = str(val or "").strip()[:128]
            if s and s not in merged_log_ids and len(merged_log_ids) < 4:
                merged_log_ids.append(s)
                tool_matched = True
        for val in tool_ctx.get("question_ids", ()) or ():
            s = str(val or "").strip()[:128]
            if s and s not in merged_qids and len(merged_qids) < 8:
                merged_qids.append(s)
                tool_matched = True
        for val in tool_ctx.get("topics", ()) or ():
            s = str(val or "").strip()[:80]
            if s and s not in merged_topics and len(merged_topics) < 12:
                merged_topics.append(s)
                tool_matched = True
    event_payload = {
        "schema_version": 1,
        "kind": "delivery.success",
        "event_type": "delivery.success",
        "delivered": True,
        "turn_id": turn_id,
        "source_message_id": source_message_id,
        "umo": umo,
        "scope": umo,
        "persona": persona_id,
        "character_scope": character_scope,
        "member": member,
        "text": text,
        "reply_text": clean_reply,
        "reply": {
            "message_id": str(message_id or "")[:128],
            "text": clean_reply,
            "delivered": True,
        },
        "reply_message_ids": reply_message_ids,
        "matched": bool(sel.get("matched") or tool_matched),
        "managed": True,
        "log_ids": merged_log_ids,
        "question_ids": merged_qids,
        "topics": merged_topics,
        "versions": list(sel.get("versions", ())),
        "timestamp": time.time(),
    }
    if private_message_recipient(umo):
        if private_message_recipient(umo) != member or not item.events:
            return
        _schedule_member_scope_ingest(host, current, event_payload, item.events[-1])
        return
    _schedule_ingest(host, current, event_payload)


def notify_proactive_delivered(
    host: Any,
    *,
    umo: str,
    intent: dict[str, Any],
    reply_text: str,
    message_id: str = "",
    character_scope: dict[str, Any] | None = None,
) -> None:
    """Forward delivery.success feedback when a proactive horizon: intent is delivered."""
    current = provider(host)
    if current is None or not umo:
        return
    qids: list[str] = []
    for key in ("question_ids",):
        raw_q = intent.get(key)
        if isinstance(raw_q, (list, tuple)):
            for q in raw_q:
                if isinstance(q, str) and q.strip() and q.strip()[:128] not in qids:
                    qids.append(q.strip()[:128])
    single_q = intent.get("question_id")
    if isinstance(single_q, str) and single_q.strip() and single_q.strip()[:128] not in qids:
        qids.append(single_q.strip()[:128])
    topics: list[str] = []
    raw_topics = intent.get("topics")
    if isinstance(raw_topics, (list, tuple)):
        for t in raw_topics:
            if isinstance(t, str) and t.strip() and t.strip()[:80] not in topics:
                topics.append(t.strip()[:80])
    single_t = intent.get("topic")
    if isinstance(single_t, str) and single_t.strip() and single_t.strip()[:80] not in topics:
        topics.append(single_t.strip()[:80])
    log_ids: list[str] = []
    raw_log_ids = intent.get("log_ids")
    if isinstance(raw_log_ids, (list, tuple)):
        for log_id in raw_log_ids:
            if isinstance(log_id, str) and log_id.strip() and log_id.strip()[:128] not in log_ids:
                log_ids.append(log_id.strip()[:128])
    single_log_id = intent.get("log_id")
    if isinstance(single_log_id, str) and single_log_id.strip() and single_log_id.strip()[:128] not in log_ids:
        log_ids.append(single_log_id.strip()[:128])
    clean_reply = str(reply_text or "")[:2000]
    recipient = str(intent.get("recipient_user_id") or intent.get("member_id") or "")
    event_payload = {
        "schema_version": 1,
        "kind": "delivery.success",
        "event_type": "delivery.success",
        "delivered": True,
        "proactive": True,
        "intent_id": str(intent.get("intent_id") or "")[:160],
        "turn_id": f"intent:{intent.get('intent_id', '')}",
        "umo": str(umo),
        "scope": str(umo),
        "character_scope": dict(character_scope) if isinstance(character_scope, dict) else {"umo": str(umo)},
        "member": recipient,
        "text": "",
        "reply_text": clean_reply,
        "reply": {
            "message_id": str(message_id or "")[:128],
            "text": clean_reply,
            "delivered": True,
        },
        "matched": True,
        "log_ids": log_ids[:4],
        "question_ids": qids[:8],
        "topics": topics[:12],
        "timestamp": time.time(),
    }
    _schedule_ingest(host, current, event_payload)


def _schedule_member_scope_ingest(host, target, payload, event, group_id=""):
    umo = payload["umo"]
    private_recipient = private_message_recipient(umo)

    async def resolve_and_enqueue():
        try:
            world = find_companion(host.context, "worldbook")
            getter = getattr(world, "get_character_context", None)
            if not callable(getter):
                return
            scope = {"umo": umo, "platform": umo.split(":", 2)[0]}
            if private_recipient:
                if (private_recipient != payload["member"] or event.unified_msg_origin != umo
                        or not is_private_message_event(event)):
                    return
                char = await asyncio.wait_for(getter(scope, event), timeout=5)
            else:
                char = await asyncio.wait_for(getter(scope), timeout=5)
            if not char.get("available") or char.get("disabled") or provider(host) is not target:
                return
            char_scope = char.get("scope") or {}
            if not char_scope.get("persona_id") or char_scope.get("umo") != umo:
                return
            if private_recipient:
                runtime = host._sessions.get(umo)
                if (runtime is None or runtime.scope_kind != "private" or not host.is_runtime_scope_enabled(runtime)
                        or not social_boundary_allows(host, event, char_scope, private_recipient)):
                    return
            elif not host.is_group_takeover_enabled(group_id):
                return
            await _invoke_ingest_async(target, {**payload, "character_scope": char_scope})
        except Exception:
            return

    creator = getattr(host, "_create_background_task", None)
    task = creator(resolve_and_enqueue()) if callable(creator) else asyncio.create_task(resolve_and_enqueue())
    if isinstance(task, asyncio.Task):
        _PENDING_FEEDBACK_TASKS.add(task)
        task.add_done_callback(_PENDING_FEEDBACK_TASKS.discard)


def observe_member(host, event, parsed):
    """Register actual managed scopes and preferences even when no reply is produced."""
    target = provider(host)
    if target is None or not parsed.sender_id or parsed.sender_id == parsed.self_id:
        return
    umo = str(event.unified_msg_origin)
    narrative = getattr(host, "narrative", None)
    if narrative is not None:
        narrative.start()
    payload = {"kind": "message.received", "umo": umo, "managed": True, "member": parsed.sender_id,
               "text": str(parsed.text or "")[:2000], "event_id": str(parsed.message_id or ""),
               "message_id": str(parsed.message_id or "")[:128],
               "reply_to_message_id": str(parsed.reply_to_id or "")[:128],
               "reply_to_sender_id": str(parsed.reply_sender_id or "")[:128],
               "delivered": False}
    _schedule_member_scope_ingest(host, target, payload, event, parsed.group_id)
