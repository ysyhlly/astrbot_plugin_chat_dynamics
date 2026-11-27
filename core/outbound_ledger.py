"""Per-session outbound ledger, OneBot outbound observer, and self-poke echo tracking.

Captures outbound messages sent by external AstrBot plugins (e.g. anti_revoke,
bili_push, scheduled reminders) directly at the OneBot/aiocqhttp client boundary
while deduplicating against chat_dynamics' own sends, and tracks bot-initiated
pokes to suppress platform self-poke echoes (including NapCat private echoes
where user_id == target_id == friend_id) while backfilling rendered poke action
text for read-only prompt awareness.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import functools
import inspect
import re
import threading
import time
from typing import Any
import weakref

from astrbot.api import logger

DEFAULT_TTL_SECONDS = 1800.0
DEFAULT_MAX_ITEMS_PER_SESSION = 8
DEFAULT_MAX_TEXT_CHARS = 120
DEFAULT_ECHO_WINDOW_SECONDS = 15.0
DEFAULT_MAX_SESSIONS = 1024
DEFAULT_MAX_PENDING_POKES_PER_SESSION = 16

_OWNED_OUTBOUND_DEPTH: ContextVar[int] = ContextVar(
    "chat_dynamics_owned_outbound_depth",
    default=0,
)
_OBSERVER_CAPTURE_DEPTH: ContextVar[int] = ContextVar(
    "chat_dynamics_outbound_observer_depth",
    default=0,
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_CQ_CODE_RE = re.compile(r"\[CQ:([a-zA-Z0-9_-]+)(?:,([^\]]*))?\]")
_ONEBOT_CLIENT_METHODS = (
    "call_action",
    "send_private_msg",
    "send_group_msg",
    "send_msg",
    "send_poke",
    "friend_poke",
    "group_poke",
)
_ONEBOT_PLATFORMS = frozenset({"aiocqhttp", "onebot", "onebot_v11", "napcat", "qq"})
_UNKNOWN_SENDER_IDS = frozenset({"", "0", "unknown", "none"})


@contextmanager
def owned_outbound_send() -> Iterator[None]:
    """Mark the active execution context as a chat_dynamics-owned outbound send."""
    token = _OWNED_OUTBOUND_DEPTH.set(_OWNED_OUTBOUND_DEPTH.get() + 1)
    try:
        yield
    finally:
        _OWNED_OUTBOUND_DEPTH.reset(token)


def is_owned_outbound_send() -> bool:
    """Return True while executing inside ``owned_outbound_send()``."""
    return _OWNED_OUTBOUND_DEPTH.get() > 0


@dataclass(frozen=True)
class OutboundEntry:
    """Single read-only outbound observation stored per session."""

    timestamp: float
    text: str
    kind: str = "external"
    source: str = "external_plugin"
    scope: str = ""
    target_id: str = ""
    platform_id: str = ""
    action: str = ""


@dataclass(frozen=True)
class _PendingBotPoke:
    token: str
    session_key: str
    target_id: str
    bot_id: str
    timestamp: float
    scope: str = ""
    platform_id: str = ""


def _norm_scope(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"group", "group_message", "groupmessage"}:
        return "group"
    if raw in {"private", "friend", "friend_message", "friendmessage", "privatemessage", "dm"}:
        return "private"
    return ""


def _norm_id(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if text in {"", "None", "none"}:
        return ""
    return text


def _parse_session_key(session_key: str) -> tuple[str, str, str]:
    """Parse ``(platform_id, scope, target_id)`` from a UMO, fallback key, or raw ID."""
    raw = str(session_key or "").strip()
    if not raw:
        return "", "", ""
    parts = raw.split(":")
    if len(parts) >= 3:
        plat = parts[0].strip()
        middle = parts[1].strip().lower()
        target = ":".join(parts[2:]).strip()
        if middle in {"group", "private"}:
            return plat, middle, target
        if "group" in middle:
            return plat, "group", target
        if "friend" in middle or "private" in middle:
            return plat, "private", target
        return plat, "", target
    if len(parts) == 2 and parts[0].strip().lower() in {"group", "private"}:
        return "", parts[0].strip().lower(), parts[1].strip()
    return "", "", raw


def _sanitize_text(value: Any, *, max_chars: int = DEFAULT_MAX_TEXT_CHARS) -> str:
    if value is None or isinstance(value, bool):
        return ""
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    limit = max(1, int(max_chars or DEFAULT_MAX_TEXT_CHARS))
    if len(cleaned) <= limit:
        return cleaned
    if limit == 1:
        return cleaned[:1]
    clipped = cleaned[: limit - 1].rstrip()
    return f"{clipped or cleaned[: limit - 1]}…"


def _unescape_cq_text(text: str) -> str:
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _format_cq_match(match: re.Match[str]) -> str:
    cq_type = str(match.group(1) or "").strip().lower()
    raw_params = str(match.group(2) or "")
    params: dict[str, str] = {}
    if raw_params:
        for pair in raw_params.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k.strip().lower()] = _unescape_cq_text(v.strip())
    if cq_type == "reply":
        return ""
    if cq_type == "at":
        qq = params.get("qq", "")
        if qq.lower() == "all":
            return "@全体成员"
        return f"@{qq}" if qq else "@"
    mapping = {
        "image": "[图片]",
        "face": "[表情]",
        "mface": "[表情]",
        "bface": "[表情]",
        "sface": "[表情]",
        "record": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "poke": "[戳一戳]",
        "forward": "[转发消息]",
        "node": "[转发消息]",
        "json": "[卡片消息]",
        "xml": "[卡片消息]",
        "share": "[分享]",
        "music": "[音乐]",
    }
    return mapping.get(cq_type, f"[{cq_type}]" if cq_type else "")


def _extract_segment_text(seg: Any) -> str:
    if seg is None:
        return ""
    if isinstance(seg, str):
        replaced = _CQ_CODE_RE.sub(_format_cq_match, seg)
        return _unescape_cq_text(replaced)
    if isinstance(seg, dict):
        seg_type = str(seg.get("type") or "").strip().lower()
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type in {"text", "plain"}:
            return str(data.get("text") or seg.get("text") or "")
        if seg_type == "reply":
            return ""
        if seg_type == "at":
            qq = _norm_id(data.get("qq") or data.get("user_id") or "")
            if qq.lower() == "all":
                return "@全体成员"
            return f"@{qq}" if qq else "@"
        mapping = {
            "image": "[图片]",
            "face": "[表情]",
            "mface": "[表情]",
            "record": "[语音]",
            "video": "[视频]",
            "file": "[文件]",
            "poke": "[戳一戳]",
            "forward": "[转发消息]",
            "node": "[转发消息]",
            "json": "[卡片消息]",
            "xml": "[卡片消息]",
        }
        return mapping.get(seg_type, f"[{seg_type}]" if seg_type else "")
    comp_name = type(seg).__name__.lower()
    if comp_name in {"plain", "text", "markdown"}:
        return str(getattr(seg, "text", "") or "")
    if comp_name == "reply":
        return ""
    if comp_name == "atall":
        return "@全体成员"
    if comp_name == "at":
        qq = _norm_id(getattr(seg, "qq", None) or getattr(seg, "user_id", None))
        return f"@{qq}" if qq else "@"
    mapping = {
        "image": "[图片]",
        "face": "[表情]",
        "record": "[语音]",
        "audio": "[语音]",
        "voice": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "poke": "[戳一戳]",
        "forward": "[转发消息]",
        "node": "[转发消息]",
        "nodes": "[转发消息]",
    }
    return mapping.get(comp_name, str(getattr(seg, "text", "") or ""))


def extract_onebot_message_text(
    message: Any,
    *,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> str:
    """Extract a single-line, bounded human-readable summary from a OneBot payload."""
    if message is None:
        return ""
    if hasattr(message, "chain") and isinstance(getattr(message, "chain", None), (list, tuple)):
        parts = [_extract_segment_text(item) for item in message.chain]
        return _sanitize_text("".join(p for p in parts if p), max_chars=max_chars)
    if isinstance(message, (list, tuple)):
        parts = [_extract_segment_text(item) for item in message]
        return _sanitize_text("".join(p for p in parts if p), max_chars=max_chars)
    return _sanitize_text(_extract_segment_text(message), max_chars=max_chars)


def _format_relative_age(age_seconds: float) -> str:
    age = max(0.0, float(age_seconds))
    if age < 45.0:
        return "刚刚"
    minutes = max(1, int(round(age / 60.0)))
    return f"{minutes}分钟前"


def _response_indicates_failure(response: Any) -> bool:
    if response is False:
        return True
    if isinstance(response, dict):
        status = str(response.get("status") or "").strip().lower()
        if status in {"failed", "error", "failure"}:
            return True
        retcode = response.get("retcode")
        if isinstance(retcode, int) and not isinstance(retcode, bool) and retcode != 0:
            return True
        if response.get("ok") is False or response.get("success") is False:
            return True
    return False


class OutboundLedger:
    """Thread-safe/async-safe per-session ledger for external outbound & poke echoes."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_items_per_session: int = DEFAULT_MAX_ITEMS_PER_SESSION,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        echo_window_seconds: float = DEFAULT_ECHO_WINDOW_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_items_per_session = max(1, int(max_items_per_session))
        self._max_text_chars = max(16, int(max_text_chars))
        self._echo_window_seconds = max(1.0, float(echo_window_seconds))
        self._max_sessions = max(16, int(max_sessions))
        self._clock = clock if callable(clock) else time.monotonic
        self._lock = threading.RLock()
        self._entries: dict[str, deque[OutboundEntry]] = {}
        self._pending_pokes: dict[str, deque[_PendingBotPoke]] = {}
        self._pending_tokens: dict[str, str] = {}
        self._session_aliases: dict[tuple[str, str, str], str] = {}
        self._token_seq = 0

    def _now(self, now: float | None = None) -> float:
        return float(self._clock() if now is None else now)

    def _prune_entries_locked(self, bucket: str, now: float) -> deque[OutboundEntry]:
        items = self._entries.get(bucket)
        if items is None:
            return deque()
        cutoff = now - self._ttl_seconds
        while items and items[0].timestamp < cutoff:
            items.popleft()
        if not items:
            self._entries.pop(bucket, None)
            return deque()
        return items

    def _prune_pending_pokes_locked(self, bucket: str, now: float) -> deque[_PendingBotPoke]:
        items = self._pending_pokes.get(bucket)
        if items is None:
            return deque()
        cutoff = now - self._echo_window_seconds
        while items and items[0].timestamp < cutoff:
            expired = items.popleft()
            self._pending_tokens.pop(expired.token, None)
        if not items:
            self._pending_pokes.pop(bucket, None)
            return deque()
        return items

    def _evict_sessions_locked(self, now: float) -> None:
        if len(self._entries) > self._max_sessions:
            cutoff = now - self._ttl_seconds
            stale = [
                key
                for key, items in self._entries.items()
                if not items or items[-1].timestamp < cutoff
            ]
            for key in stale:
                self._entries.pop(key, None)
            while len(self._entries) > self._max_sessions:
                oldest = min(
                    self._entries,
                    key=lambda k: self._entries[k][-1].timestamp if self._entries[k] else -1.0,
                )
                self._entries.pop(oldest, None)
        if len(self._pending_pokes) > self._max_sessions:
            cutoff = now - self._echo_window_seconds
            stale_pokes = [
                key
                for key, items in self._pending_pokes.items()
                if not items or items[-1].timestamp < cutoff
            ]
            for key in stale_pokes:
                dropped = self._pending_pokes.pop(key, ())
                for rec in dropped:
                    self._pending_tokens.pop(rec.token, None)
            while len(self._pending_pokes) > self._max_sessions:
                oldest = min(
                    self._pending_pokes,
                    key=lambda k: self._pending_pokes[k][-1].timestamp if self._pending_pokes[k] else -1.0,
                )
                dropped = self._pending_pokes.pop(oldest, ())
                for rec in dropped:
                    self._pending_tokens.pop(rec.token, None)

    def register_session(
        self,
        session_key: str,
        *,
        scope: str = "",
        target_id: str = "",
        platform_id: str = "",
    ) -> None:
        """Associate a live AstrBot session key (UMO) with its ``(platform, scope, target_id)``."""
        canonical = str(session_key or "").strip()
        if not canonical:
            return
        p_plat, p_scope, p_tid = _parse_session_key(canonical)
        eff_plat = str(platform_id or p_plat or "").strip()
        eff_scope = _norm_scope(scope or p_scope)
        eff_tid = _norm_id(target_id or p_tid)
        if not eff_scope or not eff_tid:
            return
        with self._lock:
            if eff_plat:
                self._session_aliases[(eff_plat, eff_scope, eff_tid)] = canonical
            if p_plat and p_plat != eff_plat:
                self._session_aliases[(p_plat, eff_scope, eff_tid)] = canonical
            self._session_aliases[("", eff_scope, eff_tid)] = canonical
            if len(self._session_aliases) > self._max_sessions * 4:
                excess = len(self._session_aliases) - self._max_sessions * 2
                for key in list(self._session_aliases)[:excess]:
                    self._session_aliases.pop(key, None)

    def _resolve_write_bucket_locked(
        self,
        session_key: str = "",
        *,
        scope: str = "",
        target_id: str = "",
        platform_id: str = "",
    ) -> str:
        explicit = str(session_key or "").strip()
        if explicit:
            return explicit
        eff_plat = str(platform_id or "").strip()
        eff_scope = _norm_scope(scope)
        eff_tid = _norm_id(target_id)
        if eff_scope and eff_tid:
            if eff_plat:
                mapped = self._session_aliases.get((eff_plat, eff_scope, eff_tid))
                if mapped:
                    return mapped
                return f"{eff_plat}:{eff_scope}:{eff_tid}"
            mapped = self._session_aliases.get(("", eff_scope, eff_tid))
            if mapped:
                return mapped
            return f"{eff_scope}:{eff_tid}"
        return eff_tid

    def _candidate_buckets_locked(
        self,
        session_key: str,
        *,
        platform_id: str = "",
        target_id: str = "",
        scope: str = "",
    ) -> tuple[list[str], set[str]]:
        canonical = str(session_key or "").strip()
        p_plat, p_scope, p_tid = _parse_session_key(canonical)
        eff_scope = _norm_scope(scope or p_scope)
        eff_tid = _norm_id(target_id or p_tid)
        plats: list[str] = []
        for candidate in (p_plat, str(platform_id or "").strip()):
            if candidate and candidate not in plats:
                plats.append(candidate)

        buckets: list[str] = []

        def _add(key: str) -> None:
            if key and key not in buckets:
                buckets.append(key)

        _add(canonical)
        if eff_scope and eff_tid:
            for plat in plats:
                mapped = self._session_aliases.get((plat, eff_scope, eff_tid))
                if mapped:
                    _add(mapped)
                _add(f"{plat}:{eff_scope}:{eff_tid}")
            if not plats:
                mapped_any = self._session_aliases.get(("", eff_scope, eff_tid))
                if mapped_any:
                    _add(mapped_any)
            _add(f"{eff_scope}:{eff_tid}")
        elif eff_tid:
            for sc in ("private", "group"):
                for plat in plats:
                    mapped = self._session_aliases.get((plat, sc, eff_tid))
                    if mapped:
                        _add(mapped)
                    _add(f"{plat}:{sc}:{eff_tid}")
                if not plats:
                    mapped_any = self._session_aliases.get(("", sc, eff_tid))
                    if mapped_any:
                        _add(mapped_any)
                _add(f"{sc}:{eff_tid}")
            _add(eff_tid)
        return buckets, set(plats)

    def _append_entry_locked(self, bucket: str, entry: OutboundEntry, now: float) -> None:
        items = self._entries.get(bucket)
        if items is None:
            items = deque(maxlen=self._max_items_per_session)
            self._entries[bucket] = items
        else:
            cutoff = now - self._ttl_seconds
            while items and items[0].timestamp < cutoff:
                items.popleft()
        items.append(entry)
        self._evict_sessions_locked(now)

    def record_external_outbound(
        self,
        session_key: str = "",
        *,
        text: str,
        now: float | None = None,
        scope: str = "",
        target_id: str = "",
        platform_id: str = "",
        action: str = "",
        source: str = "external_plugin",
        kind: str = "external",
    ) -> bool:
        """Record an outbound message sent by an external plugin."""
        cleaned = _sanitize_text(text, max_chars=self._max_text_chars)
        if not cleaned:
            return False
        p_plat, p_scope, p_tid = _parse_session_key(session_key)
        eff_plat = str(platform_id or p_plat or "").strip()
        eff_scope = _norm_scope(scope or p_scope)
        eff_tid = _norm_id(target_id or p_tid)
        ts = self._now(now)
        with self._lock:
            bucket = self._resolve_write_bucket_locked(
                session_key,
                scope=eff_scope,
                target_id=eff_tid,
                platform_id=eff_plat,
            )
            if not bucket:
                return False
            self._append_entry_locked(
                bucket,
                OutboundEntry(
                    timestamp=ts,
                    text=cleaned,
                    kind=str(kind or "external"),
                    source=str(source or "external_plugin"),
                    scope=eff_scope,
                    target_id=eff_tid,
                    platform_id=eff_plat,
                    action=str(action or ""),
                ),
                ts,
            )
            return True

    def record_poke_action_detail(
        self,
        session_key: str,
        action_text: str,
        *,
        target_id: str = "",
        now: float | None = None,
        scope: str = "",
        platform_id: str = "",
    ) -> bool:
        """Backfill the platform-rendered action text of a bot-initiated poke."""
        cleaned_action = _sanitize_text(action_text, max_chars=80)
        if not cleaned_action:
            return False
        summary = f"刚才你戳了对方（实际展示动作：{cleaned_action}）"
        return self.record_external_outbound(
            session_key,
            text=summary,
            now=now,
            scope=scope,
            target_id=target_id,
            platform_id=platform_id,
            action="send_poke",
            source="poke_echo",
            kind="poke_echo",
        )

    def record_bot_poke_sent(
        self,
        session_key: str,
        target_id: str,
        bot_id: str = "",
        now: float | None = None,
        *,
        scope: str = "",
        platform_id: str = "",
    ) -> str:
        """Register an outgoing bot poke for self-poke echo matching within the echo window."""
        eff_target = _norm_id(target_id)
        if not eff_target:
            return ""
        p_plat, p_scope, _ = _parse_session_key(session_key)
        eff_plat = str(platform_id or p_plat or "").strip()
        eff_scope = _norm_scope(scope or p_scope)
        eff_bot = _norm_id(bot_id)
        ts = self._now(now)
        with self._lock:
            bucket = self._resolve_write_bucket_locked(
                session_key,
                scope=eff_scope,
                target_id=eff_target,
                platform_id=eff_plat,
            )
            if not bucket:
                return ""
            items = self._pending_pokes.get(bucket)
            if items is None:
                items = deque(maxlen=DEFAULT_MAX_PENDING_POKES_PER_SESSION)
                self._pending_pokes[bucket] = items
            else:
                self._prune_pending_pokes_locked(bucket, ts)
                items = self._pending_pokes.setdefault(
                    bucket, deque(maxlen=DEFAULT_MAX_PENDING_POKES_PER_SESSION)
                )
            self._token_seq += 1
            token = f"poke-{self._token_seq}"
            if len(items) == items.maxlen:
                dropped = items.popleft()
                self._pending_tokens.pop(dropped.token, None)
            items.append(
                _PendingBotPoke(
                    token=token,
                    session_key=bucket,
                    target_id=eff_target,
                    bot_id=eff_bot,
                    timestamp=ts,
                    scope=eff_scope,
                    platform_id=eff_plat,
                )
            )
            self._pending_tokens[token] = bucket
            self._evict_sessions_locked(ts)
            return token

    def cancel_bot_poke_sent(self, token: str) -> bool:
        """Remove an unconsumed pending bot poke (e.g. when ``send_poke`` failed)."""
        if not token:
            return False
        with self._lock:
            bucket = self._pending_tokens.pop(token, None)
            if not bucket:
                return False
            items = self._pending_pokes.get(bucket)
            if not items:
                return False
            for idx, rec in enumerate(items):
                if rec.token == token:
                    del items[idx]
                    if not items:
                        self._pending_pokes.pop(bucket, None)
                    return True
            return False

    def match_and_consume_self_poke_echo(
        self,
        session_key: str,
        *,
        sender_id: str,
        target_id: str,
        bot_id: str = "",
        now: float | None = None,
        action_text: str = "",
        scope: str = "",
        platform_id: str = "",
    ) -> bool:
        """Match and consume a bot-initiated poke echo; backfill ``action_text`` when present.

        Handles:
        1. Standard echoes where ``sender_id == bot_id`` (private or group).
        2. NapCat private-chat echoes where ``user_id == target_id == friend_id``
           (matching a recent bot-initiated poke to ``target_id``).
        3. Group-chat echoes where ``sender_id == bot_id`` or an anomalous
           self/empty sender echo matching a recent bot poke to ``target_id``.
        """
        eff_sender = _norm_id(sender_id)
        eff_target = _norm_id(target_id)
        eff_bot = _norm_id(bot_id)
        p_plat, p_scope, _ = _parse_session_key(session_key)
        eff_scope = _norm_scope(scope or p_scope)
        eff_plat = str(platform_id or p_plat or "").strip()
        ts = self._now(now)

        is_bot_sender = bool(eff_sender and eff_bot and eff_sender == eff_bot)
        anomalous_sender = bool(
            eff_target
            and (
                eff_sender == eff_target
                or eff_sender.lower() in _UNKNOWN_SENDER_IDS
            )
        )

        matched = is_bot_sender
        with self._lock:
            buckets, _ = self._candidate_buckets_locked(
                session_key,
                platform_id=eff_plat,
                target_id=eff_target if eff_scope == "private" else "",
                scope=eff_scope,
            )
            consumed: _PendingBotPoke | None = None
            if is_bot_sender or anomalous_sender:
                for bucket in buckets:
                    items = self._prune_pending_pokes_locked(bucket, ts)
                    if not items:
                        continue
                    for idx, rec in enumerate(items):
                        if rec.timestamp > ts + 1e-6:
                            continue
                        if eff_target and rec.target_id and rec.target_id != eff_target:
                            continue
                        if eff_scope and rec.scope and rec.scope != eff_scope:
                            continue
                        consumed = rec
                        del items[idx]
                        self._pending_tokens.pop(rec.token, None)
                        if not items:
                            self._pending_pokes.pop(bucket, None)
                        break
                    if consumed is not None:
                        break
            if consumed is not None:
                matched = True

        if not matched:
            return False

        if action_text and _sanitize_text(action_text, max_chars=80):
            self.record_poke_action_detail(
                session_key,
                action_text,
                target_id=eff_target,
                now=ts,
                scope=eff_scope,
                platform_id=eff_plat,
            )
        return True

    def get_recent_entries(
        self,
        session_key: str,
        *,
        now: float | None = None,
        platform_id: str = "",
        target_id: str = "",
        scope: str = "",
    ) -> list[OutboundEntry]:
        """Return unexpired outbound entries for a session, oldest-first (capped at max_items)."""
        ts = self._now(now)
        with self._lock:
            buckets, query_plats = self._candidate_buckets_locked(
                session_key,
                platform_id=platform_id,
                target_id=target_id,
                scope=scope,
            )
            collected: list[OutboundEntry] = []
            seen_ids: set[int] = set()
            for bucket in buckets:
                items = self._prune_entries_locked(bucket, ts)
                for entry in items:
                    if id(entry) in seen_ids:
                        continue
                    if query_plats and entry.platform_id and entry.platform_id not in query_plats:
                        continue
                    seen_ids.add(id(entry))
                    collected.append(entry)
        if not collected:
            return []
        collected.sort(key=lambda item: item.timestamp)
        if len(collected) > self._max_items_per_session:
            collected = collected[-self._max_items_per_session :]
        return collected

    def format_recent_outbound_note(
        self,
        session_key: str,
        *,
        now: float | None = None,
        platform_id: str = "",
        target_id: str = "",
        scope: str = "",
    ) -> str:
        """Format recent external outbound messages and poke action details as a read-only note."""
        ts = self._now(now)
        entries = self.get_recent_entries(
            session_key,
            now=ts,
            platform_id=platform_id,
            target_id=target_id,
            scope=scope,
        )
        if not entries:
            return ""
        lines = ["[近期其他插件或动作的对外记录（只读参考，非你本轮发言）]"]
        for entry in entries:
            rel = _format_relative_age(ts - entry.timestamp)
            tag = "[戳一戳回显]" if entry.kind == "poke_echo" else "[外部插件]"
            lines.append(f"- [{rel}]{tag} {entry.text}")
        return "\n".join(lines)

    def clear_session(self, session_key: str) -> None:
        canonical = str(session_key or "").strip()
        if not canonical:
            return
        with self._lock:
            buckets, _ = self._candidate_buckets_locked(canonical)
            for bucket in buckets:
                self._entries.pop(bucket, None)
                dropped = self._pending_pokes.pop(bucket, ())
                for rec in dropped:
                    self._pending_tokens.pop(rec.token, None)
            stale_aliases = [k for k, v in self._session_aliases.items() if v == canonical]
            for key in stale_aliases:
                self._session_aliases.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._pending_pokes.clear()
            self._pending_tokens.clear()
            self._session_aliases.clear()


def _parse_onebot_call(
    method_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[str, str, str, str]:
    """Return ``(scope, target_id, text, action_name)`` from a OneBot client call."""
    action = method_name
    params: dict[str, Any] = dict(kwargs)
    pos = list(args)

    if method_name == "call_action":
        if pos:
            action = str(pos.pop(0) or "").strip()
        elif "action" in params:
            action = str(params.pop("action") or "").strip()
        if pos and isinstance(pos[0], dict):
            merged = dict(pos.pop(0))
            merged.update(params)
            params = merged
        elif "params" in params and isinstance(params["params"], dict):
            merged = dict(params.pop("params"))
            merged.update(params)
            params = merged
    else:
        if pos and isinstance(pos[0], dict):
            merged = dict(pos.pop(0))
            merged.update(params)
            params = merged

    action_lower = str(action or "").strip().lower()
    if action_lower == "send_private_msg":
        user_id = _norm_id(params.get("user_id") if "user_id" in params else (pos[0] if pos else ""))
        message = params.get("message") if "message" in params else (pos[1] if len(pos) > 1 else None)
        return "private", user_id, extract_onebot_message_text(message), action_lower

    if action_lower == "send_group_msg":
        group_id = _norm_id(params.get("group_id") if "group_id" in params else (pos[0] if pos else ""))
        message = params.get("message") if "message" in params else (pos[1] if len(pos) > 1 else None)
        return "group", group_id, extract_onebot_message_text(message), action_lower

    if action_lower == "send_msg":
        msg_type = _norm_scope(params.get("message_type") or "")
        group_id = _norm_id(params.get("group_id"))
        user_id = _norm_id(params.get("user_id"))
        message = params.get("message")
        if msg_type == "group" or (not msg_type and group_id):
            return "group", group_id, extract_onebot_message_text(message), action_lower
        return "private", user_id, extract_onebot_message_text(message), action_lower

    if action_lower in {"send_poke", "friend_poke", "group_poke"}:
        group_id = _norm_id(params.get("group_id"))
        user_id = _norm_id(params.get("user_id") or params.get("target_id"))
        if not group_id and not user_id and pos:
            if action_lower == "group_poke" and len(pos) >= 2:
                group_id = _norm_id(pos[0])
                user_id = _norm_id(pos[1])
            else:
                user_id = _norm_id(pos[0])
        if group_id:
            label = f"[戳了 {user_id}]" if user_id else "[戳一戳]"
            return "group", group_id, label, action_lower
        return "private", user_id, "[戳了对方]" if user_id else "", action_lower

    return "", "", "", action_lower


def _static_has_attr(target: Any, name: str) -> bool:
    try:
        inspect.getattr_static(target, name)
        return True
    except AttributeError:
        return False
    except Exception:
        return False


def _extract_platform_id(inst: Any) -> str:
    meta_fn = getattr(inst, "meta", None)
    meta = None
    if callable(meta_fn):
        try:
            meta = meta_fn()
        except Exception:
            meta = None
    if meta is None:
        meta = getattr(inst, "metadata", None)
    if meta is not None:
        for attr in ("id", "name"):
            val = str(getattr(meta, attr, "") or "").strip()
            if val:
                return val
    return ""


def _extract_platform_name(inst: Any) -> str:
    meta_fn = getattr(inst, "meta", None)
    meta = None
    if callable(meta_fn):
        try:
            meta = meta_fn()
        except Exception:
            meta = None
    if meta is None:
        meta = getattr(inst, "metadata", None)
    if meta is not None:
        name = str(getattr(meta, "name", "") or "").strip().lower()
        if name:
            return name
    return type(inst).__name__.lower()


def _extract_client_from_platform(inst: Any) -> Any:
    getter = getattr(inst, "get_client", None)
    if callable(getter):
        try:
            client = getter()
            if client is not None:
                return client
        except Exception:
            pass
    for attr in ("bot", "client"):
        client = getattr(inst, attr, None)
        if client is not None:
            return client
    return None


@dataclass
class _WrappedTargetRecord:
    target_ref: Any
    is_weakref: bool
    platform_id: str
    originals: list[tuple[str, Any, bool, Any]]

    def get_target(self) -> Any:
        return self.target_ref() if self.is_weakref else self.target_ref


class OneBotOutboundObserver:
    """Idempotent, cleanly removable observer on OneBot/aiocqhttp clients."""

    def __init__(self, ledger: OutboundLedger) -> None:
        self._ledger = ledger
        self._lock = threading.RLock()
        self._wrapped: dict[int, _WrappedTargetRecord] = {}

    @property
    def wrapped_count(self) -> int:
        with self._lock:
            return len(self._wrapped)

    def _make_wrapper(self, method_name: str, original: Any, target_id_key: int) -> Any:
        observer = self

        def _record_after(args: tuple[Any, ...], kwargs: dict[str, Any], response: Any) -> None:
            try:
                if _response_indicates_failure(response):
                    return
                scope, target_id, text, action_name = _parse_onebot_call(method_name, args, kwargs)
                if not scope or not target_id or not text:
                    return
                with observer._lock:
                    rec = observer._wrapped.get(target_id_key)
                    plat_id = rec.platform_id if rec is not None else ""
                observer._ledger.record_external_outbound(
                    scope=scope,
                    target_id=target_id,
                    platform_id=plat_id,
                    text=text,
                    action=action_name,
                    source="external_plugin",
                    kind="external",
                )
            except Exception:
                pass

        if inspect.iscoroutinefunction(original):
            @functools.wraps(original)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if is_owned_outbound_send() or _OBSERVER_CAPTURE_DEPTH.get() > 0:
                    return await original(*args, **kwargs)
                token = _OBSERVER_CAPTURE_DEPTH.set(_OBSERVER_CAPTURE_DEPTH.get() + 1)
                try:
                    response = await original(*args, **kwargs)
                finally:
                    _OBSERVER_CAPTURE_DEPTH.reset(token)
                _record_after(args, kwargs, response)
                return response

            setattr(_async_wrapper, "_cd_outbound_observer_id", id(observer))
            return _async_wrapper

        @functools.wraps(original)
        def _sync_or_awaitable_wrapper(*args: Any, **kwargs: Any) -> Any:
            if is_owned_outbound_send() or _OBSERVER_CAPTURE_DEPTH.get() > 0:
                return original(*args, **kwargs)
            token = _OBSERVER_CAPTURE_DEPTH.set(_OBSERVER_CAPTURE_DEPTH.get() + 1)
            try:
                result = original(*args, **kwargs)
            except Exception:
                _OBSERVER_CAPTURE_DEPTH.reset(token)
                raise
            if inspect.isawaitable(result):
                _OBSERVER_CAPTURE_DEPTH.reset(token)

                async def _await_and_record() -> Any:
                    inner_token = _OBSERVER_CAPTURE_DEPTH.set(_OBSERVER_CAPTURE_DEPTH.get() + 1)
                    try:
                        response = await result
                    finally:
                        _OBSERVER_CAPTURE_DEPTH.reset(inner_token)
                    _record_after(args, kwargs, response)
                    return response

                return _await_and_record()
            _OBSERVER_CAPTURE_DEPTH.reset(token)
            _record_after(args, kwargs, result)
            return result

        setattr(_sync_or_awaitable_wrapper, "_cd_outbound_observer_id", id(observer))
        return _sync_or_awaitable_wrapper

    def _install_single_target_locked(self, target: Any, *, platform_id: str = "") -> bool:
        if target is None:
            return False
        key = id(target)
        existing = self._wrapped.get(key)
        if existing is not None:
            if existing.get_target() is target:
                if platform_id and not existing.platform_id:
                    existing.platform_id = platform_id
                return True
            self._wrapped.pop(key, None)

        originals: list[tuple[str, Any, bool, Any]] = []
        target_dict = getattr(target, "__dict__", None)
        for name in _ONEBOT_CLIENT_METHODS:
            if not _static_has_attr(target, name):
                continue
            try:
                current = getattr(target, name, None)
            except Exception:
                continue
            if not callable(current):
                continue
            if getattr(current, "_cd_outbound_observer_id", None) == id(self):
                continue
            had_in_dict = isinstance(target_dict, dict) and name in target_dict
            raw_dict_val = target_dict.get(name) if had_in_dict else None
            wrapper = self._make_wrapper(name, current, key)
            try:
                setattr(target, name, wrapper)
            except Exception:
                continue
            originals.append((name, wrapper, had_in_dict, raw_dict_val))

        if not originals:
            return False

        try:
            ref: Any = weakref.ref(target)
            is_weak = True
        except TypeError:
            ref = target
            is_weak = False
        self._wrapped[key] = _WrappedTargetRecord(
            target_ref=ref,
            is_weakref=is_weak,
            platform_id=str(platform_id or "").strip(),
            originals=originals,
        )
        return True

    def install_client(self, client: Any, *, platform_id: str = "") -> bool:
        """Idempotently wrap a OneBot/aiocqhttp client (and its ``.api`` if distinct)."""
        if client is None:
            return False
        plat = str(platform_id or "").strip()
        with self._lock:
            installed_main = self._install_single_target_locked(client, platform_id=plat)
            api_obj = getattr(client, "api", None)
            installed_api = False
            if api_obj is not None and api_obj is not client:
                installed_api = self._install_single_target_locked(api_obj, platform_id=plat)
            return installed_main or installed_api

    def install_from_event(self, event: Any) -> bool:
        """Best-effort idempotent wrap of ``event.bot`` from a live AstrMessageEvent."""
        if event is None:
            return False
        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        plat_name = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                plat_name = str(getter() or "").strip().lower()
            except Exception:
                plat_name = ""
        if plat_name and plat_name not in _ONEBOT_PLATFORMS:
            if not any(_static_has_attr(bot, m) for m in _ONEBOT_CLIENT_METHODS):
                return False
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        plat_id = umo.split(":", 1)[0].strip() if ":" in umo else plat_name
        return self.install_client(bot, platform_id=plat_id)

    def install_from_context(self, context: Any) -> int:
        """Best-effort idempotent scan of ``context.platform_manager`` for OneBot clients."""
        if context is None:
            return 0
        insts: list[Any] = []
        manager = getattr(context, "platform_manager", None)
        if manager is not None:
            getter = getattr(manager, "get_insts", None)
            if callable(getter):
                try:
                    insts = list(getter() or [])
                except Exception:
                    insts = []
            if not insts:
                raw_insts = getattr(manager, "platform_insts", None)
                if raw_insts is not None:
                    try:
                        insts = list(raw_insts)
                    except Exception:
                        insts = []
        if not insts:
            direct = getattr(context, "platform_insts", None)
            if direct is not None:
                try:
                    insts = list(direct)
                except Exception:
                    insts = []

        count = 0
        for inst in insts:
            plat_name = _extract_platform_name(inst)
            client = _extract_client_from_platform(inst)
            if client is None:
                continue
            looks_onebot = any(token in plat_name for token in _ONEBOT_PLATFORMS) or any(
                _static_has_attr(client, m) for m in _ONEBOT_CLIENT_METHODS
            )
            if not looks_onebot:
                continue
            plat_id = _extract_platform_id(inst)
            if self.install_client(client, platform_id=plat_id):
                count += 1
        return count

    def uninstall(self) -> int:
        """Restore all wrapped client attributes cleanly."""
        restored = 0
        with self._lock:
            records = list(self._wrapped.values())
            self._wrapped.clear()
        for record in records:
            target = record.get_target()
            if target is None:
                continue
            changed = False
            for name, wrapper, had_in_dict, raw_dict_val in record.originals:
                try:
                    if getattr(target, name, None) is not wrapper:
                        continue
                    if had_in_dict:
                        setattr(target, name, raw_dict_val)
                    else:
                        delattr(target, name)
                    changed = True
                except Exception as exc:
                    logger.debug(
                        "[ChatDynamics] Outbound observer restore skipped attr=%s type=%s",
                        name,
                        type(exc).__name__,
                    )
            if changed:
                restored += 1
        return restored


__all__ = [
    "DEFAULT_ECHO_WINDOW_SECONDS",
    "DEFAULT_MAX_ITEMS_PER_SESSION",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_MAX_TEXT_CHARS",
    "DEFAULT_TTL_SECONDS",
    "OneBotOutboundObserver",
    "OutboundEntry",
    "OutboundLedger",
    "extract_onebot_message_text",
    "is_owned_outbound_send",
    "owned_outbound_send",
]
