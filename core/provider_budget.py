"""Per-context concurrency admission and shared series telemetry hook."""
from __future__ import annotations

import asyncio
import inspect
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

DEFAULT_PLUGIN_ID = "astrbot_plugin_chat_dynamics"
_PRIORITY_PURPOSES = frozenset({"reply", "routing"})


class ProviderBudgetBusyError(RuntimeError):
    """Raised when the provider admission queue is full or acquire_timeout elapses."""

    is_provider_budget_busy = True

    def __init__(
        self,
        message: str = "Provider admission queue is busy",
        *,
        provider: str = "",
        purpose: str = "",
        reason: str = "busy",
    ) -> None:
        super().__init__(message)
        self.provider = str(provider or "")
        self.purpose = str(purpose or "")
        self.reason = str(reason or "busy")


def _is_error_llm_response(response: Any) -> bool:
    if response is None:
        return False
    return str(getattr(response, "role", "") or "").strip().lower() == "err"


class ProviderBudget:
    supports_series_telemetry = True

    def __init__(
        self,
        capacity: int = 4,
        reply_reserve: int = 1,
        aging_seconds: float = 5.0,
        max_queue: int = 128,
        *,
        telemetry: Any = None,
    ) -> None:
        self.capacity = max(2, int(capacity))
        self.background_capacity = max(1, self.capacity - max(1, int(reply_reserve)))
        self.aging_seconds = max(0.001, float(aging_seconds))
        self.max_queue = max(1, int(max_queue))
        self.active: dict[str, tuple[int, int]] = {}
        self._plugin_active: dict[str, int] = {}
        self.waiters: list[tuple[str, str, str, float, asyncio.Future[bool]]] = []
        self.samples: dict[tuple[str, str], deque[float]] = {}
        self.counts: dict[tuple[str, str], int] = {}
        self.telemetry: Any = telemetry

    def attach_telemetry(self, telemetry: Any) -> None:
        self.telemetry = telemetry

    def _record_telemetry(
        self,
        *,
        plugin_id: str,
        provider_id: str,
        purpose: str,
        response: Any = None,
        queue_seconds: float = 0.0,
        generation_seconds: float = 0.0,
        ok: bool = True,
        error_type: str = "",
    ) -> None:
        telemetry = self.telemetry
        recorder = getattr(telemetry, "record", None) if telemetry is not None else None
        if not callable(recorder):
            return
        try:
            recorder(
                plugin_id=plugin_id,
                provider_id=provider_id,
                purpose=purpose,
                response=response,
                queue_seconds=queue_seconds,
                generation_seconds=generation_seconds,
                ok=ok,
                error_type=error_type,
            )
        except Exception:
            pass

    def observe(self, purpose: str, stage: str, elapsed: float) -> None:
        key = (purpose, stage)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.samples.setdefault(key, deque(maxlen=512)).append(elapsed)

    def diagnostics(self) -> dict[str, Any]:
        result: dict[str, dict[str, Any]] = {}
        for (purpose, stage), samples in self.samples.items():
            values = sorted(samples)
            result.setdefault(purpose, {})[stage] = {
                "calls": self.counts[(purpose, stage)],
                "samples": len(values),
                "p50_seconds": values[int((len(values) - 1) * 0.50)],
                "p95_seconds": values[int((len(values) - 1) * 0.95)],
            }
        return {
            "capacity": self.capacity,
            "background_capacity": self.background_capacity,
            "max_queue": self.max_queue,
            "queued": len(self.waiters),
            "active": sum(v[0] for v in self.active.values()),
            "stages": result,
        }

    def live_snapshot(self) -> dict[str, Any]:
        queued_by_provider: dict[str, int] = {}
        queued_by_plugin: dict[str, int] = {}
        for provider, _purpose, plugin_id, _started, future in self.waiters:
            if future.done():
                continue
            queued_by_provider[provider] = queued_by_provider.get(provider, 0) + 1
            queued_by_plugin[plugin_id] = queued_by_plugin.get(plugin_id, 0) + 1

        by_provider: dict[str, dict[str, int]] = {}
        for prov in sorted(set(self.active) | set(queued_by_provider)):
            total, bg = self.active.get(prov, (0, 0))
            by_provider[prov] = {
                "active": int(total),
                "background_active": int(bg),
                "queued": int(queued_by_provider.get(prov, 0)),
            }

        by_plugin: dict[str, dict[str, int]] = {}
        for pid in sorted(set(self._plugin_active) | set(queued_by_plugin)):
            act = int(self._plugin_active.get(pid, 0))
            q = int(queued_by_plugin.get(pid, 0))
            if act or q:
                by_plugin[pid] = {"active": act, "queued": q}

        return {
            "capacity": self.capacity,
            "background_capacity": self.background_capacity,
            "max_queue": self.max_queue,
            "active": sum(v[0] for v in self.active.values()),
            "queued": sum(queued_by_provider.values()),
            "by_provider": by_provider,
            "by_plugin": by_plugin,
        }

    def _dispatch(self) -> None:
        now = time.monotonic()

        def rank(item: tuple[str, str, str, float, asyncio.Future[bool]]) -> tuple[int, float]:
            _, purpose, _, started, _ = item
            base = 0 if purpose in _PRIORITY_PURPOSES else 2
            return (base - int((now - started) / self.aging_seconds), started)

        for item in sorted(self.waiters, key=rank):
            provider, purpose, plugin_id, _, future = item
            if future.done():
                if item in self.waiters:
                    self.waiters.remove(item)
                continue
            total, background = self.active.get(provider, (0, 0))
            low = purpose not in _PRIORITY_PURPOSES
            if total >= self.capacity or (low and background >= self.background_capacity):
                continue
            if item in self.waiters:
                self.waiters.remove(item)
            self.active[provider] = (total + 1, background + int(low))
            self._plugin_active[plugin_id] = self._plugin_active.get(plugin_id, 0) + 1
            future.set_result(True)

    @asynccontextmanager
    async def slot(
        self,
        provider: str,
        purpose: str,
        *,
        plugin_id: str = DEFAULT_PLUGIN_ID,
        acquire_timeout: float | None = None,
    ):
        clean_provider = str(provider or "").strip() or "default"
        clean_purpose = str(purpose or "").strip() or "background"
        clean_plugin = str(plugin_id or "").strip() or DEFAULT_PLUGIN_ID
        if len(self.waiters) >= self.max_queue:
            raise ProviderBudgetBusyError(
                "Provider admission queue is full",
                provider=clean_provider,
                purpose=clean_purpose,
                reason="queue_full",
            )
        started = time.monotonic()
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        item = (clean_provider, clean_purpose, clean_plugin, started, future)
        self.waiters.append(item)
        self._dispatch()
        queued: float | None = None
        try:
            if acquire_timeout is not None:
                timeout_val = max(0.0, float(acquire_timeout))
                try:
                    await asyncio.wait_for(future, timeout=timeout_val)
                except asyncio.TimeoutError as exc:
                    raise ProviderBudgetBusyError(
                        f"Provider admission timed out after {timeout_val:g}s",
                        provider=clean_provider,
                        purpose=clean_purpose,
                        reason="acquire_timeout",
                    ) from exc
            else:
                await future
            queued = max(0.0, time.monotonic() - started)
            yield queued
        finally:
            self.observe(
                clean_purpose,
                "queue",
                max(0.0, time.monotonic() - started) if queued is None else queued,
            )
            if item in self.waiters:
                self.waiters.remove(item)
            elif future.done() and not future.cancelled() and future.result() is True:
                if clean_provider in self.active:
                    total, background = self.active[clean_provider]
                    low = int(clean_purpose not in _PRIORITY_PURPOSES)
                    updated = (max(0, total - 1), max(0, background - low))
                    if updated[0]:
                        self.active[clean_provider] = updated
                    else:
                        self.active.pop(clean_provider, None)
                rem = self._plugin_active.get(clean_plugin, 0) - 1
                if rem > 0:
                    self._plugin_active[clean_plugin] = rem
                else:
                    self._plugin_active.pop(clean_plugin, None)
            self._dispatch()

    async def run(
        self,
        provider: str,
        purpose: str,
        invoke,
        *,
        plugin_id: str = DEFAULT_PLUGIN_ID,
        acquire_timeout: float | None = None,
    ):
        clean_provider = str(provider or "").strip() or "default"
        clean_purpose = str(purpose or "").strip() or "background"
        clean_plugin = str(plugin_id or "").strip() or DEFAULT_PLUGIN_ID
        queue_started = time.monotonic()
        try:
            async with self.slot(
                clean_provider,
                clean_purpose,
                plugin_id=clean_plugin,
                acquire_timeout=acquire_timeout,
            ) as queue_seconds:
                started = time.monotonic()
                try:
                    value = invoke()
                    result = await value if inspect.isawaitable(value) else value
                except BaseException as exc:
                    gen_elapsed = max(0.0, time.monotonic() - started)
                    self.observe(clean_purpose, "generation", gen_elapsed)
                    self._record_telemetry(
                        plugin_id=clean_plugin,
                        provider_id=clean_provider,
                        purpose=clean_purpose,
                        queue_seconds=queue_seconds,
                        generation_seconds=gen_elapsed,
                        ok=False,
                        error_type=type(exc).__name__,
                    )
                    raise
                gen_elapsed = max(0.0, time.monotonic() - started)
                self.observe(clean_purpose, "generation", gen_elapsed)
                is_err = _is_error_llm_response(result)
                self._record_telemetry(
                    plugin_id=clean_plugin,
                    provider_id=clean_provider,
                    purpose=clean_purpose,
                    response=result,
                    queue_seconds=queue_seconds,
                    generation_seconds=gen_elapsed,
                    ok=not is_err,
                    error_type="host_error_response" if is_err else "",
                )
                return result
        except ProviderBudgetBusyError as exc:
            self._record_telemetry(
                plugin_id=clean_plugin,
                provider_id=clean_provider,
                purpose=clean_purpose,
                queue_seconds=max(0.0, time.monotonic() - queue_started),
                generation_seconds=0.0,
                ok=False,
                error_type=f"budget_{exc.reason}",
            )
            raise


_SLOT_CONTEXTS: dict[int, tuple[weakref.ref[Any], ProviderBudget]] = {}


def context_budget(context: Any) -> ProviderBudget:
    """Context owns the scheduler: no process registry retaining retired hosts."""
    budget = getattr(context, "_chat_dynamics_provider_budget", None)
    existing = _SLOT_CONTEXTS.get(id(context))
    if existing is not None and existing[0]() is context:
        return existing[1]
    if not isinstance(budget, ProviderBudget):
        budget = ProviderBudget()
        try:
            setattr(context, "_chat_dynamics_provider_budget", budget)
        except (AttributeError, TypeError):
            # Weak references support slots-only hosts without retaining them.
            key = id(context)
            try:
                reference = weakref.ref(context, lambda _ref: _SLOT_CONTEXTS.pop(key, None))
                _SLOT_CONTEXTS[key] = (reference, budget)
            except TypeError:
                # Non-weakref legacy shims may explicitly inject a shared budget.
                pass
    telemetry = getattr(context, "_chat_dynamics_series_telemetry", None)
    if telemetry is not None and budget.telemetry is None:
        budget.attach_telemetry(telemetry)
    return budget


def attach_context_telemetry(context: Any, telemetry: Any) -> ProviderBudget:
    """Attach a shared SeriesTelemetryStore to both the context and its ProviderBudget."""
    if context is not None:
        try:
            setattr(context, "_chat_dynamics_series_telemetry", telemetry)
        except (AttributeError, TypeError):
            pass
    budget = context_budget(context)
    budget.attach_telemetry(telemetry)
    return budget
