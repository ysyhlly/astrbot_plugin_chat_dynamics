"""Body-free tracing for the final owned private provider boundary."""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
from collections import Counter
from collections.abc import Mapping
from typing import Any

_LOGGER = logging.getLogger("astrbot_plugin_chat_dynamics.private_provider_trace")
_HOOK_OUTCOMES = frozenset({
    "ready", "partial", "empty", "deadline_exceeded", "cancelled", "busy",
    "error", "original_input_unavailable", "disabled",
})
_INPUT_STATUSES = frozenset({"valid", "unavailable", "not_owned"})
_SOURCE_LEVELS = frozenset({"verified_quote", "legacy_summary", "other"})
_TEXT_KEYS = frozenset({"text", "content", "prompt"})
_CONTAINER_KEYS = frozenset({"messages", "contents", "contexts", "parts", "input", "message"})
_OPAQUE_TYPES = frozenset({
    "image", "image_url", "input_image", "audio", "input_audio", "record", "voice",
    "video", "file", "file_data", "inline_data", "blob",
})
_MAX_ATTEMPTS = 32


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_digest_projection(value: Any) -> Any:
    """Stable, body-free canonical projection for the provider call arguments."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"number": "nonfinite"}
    if isinstance(value, str):
        raw = value.encode("utf-8", "replace")
        return {"string_chars": len(value), "string_sha256": _sha256(raw)}
    if isinstance(value, bytes):
        return {"bytes_length": len(value), "bytes_sha256": _sha256(value)}
    if isinstance(value, Mapping):
        pairs = []
        for key in sorted(value, key=lambda item: str(item)):
            pairs.append([str(key), _safe_digest_projection(value[key])])
        return {"mapping": pairs}
    if isinstance(value, (list, tuple)):
        return [_safe_digest_projection(item) for item in value]
    type_name = type(value).__name__
    # Attachments and SDK-specific parts remain opaque; never inspect URLs,
    # paths, binary payloads, or provider-specific object attributes.
    return {"opaque_type": type_name}


def _provider_args_sha256(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    try:
        value = json.dumps(
            {"args": _safe_digest_projection(args), "kwargs": _safe_digest_projection(kwargs)},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except Exception:
        value = b"provider-args-projection-unavailable"
    return _sha256(value)


def _iter_text_strings(value: Any, *, parent_key: str | None = None):
    if isinstance(value, str):
        if parent_key in _TEXT_KEYS:
            yield value
        return
    if isinstance(value, Mapping):
        part_type = str(value.get("type", "")).strip().lower()
        if part_type in _OPAQUE_TYPES:
            return
        if part_type in {"text", "input_text"}:
            text = value.get("text")
            if isinstance(text, str):
                yield text
            return
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in _TEXT_KEYS or key_text in _CONTAINER_KEYS:
                yield from _iter_text_strings(item, parent_key=key_text)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_text_strings(item, parent_key=parent_key)
        return
    name = type(value).__name__.lower()
    if name in _OPAQUE_TYPES:
        return
    if name in {"plain", "text", "textpart"}:
        text = getattr(value, "text", None)
        if isinstance(text, str):
            yield text


def _segments_from_receipt(getter: Any, request: Any, event: Any) -> tuple[str, str, list[dict[str, Any]]]:
    if not callable(getter):
        return "unavailable", "original_input_unavailable", []
    try:
        receipt = getter(request, event)
    except Exception:
        return "unavailable", "error", []
    if inspect.isawaitable(receipt) or not isinstance(receipt, Mapping):
        close = getattr(receipt, "close", None)
        if callable(close):
            close()
        return "unavailable", "error", []
    if type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != 1:
        return "unavailable", "error", []
    input_status = receipt.get("original_input_status")
    hook_outcome = receipt.get("hook_outcome")
    if input_status not in _INPUT_STATUSES or hook_outcome not in _HOOK_OUTCOMES:
        return "unavailable", "error", []
    raw_segments = receipt.get("segments")
    if not isinstance(raw_segments, (list, tuple)):
        return input_status, hook_outcome, []
    segments: list[dict[str, Any]] = []
    for item in raw_segments[:256]:
        if not isinstance(item, Mapping):
            continue
        digest = item.get("sha256")
        char_count = item.get("char_count")
        item_count = item.get("item_count")
        source_level = item.get("source_level")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
                or type(char_count) is not int or char_count < 0
                or type(item_count) is not int or item_count < 0
                or source_level not in _SOURCE_LEVELS):
            continue
        segments.append({
            "sha256": digest,
            "char_count": char_count,
            "item_count": item_count,
            "source_level": source_level,
        })
    return input_status, hook_outcome, segments


class PrivateProviderTraceState:
    """Accumulate bounded metrics without retaining request text or identities."""

    def __init__(self, request: Any, event: Any):
        self.request = request
        self.event = event
        self.attempts = 0
        self.guard_count = 0
        self.source_validation_outcome = "not_observed"
        self.provider_reached = False
        self.logged_calls = 0

    def validation_result(self, value: str) -> None:
        if value in {"passed", "failed", "cancelled", "error"}:
            self.source_validation_outcome = value

    def _receipt_match_counts(
        self, args: tuple[Any, ...], kwargs: Mapping[str, Any], segments: list[dict[str, Any]]
    ) -> dict[str, int]:
        actual = Counter(
            (_sha256(text.encode("utf-8")), len(text))
            for text in _iter_text_strings((args, kwargs))
        )
        retained = verified = legacy = chars = 0
        for segment in segments:
            key = (segment["sha256"], segment["char_count"])
            if not segment["item_count"] or actual[key] <= 0:
                continue
            actual[key] -= 1
            retained += segment["item_count"]
            chars += segment["char_count"]
            if segment["source_level"] == "verified_quote":
                verified += segment["item_count"]
            elif segment["source_level"] == "legacy_summary":
                legacy += segment["item_count"]
        return {
            "retained_snippets": retained,
            "verified_quote_count": verified,
            "legacy_summary_count": legacy,
            "retained_chars": chars,
        }

    def provider_call(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
        self.attempts += 1
        self.provider_reached = True
        try:
            input_status, hook_outcome, segments = _segments_from_receipt(
                getattr(self.request, "_lm_private_injection_v1", None),
                self.request, self.event,
            )
            counts = self._receipt_match_counts(args, kwargs, segments)
            if self.guard_count <= 0:
                validation = "guard_unavailable" if segments else "not_applicable"
            else:
                validation = self.source_validation_outcome
            return {
                "path": "native_private",
                "provider_reached": True,
                "hook_outcome": hook_outcome,
                "original_input_status": input_status,
                "source_validation_outcome": validation,
                **counts,
                "args_sha256": _provider_args_sha256(args, kwargs),
                "attempt_ordinal": self.attempts,
                # Provider-boundary entry alone is not an SDK/wire-body assertion.
                "injection_success": False,
            }
        except Exception:
            # Diagnostics must never prevent an otherwise valid provider call.
            return {
                "path": "native_private",
                "provider_reached": True,
                "hook_outcome": "error",
                "original_input_status": "unavailable",
                "source_validation_outcome": (
                    self.source_validation_outcome if self.guard_count else "guard_unavailable"
                ),
                "retained_snippets": 0,
                "verified_quote_count": 0,
                "legacy_summary_count": 0,
                "retained_chars": 0,
                "args_sha256": _provider_args_sha256(args, kwargs),
                "attempt_ordinal": self.attempts,
                "injection_success": False,
            }

    def emit_call(self, record: dict[str, Any], provider_result: str) -> None:
        if self.logged_calls >= _MAX_ATTEMPTS:
            return
        self.logged_calls += 1
        payload = dict(record, provider_result=provider_result)
        try:
            _LOGGER.info("private_native_provider_trace_v1 %s", json.dumps(
                payload, sort_keys=True, separators=(",", ":"),
            ))
        except Exception:
            pass

    def emit_no_provider_call(self) -> None:
        if self.provider_reached or self.logged_calls:
            return
        receipt_status = "not_observed"
        hook_outcome = "not_observed"
        payload = {
            "path": "native_private",
            "provider_reached": False,
            "hook_outcome": hook_outcome,
            "original_input_status": receipt_status,
            "source_validation_outcome": self.source_validation_outcome,
            "retained_snippets": 0,
            "verified_quote_count": 0,
            "legacy_summary_count": 0,
            "retained_chars": 0,
            "args_sha256": "not_observed",
            "attempt_ordinal": 0,
            "provider_result": "no_provider_call",
            "injection_success": False,
        }
        try:
            _LOGGER.info("private_native_provider_trace_v1 %s", json.dumps(
                payload, sort_keys=True, separators=(",", ":"),
            ))
        except Exception:
            pass


class PrivateProviderTrace:
    """Observe provider calls after the existing source/policy guard."""

    _cd_private_provider_trace_v1 = True

    def __init__(self, provider: Any, state: PrivateProviderTraceState):
        self.provider = provider
        self.state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    async def text_chat(self, *args: Any, **kwargs: Any):
        record = self.state.provider_call(args, kwargs)
        try:
            response = await self.provider.text_chat(*args, **kwargs)
        except BaseException as exc:
            result = "cancelled" if isinstance(exc, BaseException) and type(exc).__name__ == "CancelledError" else "error"
            self.state.emit_call(record, result)
            raise
        self.state.emit_call(record, "returned")
        return response

    async def text_chat_stream(self, *args: Any, **kwargs: Any):
        record = self.state.provider_call(args, kwargs)
        completed = False
        stream = None
        try:
            stream = self.provider.text_chat_stream(*args, **kwargs)
            async for item in stream:
                if not getattr(item, 'is_chunk', False):
                    completed = True
                yield item
        except BaseException as exc:
            result = "returned" if completed else "cancelled" if type(exc).__name__ == "CancelledError" else "error"
            self.state.emit_call(record, result)
            raise
        else:
            self.state.emit_call(record, "returned")
        finally:
            close = getattr(stream, 'aclose', None)
            if callable(close):
                await close()


def _find_native_validation_guard(root: Any):
    current = root
    seen: set[int] = set()
    for _ in range(16):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if (type(current).__name__ == "_NativeMemoryValidationProvider"
                and callable(getattr(current, "validate", None))
                and getattr(current, "provider", None) is not None):
            return current
        current = getattr(current, "provider", None)
    return None


def _find_leaf_and_parent(root: Any):
    parent = None
    current = root
    seen: set[int] = set()
    for _ in range(16):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        child = getattr(current, "provider", None)
        if child is None:
            break
        parent, current = current, child
    return parent, current


def _install_on_provider_root(provider: Any, state: PrivateProviderTraceState) -> bool:
    if provider is None:
        return False
    if getattr(provider, "_cd_private_provider_trace_v1", False):
        return True
    guard = _find_native_validation_guard(provider)
    if guard is not None:
        if getattr(guard, "_cd_private_validation_trace_v1", False):
            return True
        original_validate = guard.validate

        async def traced_validate(*args: Any, **kwargs: Any) -> bool:
            state.guard_count += 1
            try:
                outcome = await original_validate(*args, **kwargs)
            except BaseException as exc:
                state.validation_result(
                    "cancelled" if type(exc).__name__ == "CancelledError" else "error"
                )
                raise
            state.validation_result("passed" if outcome is True else "failed")
            return outcome

        try:
            guard.validate = traced_validate
            guard.provider = PrivateProviderTrace(guard.provider, state)
            guard._cd_private_validation_trace_v1 = True
            return True
        except Exception:
            state.source_validation_outcome = "guard_unavailable"
            return False
    parent, leaf = _find_leaf_and_parent(provider)
    try:
        trace = PrivateProviderTrace(leaf, state)
        if parent is None:
            return False
        parent.provider = trace
        return True
    except Exception:
        return False


def install_private_provider_trace(runner: Any, request: Any, event: Any) -> PrivateProviderTraceState:
    """Instrument primary and fallback provider chains in-place, once each."""
    state = PrivateProviderTraceState(request, event)
    roots = [getattr(runner, "provider", None), *list(getattr(runner, "fallback_providers", ()) or ())]
    installed = 0
    for provider in roots:
        if _install_on_provider_root(provider, state):
            installed += 1
    if not installed:
        state.source_validation_outcome = "guard_unavailable"
    return state


__all__ = ["PrivateProviderTraceState", "install_private_provider_trace"]
