"""Resolve Chat Dynamics' selected decision Provider on every request."""

import asyncio
import time

from .client import SystemOneError


class RoutedSystemOneClient:
    is_systemone_router = True

    def __init__(self, host, fallback):
        self.host = host
        self.fallback = fallback
        self._closed = False
        self._generation = 0
        self._last_identity = None
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
        supported = provider is not None and getattr(
            provider, "is_systemone_provider", False
        )
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
            "model": provider.get_model() if supported else "",
            "calls": self.calls,
            "failures": self.failures,
            "last_latency_ms": self.last_latency_ms if current else 0,
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
            or not getattr(provider, "is_systemone_provider", False)
        ):
            self._status, self._detail = "missing", "invalid_systemone_provider"
            return None
        self.calls += 1
        self._last_identity = identity
        started = time.perf_counter()
        try:
            answers = await provider.systemone_evaluate(
                state=state, questions=questions, timeout=timeout
            )
        except asyncio.CancelledError:
            raise
        except SystemOneError as exc:
            self.failures += 1
            self._status, self._detail = "degraded", exc.code
            return None
        except Exception:
            self.failures += 1
            self._status, self._detail = "degraded", "systemone_provider_failed"
            return None
        finally:
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
