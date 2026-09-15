"""Per-context concurrency admission, not provider RPM or token accounting."""
from __future__ import annotations

import asyncio
import time
import weakref
from collections import deque
from contextlib import asynccontextmanager


class ProviderBudget:
    def __init__(self, capacity=4, reply_reserve=1, aging_seconds=5.0, max_queue=128):
        self.capacity = max(2, int(capacity))
        self.background_capacity = max(1, self.capacity - max(1, int(reply_reserve)))
        self.aging_seconds = max(0.001, float(aging_seconds))
        self.max_queue = max(1, int(max_queue))
        self.active = {}
        self.waiters = []
        self.samples = {}
        self.counts = {}

    def observe(self, purpose, stage, elapsed):
        key = (purpose, stage)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.samples.setdefault(key, deque(maxlen=512)).append(elapsed)

    def diagnostics(self):
        result = {}
        for (purpose, stage), samples in self.samples.items():
            values = sorted(samples)
            result.setdefault(purpose, {})[stage] = {
                "calls": self.counts[(purpose, stage)], "samples": len(values),
                "p50_seconds": values[int((len(values)-1)*.50)],
                "p95_seconds": values[int((len(values)-1)*.95)],
            }
        return {"capacity": self.capacity, "background_capacity": self.background_capacity,
                "queued": len(self.waiters), "active": sum(v[0] for v in self.active.values()),
                "stages": result}

    def _dispatch(self):
        now = time.monotonic()
        def rank(item):
            _, purpose, started, _ = item
            base = 0 if purpose in ("reply", "routing") else 1 if purpose == "draft" else 2
            return (base - int((now-started)/self.aging_seconds), started)
        for item in sorted(self.waiters, key=rank):
            provider, purpose, _, future = item
            if future.done():
                self.waiters.remove(item)
                continue
            total, background = self.active.get(provider, (0, 0))
            low = purpose not in ("reply", "routing")
            if total >= self.capacity or (low and background >= self.background_capacity):
                continue
            self.waiters.remove(item)
            self.active[provider] = (total+1, background+int(low))
            future.set_result(True)

    @asynccontextmanager
    async def slot(self, provider, purpose):
        if len(self.waiters) >= self.max_queue:
            raise RuntimeError("Provider admission queue is full")
        started = time.monotonic()
        future = asyncio.get_running_loop().create_future()
        item = (provider, purpose, started, future)
        self.waiters.append(item)
        self._dispatch()
        queued = None
        try:
            await future
            queued = time.monotonic()-started
            yield
        finally:
            self.observe(purpose, "queue", time.monotonic()-started if queued is None else queued)
            if item in self.waiters:
                self.waiters.remove(item)
            elif future.done() and not future.cancelled() and future.result() is True:
                total, background = self.active[provider]
                updated = (total-1, background-int(purpose not in ("reply", "routing")))
                if updated[0]:
                    self.active[provider] = updated
                else:
                    self.active.pop(provider)
            self._dispatch()

    async def run(self, provider, purpose, invoke):
        async with self.slot(provider, purpose):
            started = time.monotonic()
            try:
                value = invoke()
                import inspect
                return await value if inspect.isawaitable(value) else value
            finally:
                self.observe(purpose, "generation", time.monotonic()-started)


_SLOT_CONTEXTS = {}


def context_budget(context):
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
                reference = weakref.ref(context, lambda ref: _SLOT_CONTEXTS.pop(key, None))
                _SLOT_CONTEXTS[key] = (reference, budget)
            except TypeError:
                # Non-weakref legacy shims may explicitly inject a shared budget.
                pass
    return budget
