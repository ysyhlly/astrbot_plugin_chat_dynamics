"""Opt-in, bounded capture of the plugin's final visible provider input.

This is diagnostic evidence only. It does not claim to capture the provider's
native wire request and never reads a provider configuration object.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_MAX_TRACE_BYTES = 1024 * 1024
_MAX_TEXT_CHARS = 256 * 1024
_MAX_CONTEXT_CHARS = 768 * 1024
_MAX_RESPONSE_CHARS = 128 * 1024
_MAX_GATE_BYTES = 4096
_BASE_ARGUMENTS = (
    "prompt", "session_id", "image_urls", "audio_urls", "func_tool",
    "contexts", "system_prompt", "tool_calls_result", "model",
    "extra_user_content_parts", "tool_choice", "request_max_retries",
)
_INPUT_KEYS = {"prompt", "system_prompt", "contexts", "model"}
_NON_GENERATION_KEYS = _INPUT_KEYS | {
    "session_id", "image_urls", "audio_urls", "func_tool", "tool_calls_result",
    "extra_user_content_parts", "tool_choice", "request_max_retries",
}
_GENERATION_KEYS = {
    "temperature", "top_p", "top_k", "max_tokens", "max_output_tokens",
    "max_completion_tokens", "stop", "seed", "presence_penalty",
    "frequency_penalty", "candidate_count", "response_mime_type",
    "response_schema", "thinking_config", "reasoning_effort",
}
_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|credential)", re.I)
_SECRET_TEXT = re.compile(
    r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}|\bAIza[0-9A-Za-z_-]{20,}|\bsk-[A-Za-z0-9_-]{16,}|"
    r"((?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[:=]\s*)[^\s,;]+|"
    r"([?&](?:api[_-]?key|access[_-]?token|token|key)=)[^&\s]+"
)
_MEDIA_DATA = re.compile(r"(?i)^data:[^,]{0,100};base64,")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json_safe(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Copy only JSON-shaped values; never fall back to repr or arbitrary attrs."""
    if budget is None:
        budget = [0, 0]
    if depth > 24 or budget[0] > _MAX_CONTEXT_CHARS:
        raise ValueError("capture_value_limit")
    if value is None or type(value) in (bool, int, float):
        if type(value) is float and (value != value or abs(value) == float("inf")):
            raise ValueError("capture_nonfinite_number")
        budget[0] += 8
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_CHARS:
            raise ValueError("capture_text_limit")
        budget[0] += len(value.encode("utf-8", "replace"))
        if _MEDIA_DATA.match(value):
            budget[1] += 1
            return "[media payload omitted]"
        def redact(match):
            budget[1] += 1
            prefix = next((group for group in match.groups() if group), "")
            return prefix + "[redacted]"
        return _SECRET_TEXT.sub(redact, value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("capture_nonstring_key")
            if _SENSITIVE_KEY.search(key):
                budget[1] += 1
                result[key] = "[redacted]"
                continue
            result[key] = _json_safe(item, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1, budget=budget) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, dict):
            return _json_safe(data, depth=depth + 1, budget=budget)
    raise ValueError("capture_unsupported_value")


def _request_values(provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Resolve explicit arguments without calling the provider or filling defaults."""
    values: dict[str, Any] = {}
    try:
        signature = inspect.signature(provider.text_chat)
        bound = signature.bind_partial(*args, **kwargs)
        values.update(bound.arguments)
    except Exception:
        for index, value in enumerate(args[:len(_BASE_ARGUMENTS)]):
            values[_BASE_ARGUMENTS[index]] = value
        values.update(kwargs)
    return values


def _tool_schemas(tool_set: Any, budget: list[int] | None = None) -> list[dict[str, Any]] | None:
    if tool_set is None:
        return []
    tools = getattr(tool_set, "tools", None)
    if not isinstance(tools, (list, tuple)):
        return None
    result = []
    for tool in tools:
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None)
        parameters = getattr(tool, "parameters", None)
        if not isinstance(name, str) or not isinstance(description, str) or not isinstance(parameters, dict):
            return None
        try:
            result.append({"name": _json_safe(name, budget=budget),
                           "description": _json_safe(description, budget=budget),
                           "schema": _json_safe(parameters, budget=budget)})
        except Exception:
            return None
    return result


def _provider_metadata(provider: Any, explicit: dict[str, Any]) -> dict[str, Any]:
    provider_id = explicit.get("provider_id")
    model = explicit.get("model")
    provider_type = explicit.get("provider_type")
    provider_id_source = explicit.get("provider_id_source") or (
        "unknown" if provider_id in (None, "unknown") else "explicit_metadata")
    model_source = ("call_argument" if isinstance(model, str) else
                    "provider_meta" if isinstance(explicit.get("provider_model"), str) else "unknown")
    if not isinstance(model, str):
        model = explicit.get("provider_model")
    chain = []
    current = provider
    seen: set[int] = set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        cls = type(current)
        chain.append(f"{cls.__module__}.{cls.__name__}")
        state = getattr(current, "__dict__", {})
        if isinstance(state, dict):
            class_fields = getattr(cls, "__dict__", {})
            if provider_id is None:
                candidate = state.get("provider_id", class_fields.get("provider_id"))
                if isinstance(candidate, str):
                    provider_id = candidate
                    provider_id_source = "provider_attribute"
            if model is None:
                candidate = state.get("model", class_fields.get("model"))
                if isinstance(candidate, str):
                    model = candidate
                    model_source = "provider_attribute"
            current = state.get("provider")
        else:
            break
    return {
        "provider_chain": chain,
        "provider_id": provider_id if isinstance(provider_id, str) else "unknown",
        "provider_id_source": provider_id_source,
        "model": model if isinstance(model, str) else "unknown",
        "model_source": model_source,
        "provider_type": provider_type if isinstance(provider_type, str) else "unknown",
        "provider_config": "unknown_not_read",
    }


def known_provider_id(provider: Any) -> str | None:
    """Return only an explicit provider_id field; never infer from config or model."""
    current = provider
    seen: set[int] = set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        cls = type(current)
        state = getattr(current, "__dict__", {})
        class_fields = getattr(cls, "__dict__", {})
        candidate = state.get("provider_id") if isinstance(state, dict) else None
        if candidate is None:
            candidate = class_fields.get("provider_id")
        if isinstance(candidate, str) and candidate and not _SECRET_TEXT.search(candidate):
            return candidate[:128]
        current = state.get("provider") if isinstance(state, dict) else None
    return None


def known_provider_metadata(provider: Any) -> dict[str, str | None] | None:
    """Read only the framework's safe ProviderMeta for known base implementations."""
    try:
        from astrbot.core.provider.entities import ProviderMeta
        from astrbot.core.provider.provider import AbstractProvider
    except Exception:
        return None
    current = provider
    seen: set[int] = set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        cls = type(current)
        if (isinstance(current, AbstractProvider)
                and cls.meta is AbstractProvider.meta
                and cls.get_model is AbstractProvider.get_model
                and cls.__module__.startswith("astrbot.core.provider.sources.")):
            try:
                meta = AbstractProvider.meta(current)
            except Exception:
                return None
            if not isinstance(meta, ProviderMeta):
                return None
            values = {"id": meta.id, "model": meta.model, "type": meta.type}
            if any(not isinstance(value, str) for value in (values["id"], values["type"])):
                return None
            for key, value in values.items():
                if value is not None and (_SECRET_TEXT.search(value) or len(value) > 128):
                    return None
            return values
        state = getattr(current, "__dict__", {})
        current = state.get("provider") if isinstance(state, dict) else None
    return None


def _plain_completion(response: Any) -> str | None:
    """Read public plain completion text only; exclude reasoning and raw objects."""
    try:
        value = getattr(response, "completion_text", None)
        if isinstance(value, str):
            return value[:_MAX_RESPONSE_CHARS]
        chain = getattr(response, "result_chain", None)
        get_plain = getattr(chain, "get_plain_text", None)
        if callable(get_plain):
            value = get_plain()
            if isinstance(value, str):
                return value[:_MAX_RESPONSE_CHARS]
    except Exception:
        return None
    return None


class ToneCaptureSession:
    """A per-turn identity pinned to the shared, on-disk three-call gate."""

    def __init__(self, data_dir: Path, *, umo: str, actor_id: str, persona_id: str,
                 provider_id: str | None = None, provider_id_source: str | None = None,
                 provider_model: str | None = None, provider_type: str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.gate_path = self.data_dir / "tone-capture.gate.json"
        self.trace_dir = self.data_dir / "tone-capture"
        self.trace_path = self.trace_dir / "trace.jsonl"
        self.umo = str(umo or "")
        self.actor_id = str(actor_id or "")
        self.persona_id = str(persona_id or "")
        self.provider_id = provider_id if isinstance(provider_id, str) else None
        self.provider_id_source = provider_id_source
        self.provider_model = provider_model
        self.provider_type = provider_type

    def _reserve(self) -> int | None:
        if not (self.umo and self.actor_id and self.persona_id):
            return None
        with _LOCK:
            if self.gate_path.is_symlink() or not self.gate_path.is_file():
                return None
            try:
                info = self.gate_path.stat()
                if info.st_size > _MAX_GATE_BYTES:
                    return None
                if os.name != "nt" and (stat.S_IMODE(info.st_mode) != 0o600
                                          or info.st_uid != os.getuid()):
                    return None
                flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.gate_path, flags)
                with os.fdopen(fd, "r+", encoding="utf-8") as stream:
                    try:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    except ImportError:
                        pass
                    raw = stream.read(_MAX_GATE_BYTES + 1)
                    if len(raw.encode("utf-8")) > _MAX_GATE_BYTES:
                        return None
                    gate = json.loads(raw)
                    if not isinstance(gate, dict) or set(gate) - {
                            "enabled", "umo", "actor_id", "persona_id", "created_at",
                            "expires_at", "max_calls", "reserved_calls"}:
                        return None
                    if not {"enabled", "umo", "actor_id", "persona_id", "created_at",
                            "expires_at", "max_calls"} <= set(gate):
                        return None
                    now = time.time()
                    if (type(gate["created_at"]) not in (int, float)
                            or type(gate["expires_at"]) not in (int, float)
                            or type(gate["max_calls"]) is not int
                            or type(gate.get("reserved_calls", 0)) is not int):
                        return None
                    created = float(gate["created_at"])
                    expires = float(gate["expires_at"])
                    max_calls = gate["max_calls"]
                    reserved = gate.get("reserved_calls", 0)
                    if (gate.get("enabled") is not True or gate.get("umo") != self.umo
                            or gate.get("actor_id") != self.actor_id
                            or gate.get("persona_id") != self.persona_id
                            or not math.isfinite(created) or not math.isfinite(expires)
                            or expires <= now or created > now or expires - created > 1800
                            or not 1 <= max_calls <= 3 or not 0 <= reserved < max_calls):
                        return None
                    reserved += 1
                    gate["reserved_calls"] = reserved
                    stream.seek(0)
                    json.dump(gate, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                    return reserved
            except Exception:
                return None

    def reserve_call(self) -> int | None:
        """Reserve the shared budget before wrapping or consuming a stream."""
        return self._reserve()

    def _build_record(self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any],
                      *, call_index: int, mode: str, status: str,
                      response_text: str | None, error_type: str | None = None) -> dict[str, Any]:
        values = _request_values(provider, args, kwargs)
        input_data: dict[str, Any] = {}
        omitted = []
        privacy_budget = [0, 0]
        for key in ("prompt", "system_prompt", "contexts"):
            if key in values:
                try:
                    input_data[key] = _json_safe(values[key], budget=privacy_budget)
                except Exception:
                    input_data[key] = "[capture omitted: unsupported or over limit]"
                    omitted.append(key)
        raw_contexts = values.get("contexts")
        if "contexts" not in input_data and raw_contexts is not None:
            input_data["contexts"] = "[capture omitted: unsupported or over limit]"
            omitted.append("contexts")
        for key in ("image_urls", "audio_urls", "extra_user_content_parts", "tool_calls_result"):
            if values.get(key):
                omitted.append(key)
        input_size = len(json.dumps(input_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if input_size > _MAX_CONTEXT_CHARS:
            input_data = {key: value for key, value in input_data.items() if key != "contexts"}
            input_data["contexts"] = "[capture omitted: contexts size limit]"
            omitted.append("contexts")
        generation = {}
        unrecorded = []
        for key, value in values.items():
            if key in _GENERATION_KEYS:
                try:
                    generation[key] = _json_safe(value)
                except Exception:
                    unrecorded.append(key)
            elif key not in _NON_GENERATION_KEYS and key != "self":
                if isinstance(key, str) and not _SENSITIVE_KEY.search(key):
                    unrecorded.append(key)
        tool_info = _tool_schemas(values.get("func_tool"), budget=privacy_budget)
        if tool_info is None:
            tool_info = "[tool schema unavailable]"
            omitted.append("tool_schema")
        if response_text is not None:
            try:
                response_text = _json_safe(response_text, budget=privacy_budget)
            except Exception:
                response_text = "[completion omitted: capture limit]"
        return {
            "schema_version": 1,
            "capture_id": uuid.uuid4().hex,
            "captured_at_utc": _utc_now(),
            "call_index": call_index,
            "call_mode": mode,
            "stream_status": status,
            "error_type": error_type if isinstance(error_type, str) else None,
            "scope": {"umo": self.umo, "actor_id": self.actor_id, "persona_id": self.persona_id},
            "provider": _provider_metadata(provider, {"provider_id": self.provider_id,
                                                       "provider_id_source": self.provider_id_source,
                                                       "provider_model": self.provider_model,
                                                       "provider_type": self.provider_type,
                                                       "model": values.get("model")}),
            "input": input_data,
            "input_omissions": sorted(set(omitted)),
            "tools": tool_info,
            "generation_parameters_explicit_only": generation,
            "unrecorded_argument_names": sorted(set(unrecorded)),
            "generation_defaults": "unknown_not_filled",
            "privacy_redactions": privacy_budget[1],
            "capture_exactness": ("redacted" if privacy_budget[1]
                                  else "incomplete" if omitted
                                  else "exact_for_captured_fields"),
            "completion_text": response_text,
            "completion_truncated": bool(response_text is not None and len(response_text) >= _MAX_RESPONSE_CHARS),
        }

    def _write(self, record: dict[str, Any]) -> None:
        """Best-effort append; all failures are intentionally swallowed by caller."""
        encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        with _LOCK:
            if self.trace_dir.is_symlink() or self.trace_path.is_symlink():
                return
            self.trace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(self.trace_dir, 0o700)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.trace_path, flags, 0o600)
            try:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except ImportError:
                    pass
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                current_size = os.fstat(fd).st_size
                if current_size + len(encoded) > _MAX_TRACE_BYTES:
                    return
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise OSError("short_capture_write")
                os.fsync(fd)
            finally:
                os.close(fd)

    def _finish(self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any],
                call_index: int | None, *, mode: str, status: str,
                response: Any = None, error_type: str | None = None) -> None:
        if call_index is None:
            return
        try:
            record = self._build_record(provider, args, kwargs, call_index=call_index,
                                        mode=mode, status=status,
                                        response_text=_plain_completion(response) if response is not None else None,
                                        error_type=error_type)
            self._write(record)
        except Exception:
            return

    async def text_chat(self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any],
                        *, call_index: int) -> Any:
        try:
            response = await provider.text_chat(*args, **kwargs)
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "provider_error"
            self._finish(provider, args, kwargs, call_index, mode="text_chat",
                         status=status, response=None, error_type=type(exc).__name__)
            raise
        self._finish(provider, args, kwargs, call_index, mode="text_chat", status="completed", response=response)
        return response

    async def text_chat_stream(self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any],
                               *, call_index: int):
        stream = provider.text_chat_stream(*args, **kwargs)
        status = "completed"
        final_response = None
        source_error = None
        try:
            async for response in stream:
                final_response = response
                yield response
        except asyncio.CancelledError as exc:
            status = "cancelled"
            source_error = exc
            raise
        except GeneratorExit as exc:
            status = "closed_early"
            source_error = exc
            raise
        except BaseException as exc:
            status = "provider_error"
            source_error = exc
            raise
        finally:
            close = getattr(stream, "aclose", None)
            try:
                try:
                    if close:
                        await close()
                except BaseException:
                    if source_error is None:
                        raise
            finally:
                self._finish(provider, args, kwargs, call_index, mode="text_chat_stream",
                             status=status, response=final_response,
                             error_type=type(source_error).__name__ if source_error else None)
