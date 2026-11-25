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
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.provider = str(provider or "")
        self.purpose = str(purpose or "")
        self.reason = str(reason or "busy")
        try:
            retry_val = float(retry_after or 0.0)
        except (TypeError, ValueError):
            retry_val = 0.0
        self.retry_after = max(0.0, retry_val) if retry_val == retry_val else 0.0


def _is_error_llm_response(response: Any) -> bool:
    if response is None:
        return False
    return str(getattr(response, "role", "") or "").strip().lower() == "err"


_LOCAL_ERROR_NAMES = frozenset(
    {
        "BoundaryCancelled",
        "PersonaChanged",
        "ProviderBudgetBusyError",
        "ValueError",
        "KeyError",
        "TypeError",
        "AttributeError",
        "LookupError",
    }
)
_LOCAL_REASON_CODES = frozenset(
    {
        "invalid_reply_mode_plan",
        "invalid_reply_narration",
        "invalid_reply_choice",
        "empty_narration",
        "native_memory_source_invalidated",
        "provider_content_blocked",
        "current_actor_unknown",
        "current_actor_blocked",
        "current_actor_policy_changed",
        "boundary_service_unavailable",
        "boundary_snapshot_unavailable",
        "shield_history_boundary_unresolved",
        "reply_learning_invalidated",
        "evidence_revision_changed",
    }
)
_LOCAL_ERROR_MARKERS = (
    "boundarycancelled",
    "current_actor_unknown",
    "current_actor_blocked",
    "current_actor_policy_changed",
    "boundary_service_unavailable",
    "boundary_snapshot_unavailable",
    "shield_history_boundary_unresolved",
    "reply_learning_invalidated",
    "evidence_revision_changed",
    "native_memory_source_invalidated",
    "provider_content_blocked",
    "content_filter",
    "content_blocked",
)


def _is_non_upstream_error(exc: BaseException | None, response: Any = None) -> bool:
    """Return True for local policy/boundary/validation errors that must not trip upstream circuit breakers."""
    if exc is not None:
        if isinstance(exc, asyncio.CancelledError):
            return True
        if getattr(exc, "is_local_policy_error", False) or getattr(exc, "is_provider_budget_busy", False):
            return True
        if type(exc).__name__ in _LOCAL_ERROR_NAMES:
            return True
        reason_code = str(getattr(exc, "reason_code", "") or "").strip().lower()
        if reason_code and (
            reason_code in _LOCAL_REASON_CODES
            or reason_code.startswith("boundary_")
            or reason_code.startswith("current_actor_")
            or reason_code.startswith("invalid_reply_")
        ):
            return True
        msg = str(exc).lower()
        if msg and any(marker in msg for marker in _LOCAL_ERROR_MARKERS):
            return True
    if response is not None and _is_error_llm_response(response):
        txt = str(getattr(response, "completion_text", "") or "").lower()
        if txt and any(marker in txt for marker in _LOCAL_ERROR_MARKERS):
            return True
    return False


def _is_rate_limit_error(exc: BaseException | None, response: Any = None) -> bool:
    if exc is not None:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        if any(tok in name for tok in ("ratelimit", "toomanyrequests", "resourceexhausted")):
            return True
        if any(tok in msg for tok in ("429", "rate limit", "resource_exhausted", "too many requests", "quota")):
            return True
    if response is not None and _is_error_llm_response(response):
        txt = str(getattr(response, "completion_text", "") or "").lower()
        if any(tok in txt for tok in ("429", "rate limit", "resource_exhausted", "too many requests")):
            return True
    return False


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
        daily_token_soft_limit: int = 0,
        daily_token_hard_limit: int = 0,
        daily_cost_soft_usd: float = 0.0,
        daily_cost_hard_usd: float = 0.0,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_base_cooldown: float = 10.0,
        circuit_breaker_max_cooldown: float = 120.0,
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
        self.daily_token_soft_limit = max(0, int(daily_token_soft_limit))
        self.daily_token_hard_limit = max(0, int(daily_token_hard_limit))
        self.daily_cost_soft_usd = max(0.0, float(daily_cost_soft_usd))
        self.daily_cost_hard_usd = max(0.0, float(daily_cost_hard_usd))
        self.circuit_breaker_threshold = max(0, int(circuit_breaker_threshold))
        self.circuit_breaker_base_cooldown = max(1.0, float(circuit_breaker_base_cooldown))
        self.circuit_breaker_max_cooldown = max(
            self.circuit_breaker_base_cooldown, float(circuit_breaker_max_cooldown)
        )
        # provider_id -> (consecutive_failures, open_until_monotonic, last_reason)
        self._circuit: dict[str, tuple[int, float, str]] = {}
        # provider_id currently executing its single half-open probe after cooldown expiry
        self._circuit_probes: set[str] = set()
        self._budget_cache: tuple[float, bool, bool] = (0.0, False, False)
        self._budget_refresh_task: asyncio.Task | None = None
        self._budget_cache_ttl: float = 5.0

    def configure_limits(
        self,
        *,
        daily_token_soft_limit: int | None = None,
        daily_token_hard_limit: int | None = None,
        daily_cost_soft_usd: float | None = None,
        daily_cost_hard_usd: float | None = None,
        circuit_breaker_threshold: int | None = None,
    ) -> None:
        if daily_token_soft_limit is not None:
            self.daily_token_soft_limit = max(0, int(daily_token_soft_limit))
        if daily_token_hard_limit is not None:
            self.daily_token_hard_limit = max(0, int(daily_token_hard_limit))
        if daily_cost_soft_usd is not None:
            self.daily_cost_soft_usd = max(0.0, float(daily_cost_soft_usd))
        if daily_cost_hard_usd is not None:
            self.daily_cost_hard_usd = max(0.0, float(daily_cost_hard_usd))
        if circuit_breaker_threshold is not None:
            self.circuit_breaker_threshold = max(0, int(circuit_breaker_threshold))
        self._budget_cache = (0.0, False, False)

    def _limits_configured(self) -> bool:
        return bool(
            self.daily_token_soft_limit > 0
            or self.daily_token_hard_limit > 0
            or self.daily_cost_soft_usd > 0.0
            or self.daily_cost_hard_usd > 0.0
        )

    def _evaluate_today_usage(self, today: Any) -> tuple[bool, bool]:
        if not isinstance(today, dict):
            return False, False
        tok = int(today.get("total_tokens", 0) or 0)
        cost = float(today.get("estimated_cost_usd", 0.0) or 0.0)
        hard = bool(
            (self.daily_token_hard_limit > 0 and tok >= self.daily_token_hard_limit)
            or (self.daily_cost_hard_usd > 0.0 and cost >= self.daily_cost_hard_usd)
        )
        soft = hard or bool(
            (self.daily_token_soft_limit > 0 and tok >= self.daily_token_soft_limit)
            or (self.daily_cost_soft_usd > 0.0 and cost >= self.daily_cost_soft_usd)
        )
        return soft, hard

    def _schedule_budget_refresh(self) -> None:
        if not self._limits_configured():
            return
        today_fn = getattr(self.telemetry, "today_usage_summary", None)
        if not callable(today_fn):
            return
        if self._budget_refresh_task is not None and not self._budget_refresh_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _refresh() -> tuple[bool, bool]:
            try:
                today = await asyncio.to_thread(today_fn)
                soft, hard = self._evaluate_today_usage(today)
                self._budget_cache = (time.monotonic(), soft, hard)
                return soft, hard
            except Exception:
                return self._budget_cache[1], self._budget_cache[2]
            finally:
                if self._budget_refresh_task is asyncio.current_task():
                    self._budget_refresh_task = None

        self._budget_refresh_task = loop.create_task(_refresh())

    async def _daily_budget_state_async(self) -> tuple[bool, bool]:
        """Return (soft_exceeded, hard_exceeded), refreshing SQLite off the event loop."""
        if not self._limits_configured():
            return False, False
        now = time.monotonic()
        if self._budget_cache[0] > 0.0 and (now - self._budget_cache[0]) < self._budget_cache_ttl:
            return self._budget_cache[1], self._budget_cache[2]
        today_fn = getattr(self.telemetry, "today_usage_summary", None)
        if not callable(today_fn):
            return False, False
        self._schedule_budget_refresh()
        task = self._budget_refresh_task
        if task is not None:
            try:
                return await asyncio.shield(task)
            except Exception:
                return self._budget_cache[1], self._budget_cache[2]
        return self._budget_cache[1], self._budget_cache[2]

    def _daily_budget_state(self) -> tuple[bool, bool]:
        """Non-blocking cached (soft_exceeded, hard_exceeded); refreshes in background off-loop."""
        if not self._limits_configured():
            return False, False
        now = time.monotonic()
        if self._budget_cache[0] > 0.0 and (now - self._budget_cache[0]) < self._budget_cache_ttl:
            return self._budget_cache[1], self._budget_cache[2]
        self._schedule_budget_refresh()
        return self._budget_cache[1], self._budget_cache[2]

    def _circuit_remaining(self, provider: str) -> tuple[float, str]:
        if self.circuit_breaker_threshold <= 0:
            return 0.0, ""
        entry = self._circuit.get(provider)
        if not entry:
            return 0.0, ""
        _fails, until, reason = entry
        rem = until - time.monotonic()
        if rem <= 0.0:
            return 0.0, ""
        return rem, reason

    def _is_half_open(self, provider: str) -> bool:
        if self.circuit_breaker_threshold <= 0:
            return False
        entry = self._circuit.get(provider)
        if not entry:
            return False
        fails, until, _reason = entry
        return fails >= self.circuit_breaker_threshold and until > 0.0 and until <= time.monotonic()

    def _probe_retry_after(self) -> float:
        return min(15.0, max(1.0, self.circuit_breaker_base_cooldown / 2.0))

    def circuit_remaining(self, provider: str) -> float:
        """Read-only check of remaining circuit cooldown (or half-open probe wait) in seconds."""
        clean_provider = str(provider or "").strip()
        if not clean_provider:
            return 0.0
        rem, _reason = self._circuit_remaining(clean_provider)
        if rem > 0.0:
            return rem
        if self._is_half_open(clean_provider) and clean_provider in self._circuit_probes:
            return self._probe_retry_after()
        return 0.0

    def _record_provider_outcome(
        self,
        provider: str,
        *,
        ok: bool,
        exc: BaseException | None = None,
        response: Any = None,
    ) -> None:
        if self.circuit_breaker_threshold <= 0:
            return
        if ok:
            self._circuit.pop(provider, None)
            self._circuit_probes.discard(provider)
            return
        if _is_non_upstream_error(exc, response):
            return
        prev_fails, _prev_until, _prev_reason = self._circuit.get(provider, (0, 0.0, ""))
        rate_limited = _is_rate_limit_error(exc, response)
        # Rate limits trip the breaker faster (weight 2) so background traffic backs off promptly.
        next_fails = prev_fails + (2 if rate_limited else 1)
        open_until = 0.0
        reason = "rate_limit" if rate_limited else (type(exc).__name__ if exc is not None else "host_error")
        if next_fails >= self.circuit_breaker_threshold:
            exponent = min(6, next_fails - self.circuit_breaker_threshold)
            cooldown = min(
                self.circuit_breaker_max_cooldown,
                self.circuit_breaker_base_cooldown * (2 ** exponent),
            )
            open_until = time.monotonic() + cooldown
        self._circuit[provider] = (next_fails, open_until, reason)

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
        model: str = "",
        group_id: str = "",
        image_count: int = 0,
    ) -> None:
        telemetry = self.telemetry
        recorder = getattr(telemetry, "record", None) if telemetry is not None else None
        if not callable(recorder):
            return
        base_kwargs = {
            "plugin_id": plugin_id,
            "provider_id": provider_id,
            "purpose": purpose,
            "response": response,
            "queue_seconds": queue_seconds,
            "generation_seconds": generation_seconds,
            "ok": ok,
            "error_type": error_type,
        }
        try:
            recorder(
                **base_kwargs,
                model=model,
                group_id=group_id,
                image_count=image_count,
            )
        except TypeError:
            try:
                recorder(**base_kwargs)
            except Exception:
                pass
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

        now_mono = time.monotonic()
        breakers = {}
        for prov, (fails, until, reason) in self._circuit.items():
            rem = max(0.0, until - now_mono)
            half_open = bool(rem <= 0.0 and until > 0.0 and fails >= self.circuit_breaker_threshold)
            if rem > 0.0 or half_open or fails >= max(1, self.circuit_breaker_threshold - 1):
                breakers[prov] = {
                    "consecutive_failures": fails,
                    "open": rem > 0.0,
                    "half_open": half_open,
                    "probe_in_flight": prov in self._circuit_probes,
                    "cooldown_remaining_s": round(rem, 1),
                    "reason": reason,
                }
        soft_exceeded, hard_exceeded = self._daily_budget_state()
        return {
            "capacity": self.capacity,
            "background_capacity": self.background_capacity,
            "max_queue": self.max_queue,
            "active": sum(v[0] for v in self.active.values()),
            "queued": sum(queued_by_provider.values()),
            "by_provider": by_provider,
            "by_plugin": by_plugin,
            "circuit_breakers": breakers,
            "budget_guard": {
                "soft_exceeded": soft_exceeded,
                "hard_exceeded": hard_exceeded,
                "daily_token_soft_limit": self.daily_token_soft_limit,
                "daily_token_hard_limit": self.daily_token_hard_limit,
                "daily_cost_soft_usd": self.daily_cost_soft_usd,
                "daily_cost_hard_usd": self.daily_cost_hard_usd,
            },
        }

    def _dispatch(self) -> None:
        now = time.monotonic()
        soft_exceeded, _hard_exceeded = self._daily_budget_state()
        eff_bg_cap = 1 if soft_exceeded else self.background_capacity

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
            if total >= self.capacity or (low and background >= eff_bg_cap):
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
        is_background = clean_purpose not in _PRIORITY_PURPOSES
        owns_half_open_probe = False
        if is_background:
            _soft, hard = await self._daily_budget_state_async()
            if hard:
                raise ProviderBudgetBusyError(
                    "Daily LLM budget hard limit reached for background tasks",
                    provider=clean_provider,
                    purpose=clean_purpose,
                    reason="daily_budget_exceeded",
                    retry_after=300.0,
                )
            rem, reason = self._circuit_remaining(clean_provider)
            if rem > 0.0:
                raise ProviderBudgetBusyError(
                    f"Provider circuit breaker open ({reason}, {rem:.1f}s remaining)",
                    provider=clean_provider,
                    purpose=clean_purpose,
                    reason="circuit_open",
                    retry_after=rem,
                )
            if self._is_half_open(clean_provider):
                if clean_provider in self._circuit_probes:
                    probe_wait = self._probe_retry_after()
                    raise ProviderBudgetBusyError(
                        f"Provider circuit breaker half-open probe in flight ({probe_wait:.1f}s retry)",
                        provider=clean_provider,
                        purpose=clean_purpose,
                        reason="circuit_open",
                        retry_after=probe_wait,
                    )
                self._circuit_probes.add(clean_provider)
                owns_half_open_probe = True
        lane_queued = sum(
            1
            for w_provider, w_purpose, _w_plugin, _w_started, w_future in self.waiters
            if not w_future.done()
            and w_provider == clean_provider
            and (w_purpose not in _PRIORITY_PURPOSES) == is_background
        )
        if lane_queued >= self.max_queue:
            if owns_half_open_probe:
                self._circuit_probes.discard(clean_provider)
            raise ProviderBudgetBusyError(
                "Provider admission queue is full",
                provider=clean_provider,
                purpose=clean_purpose,
                reason="queue_full",
                retry_after=max(1.0, float(self.aging_seconds)),
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
                        retry_after=max(1.0, timeout_val),
                    ) from exc
            else:
                await future
            queued = max(0.0, time.monotonic() - started)
            yield queued
        finally:
            if owns_half_open_probe:
                self._circuit_probes.discard(clean_provider)
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
        model: str = "",
        group_id: str = "",
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
                    if not isinstance(exc, asyncio.CancelledError):
                        self._record_provider_outcome(clean_provider, ok=False, exc=exc)
                    self._record_telemetry(
                        plugin_id=clean_plugin,
                        provider_id=clean_provider,
                        purpose=clean_purpose,
                        queue_seconds=queue_seconds,
                        generation_seconds=gen_elapsed,
                        ok=False,
                        error_type=type(exc).__name__,
                        model=model,
                        group_id=group_id,
                    )
                    raise
                gen_elapsed = max(0.0, time.monotonic() - started)
                self.observe(clean_purpose, "generation", gen_elapsed)
                is_err = _is_error_llm_response(result)
                self._record_provider_outcome(clean_provider, ok=not is_err, response=result)
                self._record_telemetry(
                    plugin_id=clean_plugin,
                    provider_id=clean_provider,
                    purpose=clean_purpose,
                    response=result,
                    queue_seconds=queue_seconds,
                    generation_seconds=gen_elapsed,
                    ok=not is_err,
                    error_type="host_error_response" if is_err else "",
                    model=model,
                    group_id=group_id,
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
                model=model,
                group_id=group_id,
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
