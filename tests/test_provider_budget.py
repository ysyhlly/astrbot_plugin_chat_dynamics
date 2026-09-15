import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.provider_budget import ProviderBudget
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter, LLMUnavailable


@pytest.mark.asyncio
async def test_background_reserve_and_cancellation():
    budget = ProviderBudget(capacity=2)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def slow():
        entered.set()
        await release.wait()
    background = asyncio.create_task(budget.run("p", "title", slow))
    await entered.wait()
    queued = asyncio.create_task(budget.run("p", "vibe", slow))
    await asyncio.sleep(0)
    assert await budget.run("p", "reply", lambda: "reply") == "reply"
    assert len(budget.waiters) == 1
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    release.set()
    await background
    assert budget.diagnostics()["active"] == 0
    assert budget.diagnostics()["queued"] == 0


@pytest.mark.asyncio
async def test_shared_adapters_queue_timeout_and_provider_selection():
    release = asyncio.Event()
    async def generate(**kwargs):
        await release.wait()
        return "ok"
    ctx = SimpleNamespace(llm_generate=generate)
    first = LLMAdapter(ctx, "p", vibe_provider_id="v", draft_provider_id="d")
    second = LLMAdapter(ctx, "p")
    assert first.provider_budget is second.provider_budget
    assert first.configured_provider("routing") == "p"
    assert first.configured_provider("title") == "p"
    assert first.configured_provider("auto_draft") == "d"
    tasks = [asyncio.create_task(first.generate(prompt="x", umo="u", system_prompt="", purpose="title")) for _ in range(3)]
    await asyncio.sleep(.01)
    with pytest.raises(LLMUnavailable, match="timed out"):
        await second.generate(prompt="x", umo="u", system_prompt="", purpose="title", timeout=.02)
    assert first.provider_budget.diagnostics()["queued"] == 0
    release.set()
    await asyncio.gather(*tasks)
    stats = first.provider_budget.diagnostics()["stages"]["title"]
    assert stats["generation"]["calls"] == 3
    assert stats["queue"]["calls"] == 4
    assert stats["lookup"]["calls"] == 4


@pytest.mark.asyncio
async def test_aging_prevents_background_starvation(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import provider_budget
    now = [0.0]
    monkeypatch.setattr(provider_budget.time, "monotonic", lambda: now[0])
    budget = ProviderBudget(capacity=2, aging_seconds=1)
    budget.active["p"] = (2, 0)
    order = []
    old = asyncio.create_task(budget.run("p", "title", lambda: order.append("title")))
    await asyncio.sleep(0)
    now[0] = 10
    reply = asyncio.create_task(budget.run("p", "reply", lambda: order.append("reply")))
    await asyncio.sleep(0)
    budget.active["p"] = (1, 0)
    budget._dispatch()
    await asyncio.gather(old, reply)
    assert order == ["title", "reply"]


@pytest.mark.asyncio
async def test_fresh_reply_precedes_background_in_shared_queue():
    budget = ProviderBudget(capacity=2)
    budget.active["p"] = (2, 0)
    order = []
    tasks = [asyncio.create_task(budget.run("p", purpose, lambda p=purpose: order.append(p)))
             for purpose in ("title", "draft", "reply", "routing")]
    await asyncio.sleep(0)
    budget.active["p"] = (1, 0)
    budget._dispatch()
    await asyncio.gather(*tasks)
    assert order == ["reply", "routing", "draft", "title"]


@pytest.mark.asyncio
async def test_controlled_slow_background_reply_latency_comparison():
    import time
    async def measure(reserved):
        budget = ProviderBudget(capacity=2)
        semaphore = asyncio.Semaphore(2)
        async def run(purpose, operation):
            if reserved:
                return await budget.run("p", purpose, operation)
            async with semaphore:
                return await operation()
        tasks = [asyncio.create_task(run("title", lambda: asyncio.sleep(.06))) for _ in range(4)]
        await asyncio.sleep(.01)
        start = time.perf_counter()
        await run("reply", lambda: asyncio.sleep(0))
        elapsed = time.perf_counter()-start
        await asyncio.gather(*tasks)
        return elapsed
    fifo = await measure(False)
    reserved = await measure(True)
    print(f"controlled reply queue latency: FIFO={fifo:.4f}s reserve={reserved:.4f}s")
    assert fifo > .07
    assert reserved < fifo / 2


def test_slots_context_registry_does_not_retain_host():
    import gc
    import weakref
    from astrbot_plugin_chat_dynamics.core.provider_budget import context_budget, _SLOT_CONTEXTS
    class Host:
        __slots__ = ("__weakref__",)
    host = Host()
    key = id(host)
    ref = weakref.ref(host)
    assert context_budget(host) is context_budget(host)
    del host
    gc.collect()
    assert ref() is None
    assert key not in _SLOT_CONTEXTS


@pytest.mark.asyncio
async def test_native_and_completion_share_provider_capacity(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request
    async def media(*args):
        return [], []
    async def request(*args):
        return None
    monkeypatch.setattr(llm_adapter, "collect_media_urls", media)
    monkeypatch.setattr(native_request, "prepare_request", request)
    release = asyncio.Event()
    entered = asyncio.Event()
    async def native(**kwargs):
        entered.set()
        await release.wait()
        return "native"
    ctx = SimpleNamespace(tool_loop_agent=native, llm_generate=lambda **kwargs: "completion")
    budget = ProviderBudget(capacity=2)
    adapter = LLMAdapter(ctx, "p", provider_budget=budget)
    tasks = [asyncio.create_task(adapter.run_native_agent(SimpleNamespace(), "hi", umo="u")) for _ in range(2)]
    await entered.wait()
    await asyncio.sleep(0)
    with pytest.raises(LLMUnavailable):
        await adapter.generate(prompt="hi", umo="u", system_prompt="", timeout=.01)
    release.set()
    assert await asyncio.gather(*tasks) == ["native", "native"]
    assert budget.diagnostics()["active"] == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_dispatch_before_cancellation_cleanup():
    budget = ProviderBudget(capacity=2)
    first = budget.slot("p", "reply")
    second = budget.slot("p", "reply")
    await first.__aenter__()
    await second.__aenter__()
    queued = asyncio.create_task(budget.run("p", "reply", lambda: "unexpected"))
    await asyncio.sleep(0)
    queued.cancel()
    # Release synchronously: dispatch sees the cancelled future before the
    # queued task gets its next event-loop turn to run its own finally block.
    await first.__aexit__(None, None, None)
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert budget.active == {"p": (1, 0)}
    await second.__aexit__(None, None, None)
    assert budget.active == {}
    assert await budget.run("p", "reply", lambda: "ok") == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_legacy_lookup_has_separate_samples(configured, monkeypatch):
    from astrbot_plugin_chat_dynamics.core import llm_adapter
    clock = [10.0]
    monkeypatch.setattr(llm_adapter, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    provider = SimpleNamespace(text_chat=lambda **kwargs: "ok")
    async def lookup(*args):
        await asyncio.sleep(0)
        clock[0] += .125
        return provider
    ctx = SimpleNamespace(get_using_provider=lookup, get_provider_by_id=lookup)
    adapter = LLMAdapter(ctx, "p" if configured else "")
    assert await adapter.generate(prompt="hi", umo="u", system_prompt="", purpose="draft") == "ok"
    stages = adapter.provider_budget.diagnostics()["stages"]["draft"]
    assert stages["lookup"]["calls"] == 1
    assert stages["lookup"]["p50_seconds"] == .125
    assert stages["generation"]["calls"] == 1
