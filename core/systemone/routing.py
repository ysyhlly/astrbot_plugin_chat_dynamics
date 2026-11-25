"""Resolve Chat Dynamics' selected decision Provider on every request."""

import asyncio
import json
import math
import time

from .client import SystemOneError
from . import window
from .native import ask_native, is_decision_provider
from .usage_context import capture_decision_usage


def _extract_group_id(state, group_id: str = "") -> str:
    clean = str(group_id or "").strip()
    if clean:
        return clean
    if isinstance(state, dict):
        for key in ("group_id", "session_key", "umo"):
            val = state.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        conv = state.get("conversation")
        if isinstance(conv, dict):
            for key in ("group_id", "session_key", "umo"):
                val = conv.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


class RoutedSystemOneClient:
    is_systemone_router = True

    def __init__(self, host, fallback):
        self.host = host
        self.fallback = fallback
        self._closed = False
        self._generation = 0
        self._last_identity = None
        self.last_context_budget = None
        self.last_usage = None
        self._status = "configured"
        self._detail = "not_called"
        self.calls = 0
        self.failures = 0
        self.tone_review_calls = 0
        self.tone_review_failures = 0
        self.last_latency_ms = 0

    def __getattr__(self, name):
        return getattr(self.fallback, name)

    def configure(self, **kwargs):
        self._generation += 1
        self._status, self._detail = "configured", "not_called"
        self.fallback.configure(**kwargs)

    @property
    def context_window_tokens(self):
        _, provider, _ = self._resolve()
        return getattr(provider, "context_window_tokens", None)

    def _resolve(self):
        config = self.host._runtime_config
        provider_id = str(getattr(config, "decision_provider_id", "") or "")
        if not provider_id:
            return "", None, None
        getter = getattr(self.host.context, "get_provider_by_id", None)
        provider = getter(provider_id) if callable(getter) else None
        identity = (
            provider_id,
            id(provider),
            provider.get_model() if provider else None,
            id(config),
            self._generation,
        )
        return provider_id, provider, identity

    def snapshot(self):
        provider_id, provider, identity = self._resolve()
        if not provider_id:
            return self.fallback.snapshot()
        supported = is_decision_provider(provider)
        if (
            self._closed
            or getattr(self.host._runtime_config, "decision_backend", "") != "jev"
        ):
            return {
                "configured": False,
                "available": False,
                "status": "disabled",
                "detail": "decision_backend_not_jev",
            }
        current = identity == self._last_identity
        return {
            "configured": bool(supported),
            "available": supported and current and self._status == "available",
            "status": (self._status if current else "configured")
            if supported
            else "missing",
            "detail": (self._detail if current else "not_called")
            if supported
            else "invalid_systemone_provider",
            "provider_id": provider_id,
            "protocol": getattr(provider, "decision_protocol", "systemone" if
                getattr(provider,'is_systemone_provider',False) else 'native_decision') if supported else "",
            "model": provider.get_model() if supported else "",
            "context_window_tokens": self.context_window_tokens,
            "calls": self.calls,
            "failures": self.failures,
            "tone_review_calls": self.tone_review_calls,
            "tone_review_failures": self.tone_review_failures,
            "last_latency_ms": self.last_latency_ms if current else 0,
            "context_budget": self.last_context_budget if current else None,
            "usage": self.last_usage if current else None,
        }

    def tone_selection(self):
        """Bind a semantic follow-up to the same provider, config and generation."""
        if self._closed:
            return None
        try:
            return self._resolve()[2]
        except Exception:
            return None

    async def evaluate_tone(self, *, state, questions, timeout, selection, group_id: str = ""):
        if selection is None or self.tone_selection() != selection:
            return None
        return await self._evaluate(state, questions, timeout, tone=True, group_id=group_id)

    async def evaluate(self, *, state, questions, timeout=None, group_id: str = ""):
        return await self._evaluate(state, questions, timeout, tone=False, group_id=group_id)

    async def _evaluate(self, state, questions, timeout, *, tone, group_id: str = ""):
        if self._closed:
            return None
        provider_id, provider, identity = self._resolve()
        if not provider_id:
            return await self.fallback.evaluate(
                state=state, questions=questions, timeout=timeout
            )
        if (
            getattr(self.host._runtime_config, "decision_backend", "") != "jev"
            or provider is None
            or not is_decision_provider(provider)
        ):
            self._status, self._detail = "missing", "invalid_systemone_provider"
            return None
        self.calls += 1
        if tone:
            self.tone_review_calls += 1
        else:
            self._last_identity = identity
        completed = False
        budget = None
        error_code = None
        started = time.perf_counter()
        resolved_group_id = _extract_group_id(state, group_id)
        usage_cm = capture_decision_usage()
        usage_sink = usage_cm.__enter__()
        try:
            timeout = float(timeout if timeout is not None else getattr(provider, 'timeout', 30))
            if not math.isfinite(timeout) or timeout <= 0:
                raise SystemOneError('Invalid decision timeout.', code='context_count_timeout')
            from ..jev_decision import MAX_TOTAL_REQUEST_CHARS, request_size
            # The decision builder reserves 256 model characters. A provider
            # may use a longer ID, so check its actual name before any HTTP IO.
            model = str(provider.get_model() or "")
            size = request_size(state, questions, model=model)
            wide = window.enabled(provider)
            if size > (window.WIRE_BYTES if wide else MAX_TOTAL_REQUEST_CHARS):
                raise SystemOneError("System One 请求超出决策预算。", code="request_budget_exceeded")
            counter = getattr(provider, 'systemone_count_tokens', None)
            if callable(counter):
                from .context_budget import validate_counts, fits, shrink_prose
                for attempt in range(4):
                    remaining = timeout - (time.perf_counter() - started)
                    if remaining <= 0:
                        raise SystemOneError('Token budget check timed out.', code='context_count_timeout')
                    counted = await asyncio.wait_for(counter(state=state, questions=questions,
                        timeout=min(3, remaining)), timeout=remaining)
                    if counted is None:
                        break
                    counts, limits = validate_counts(counted)
                    budget = {'counts':counts, 'limits':limits, 'shrink_passes':attempt,
                                                'state_characters':len(json.dumps(state,ensure_ascii=False))}
                    character = state.get('character_state', {}) if isinstance(state,dict) else {}
                    budget['character_state_summary'] = {
                        'energy_level': character.get('energy',{}).get('level','unknown'),
                        'stale': bool(character.get('stale')),
                        'active_commitments': len(character.get('commitments',[])),
                        'context_items': len(character.get('context_items',[]))}
                    if fits(counts, limits):
                        break
                    if attempt == 3:
                        raise SystemOneError('Token window exceeded.', code='token_budget_exceeded')
                    state = shrink_prose(state, counts, limits)
            remaining = timeout - (time.perf_counter() - started)
            if remaining <= 0:
                raise SystemOneError('Decision deadline exceeded.', code='context_count_timeout')
            evaluator = getattr(provider, 'systemone_evaluate', None)
            if callable(evaluator):
                for retry in range(3):
                    try:
                        answers = await evaluator(state=state, questions=questions, timeout=remaining)
                        if wide:
                            budget = {
                                "context_window_tokens": window.CONTEXT_TOKENS,
                                "shrink_passes": retry,
                                "state_characters": len(json.dumps(state, ensure_ascii=False)),
                            }
                        break
                    except SystemOneError as exc:
                        if not wide or exc.code not in {"context_length_exceeded", "payload_too_large"} or retry == 2:
                            raise
                        reduced = window.reduce_after_overflow(state, questions, retry)
                        if reduced == state:
                            raise
                        state = reduced
                        remaining = timeout - (time.perf_counter() - started)
                        if remaining <= 0 or self._closed or self._resolve()[2] != identity:
                            raise
            else:
                answers = await asyncio.wait_for(ask_native(provider,state=state,questions=questions),
                                                 timeout=remaining)
            completed = True
        except asyncio.CancelledError:
            raise
        except SystemOneError as exc:
            error_code = exc.code
        except TimeoutError:
            error_code = "timeout"
        except Exception:
            error_code = "systemone_provider_failed"
        finally:
            usage_cm.__exit__(None, None, None)
            elapsed_s = max(0.0, time.perf_counter() - started)
            if completed:
                usage_obj = usage_sink[-1] if usage_sink else getattr(provider, "last_decision_usage", None)
            else:
                usage_obj = None
            if not tone:
                self.last_context_budget = budget
                self.last_usage = usage_obj
                self.last_latency_ms = round(elapsed_s * 1000)
            try:
                from ... import series_panels

                ctx = getattr(self.host, "context", None)
                if ctx is not None:
                    usage_payload = {"usage": usage_obj} if isinstance(usage_obj, dict) else usage_obj
                    model_name = ""
                    get_model = getattr(provider, "get_model", None)
                    if callable(get_model):
                        try:
                            model_name = str(get_model() or "")
                        except Exception:
                            model_name = ""
                    series_panels.record_series_llm_usage(
                        ctx,
                        plugin_id="astrbot_plugin_chat_dynamics",
                        provider_id=provider_id or "decision",
                        purpose="tone_review" if tone else "decision",
                        response=usage_payload,
                        generation_seconds=elapsed_s,
                        ok=completed and error_code is None,
                        error_type="" if (completed and error_code is None) else str(error_code or "decision_failed"),
                        model=model_name,
                        group_id=resolved_group_id,
                    )
            except Exception:
                pass
        if error_code is not None:
            self.failures += 1
            if tone:
                self.tone_review_failures += 1
            else:
                self._status, self._detail = "degraded", error_code
            return None
        # A UI model/provider switch must not deliver an answer from the old selection.
        if self._closed or self._resolve()[2] != identity:
            return None
        if not tone:
            self._status, self._detail = "available", "ready"
        return answers

    async def close(self):
        self._closed = True
        await self.fallback.close()

    def detach(self):
        self._closed = True
        self._generation += 1
