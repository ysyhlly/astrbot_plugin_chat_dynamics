"""Request-local, body-bearing original input for owned private turns.

This capability is attached only to the owned event clone. It exposes typed
source parts to a cooperating memory hook without treating the rewritten
planning prompt, quotes, or attachments as user-authored plain text.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import weakref
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .platform_bridge import iter_message_components

MAX_MESSAGES = 32
MAX_PARTS_PER_MESSAGE = 128
MAX_TEXT_CHARS_PER_MESSAGE = 64_000
MAX_TOTAL_TEXT_CHARS = 256_000


def _message_type_name(event: Any) -> str:
    getter = getattr(event, "get_message_type", None)
    if not callable(getter):
        return ""
    try:
        value = getter()
    except Exception:
        return ""
    name = getattr(value, "name", None)
    if not isinstance(name, str):
        name = str(value)
    return name.rsplit(".", 1)[-1].strip().upper()


def is_private_message_event(event: Any) -> bool:
    """Recognize explicit single-recipient AstrBot message enum values."""
    return _message_type_name(event) in {"FRIEND_MESSAGE", "PRIVATE_MESSAGE"}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _read_occurred_at(event: Any) -> float | None:
    message_obj = getattr(event, "message_obj", None)
    candidates = (
        getattr(message_obj, "timestamp", None),
        getattr(message_obj, "time", None),
        getattr(event, "timestamp", None),
    )
    for candidate in candidates:
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
            continue
        value = float(candidate)
        if not math.isfinite(value) or value <= 0:
            continue
        # Some adapters report milliseconds. The ledger stores seconds.
        if value > 100_000_000_000:
            value /= 1000.0
        return value
    return None


def _component_name(component: Any) -> str:
    if isinstance(component, str):
        return "plain"
    value = getattr(component, "type", None)
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return type(component).__name__.lower()


def _quoted_author(component: Any) -> str | None:
    for name in ("quoted_author_id", "author_id", "sender_id", "user_id"):
        value = getattr(component, name, None)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = str(value).strip()
            if value:
                return value
    return None


def _typed_part(component: Any, part_index: int) -> tuple[dict[str, Any], int]:
    name = _component_name(component)
    if name in {"plain", "text"}:
        text = component if isinstance(component, str) else getattr(component, "text", None)
        if not isinstance(text, str):
            return {"kind": "other", "part_index": part_index}, 0
        return {
            "kind": "text",
            "part_index": part_index,
            "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "char_count": len(text),
        }, len(text)
    if name in {"reply", "quote"}:
        return {
            "kind": "quote",
            "part_index": part_index,
            "quoted_author_id": _quoted_author(component),
        }, 0
    if name in {"at", "atall", "mention"}:
        return {"kind": "mention", "part_index": part_index}, 0
    if name in {"image", "sticker", "wechatemoji", "emoji", "animatedemoji"}:
        return {"kind": "image", "part_index": part_index}, 0
    if name in {"record", "audio", "voice"}:
        return {"kind": "audio", "part_index": part_index}, 0
    if name == "video":
        return {"kind": "video", "part_index": part_index}, 0
    if name == "file":
        return {"kind": "file", "part_index": part_index}, 0
    if name in {"think", "thinkpart"}:
        return {"kind": "think", "part_index": part_index}, 0
    return {"kind": "other", "part_index": part_index}, 0


def _strict_getter_text(event: Any, name: str) -> str | None:
    try:
        getter = getattr(event, name, None)
        if not callable(getter):
            return None
        value = getter()
    except Exception:
        return None
    if inspect.isawaitable(value):
        close = getattr(value, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.casefold() in {"none", "unknown", "bot"}:
        return None
    return normalized


def _scope_and_author(event: Any) -> tuple[dict[str, str], str, str | None, float | None] | None:
    if not is_private_message_event(event):
        return None
    try:
        # Validate the adapter's raw values before any parser can stringify them.
        # Keep the same normalization contract as parse_group_event for identity:
        # UMO is stripped, platform is stripped/lowercased/capped, and self/author
        # IDs are stripped without case folding or truncation.
        umo_raw = getattr(event, "unified_msg_origin", None)
        platform_raw = _strict_getter_text(event, "get_platform_name")
        self_id_raw = _strict_getter_text(event, "get_self_id")
        author_raw = _strict_getter_text(event, "get_sender_id")
        if not isinstance(umo_raw, str):
            return None
        umo = umo_raw.strip()
        if not umo or umo.casefold() in {"none", "unknown", "bot"}:
            return None
        if platform_raw is None or self_id_raw is None or author_raw is None:
            return None
        platform = platform_raw.lower()[:64]
        self_id = self_id_raw
        author_id = author_raw
        if not platform or platform.casefold() in {"none", "unknown", "bot"}:
            return None
        if self_id == author_id:
            return None

        # Preserve parse_group_event's message ID precedence and numeric-to-text
        # behavior, without using it to normalize untrusted identity getters.
        message_id = getattr(event, "message_id", None)
        if not message_id:
            message_obj = getattr(event, "message_obj", None)
            message_id = getattr(message_obj, "message_id", None) if message_obj is not None else None
        message_id = str(message_id) if message_id else ""
        occurred_at = _read_occurred_at(event)
        scope = {
            "umo": umo,
            "platform": platform,
            "self_id": self_id,
        }
    except Exception:
        return None
    return scope, author_id, message_id or None, occurred_at


def _event_key(
    scope: Mapping[str, str],
    author_id: str,
    platform_message_id: str | None,
    occurred_at: float | None,
    parts: tuple[Mapping[str, Any], ...],
) -> str:
    if platform_message_id:
        identity = {
            "scope": dict(scope),
            "platform_message_id": platform_message_id,
            "author_id": author_id,
        }
    else:
        identity = {
            "scope": dict(scope),
            "author_id": author_id,
            "occurred_at": occurred_at,
            "parts": [
                {key: part.get(key) for key in ("kind", "sha256", "char_count", "quoted_author_id")}
                for part in parts
            ],
        }
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "cd-original-v1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _same_scope(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("umo", "platform", "self_id"))


class OriginalInputResolver:
    """Callable capability bound to one private owned event clone."""

    def __init__(self, clone_event: Any, scope: Mapping[str, str], author_id: str, dto: Mapping[str, Any]):
        self._clone_ref = weakref.ref(clone_event)
        self._scope = dict(scope)
        self._author_id = author_id
        self._dto = _freeze(dto)

    def __call__(self, candidate_event: Any) -> Mapping[str, Any] | None:
        clone = self._clone_ref()
        if clone is None or candidate_event is not clone:
            return None
        if getattr(candidate_event, "_cd_owned_original_input_v1", False) is not True:
            return None
        candidate = _scope_and_author(candidate_event)
        if candidate is None:
            return None
        scope, author_id, _message_id, _occurred_at = candidate
        if not _same_scope(scope, self._scope) or author_id != self._author_id:
            return None
        return self._dto


def build_original_input_resolver(
    *, clone_event: Any, trigger_event: Any, source_events: Any
) -> OriginalInputResolver | None:
    """Capture original typed events before the owned clone receives its prompt."""
    trigger = _scope_and_author(trigger_event)
    if trigger is None:
        return None
    scope, trigger_author, _trigger_message_id, _trigger_time = trigger
    try:
        raw_events = tuple(source_events or (trigger_event,))
    except Exception:
        return None
    if not raw_events or len(raw_events) > MAX_MESSAGES:
        return None

    trigger_positions = [index for index, raw in enumerate(raw_events) if raw is trigger_event]
    if len(trigger_positions) != 1:
        return None

    messages: list[dict[str, Any]] = []
    message_fingerprints: dict[str, str] = {}
    total_text_chars = 0
    current_event_key: str | None = None
    for raw in raw_events:
        identity = _scope_and_author(raw)
        if identity is None:
            return None
        raw_scope, author_id, platform_message_id, occurred_at = identity
        if not _same_scope(raw_scope, scope) or author_id != trigger_author:
            return None
        try:
            components = tuple(iter_message_components(raw))
        except Exception:
            return None
        if len(components) > MAX_PARTS_PER_MESSAGE:
            return None
        parts: list[dict[str, Any]] = []
        message_text_chars = 0
        for part_index, component in enumerate(components):
            try:
                part, text_chars = _typed_part(component, part_index)
            except ValueError:
                return None
            total_text_chars += text_chars
            message_text_chars += text_chars
            if (message_text_chars > MAX_TEXT_CHARS_PER_MESSAGE
                    or total_text_chars > MAX_TOTAL_TEXT_CHARS):
                return None
            parts.append(part)
        frozen_parts = tuple(parts)
        event_key = _event_key(scope, author_id, platform_message_id, occurred_at, frozen_parts)
        fingerprint = json.dumps(
            {"parts": parts, "occurred_at": occurred_at},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        previous = message_fingerprints.get(event_key)
        if previous is not None:
            if previous != fingerprint:
                return None
            if raw is trigger_event:
                current_event_key = event_key
            continue
        message_fingerprints[event_key] = fingerprint
        messages.append({
            "event_key": event_key,
            "platform_message_id": platform_message_id,
            "author_id": author_id,
            "occurred_at": occurred_at,
            "parts": frozen_parts,
        })
        if raw is trigger_event:
            current_event_key = event_key

    if not current_event_key:
        return None
    dto = {
        "schema_version": 1,
        "scope": dict(scope),
        "current_event_key": current_event_key,
        "messages": tuple(messages),
    }
    try:
        return OriginalInputResolver(clone_event, scope, trigger_author, dto)
    except TypeError:
        # If the clone cannot be weak-referenced, do not replace identity binding
        # with a self-asserted dictionary or a weak ID-only check.
        return None


__all__ = ["OriginalInputResolver", "build_original_input_resolver", "is_private_message_event"]
