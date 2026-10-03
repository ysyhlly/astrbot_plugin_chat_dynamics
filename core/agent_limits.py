"""Per-runner model budgets and protection of the active request during compaction."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class ModelCallBudget:
    def __init__(self, seconds):
        self.remaining = float(seconds)
        self.depth = 0
        self.started = 0.0

    @asynccontextmanager
    async def scope(self):
        loop = asyncio.get_running_loop()
        if self.depth == 0:
            self.started = loop.time()
        remaining = self.remaining - (loop.time() - self.started)
        self.depth += 1
        try:
            async with asyncio.timeout(max(0, remaining)):
                yield
        finally:
            self.depth -= 1
            if self.depth == 0:
                self.remaining -= loop.time() - self.started


class BudgetProvider:
    """Delegate to a provider without mutating a shared host provider instance."""
    def __init__(self, provider, budget):
        self.provider, self.budget = provider, budget

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def text_chat(self, *args, **kwargs):
        async with self.budget.scope():
            return await self.provider.text_chat(*args, **kwargs)

    async def text_chat_stream(self, *args, **kwargs):
        stream = self.provider.text_chat_stream(*args, **kwargs)
        try:
            async with self.budget.scope():
                async for response in stream:
                    yield response
        finally:
            await stream.aclose()


class ProtectedContextManager:
    def __init__(self, delegate, request, budget=None):
        self.delegate, self.request, self.budget = delegate, request, budget

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def process(self, messages, **kwargs):
        from .agent_bridge import AgentBridgeUnavailable
        boundary = next((i for i, message in enumerate(messages) if message is self.request), None)
        if boundary is None:
            raise AgentBridgeUnavailable('request_history_boundary_lost')
        active_round = list(messages[boundary:])
        if self.budget is None:
            processed = await self.delegate.process(messages, **kwargs)
        else:
            async with self.budget.scope():
                processed = await self.delegate.process(messages, **kwargs)
        boundary = next((i for i, message in enumerate(processed) if message is self.request), None)
        if boundary is None:
            # SDK truncation may retain an old user instead of the current one.
            # Keep the exact active round; never substitute another user's request.
            systems = [message for message in processed if message.role == 'system']
            return systems + active_round
        return list(processed[:boundary]) + active_round
