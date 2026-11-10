"""Resolve Chat Dynamics' selected decision Provider on every request."""

import asyncio
import json
import math
import time

from .client import SystemOneError
from .native import ask_native, is_decision_provider


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
        self.last_latency_ms = 0

    def __getattr__(self, name):
        return getattr(self.fallback, name)

    def configure(self, **kwargs):
        self._generation += 1
        self._status, self._detail = "configured", "not_called"
        self.fallback.configure(**kwargs)

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
            "calls": self.calls,
            "failures": self.failures,
            "last_latency_ms": self.last_latency_ms if current else 0,
            "context_budget": self.last_context_budget if current else None,
            "usage": self.last_usage if current else None,
        }

    async def evaluate(self, *, state, questions, timeout=None):
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
        self._last_identity = identity
        self.last_usage = None
        invoked = False
        started = time.perf_counter()
        try:
            timeout = float(timeout if timeout is not None else getattr(provider, 'timeout', 30))
            if not math.isfinite(timeout) or timeout <= 0:
                raise SystemOneError('Invalid decision timeout.', code='context_count_timeout')
            from ..jev_decision import MAX_TOTAL_REQUEST_CHARS, request_size
            # The decision builder reserves 256 model characters. A provider
            # may use a longer ID, so check its actual name before any HTTP IO.
            model = str(provider.get_model() or "")
            size = request_size(state, questions, model=model)
            if size > MAX_TOTAL_REQUEST_CHARS:
                raise SystemOneError("System One 请求超出决策预算。", code="request_budget_exceeded")
            counter = getattr(provider, 'systemone_count_tokens', None)
            self.last_context_budget = None
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
                    self.last_context_budget = {'counts':counts, 'limits':limits, 'shrink_passes':attempt,
                                                'state_characters':len(json.dumps(state,ensure_ascii=False))}
                    character = state.get('character_state', {}) if isinstance(state,dict) else {}
                    self.last_context_budget['character_state_summary'] = {
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
            invoked = True
            evaluator = getattr(provider, 'systemone_evaluate', None)
            if callable(evaluator):
                answers = await evaluator(state=state, questions=questions, timeout=remaining)
            else:
                answers = await asyncio.wait_for(ask_native(provider,state=state,questions=questions),
                                                 timeout=remaining)
        except asyncio.CancelledError:
            raise
        except SystemOneError as exc:
            self.failures += 1
            self._status, self._detail = "degraded", exc.code
            return None
        except TimeoutError:
            self.failures += 1
            self._status, self._detail = 'degraded', 'timeout'
            return None
        except Exception:
            self.failures += 1
            self._status, self._detail = "degraded", "systemone_provider_failed"
            return None
        finally:
            if invoked:
                self.last_usage = getattr(provider, "last_decision_usage", None)
            self.last_latency_ms = round((time.perf_counter() - started) * 1000)
        # A UI model/provider switch must not deliver an answer from the old selection.
        if self._closed or self._resolve()[2] != identity:
            return None
        self._status, self._detail = "available", "ready"
        return answers

    async def close(self):
        self._closed = True
        await self.fallback.close()

    def detach(self):
        self._closed = True
        self._generation += 1
