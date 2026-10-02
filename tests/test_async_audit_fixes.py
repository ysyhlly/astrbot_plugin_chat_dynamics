"""Reload/caller cancellation races and optional-context failure boundaries."""

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request
from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
from astrbot_plugin_chat_dynamics.core.integrations.selflearning import SelfLearningHubClient
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_embedding_reload_is_fallback_but_concurrent_caller_cancel_propagates(cancel_caller):
    started = asyncio.Event()

    class Provider:
        async def get_embedding(self, text):
            started.set()
            await asyncio.Event().wait()

    adapter = EmbeddingAdapter(SimpleNamespace(get_all_embedding_providers=lambda: [Provider()]), enabled=True)
    first = asyncio.create_task(adapter.embed("shared"))
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(adapter.embed("shared"))
    await asyncio.sleep(0)
    adapter.configure(enabled=False)
    if cancel_caller:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        assert await first is None
    assert await second is None








@pytest.mark.asyncio
async def test_reply_cancel_waits_for_optional_context_cleanup(monkeypatch):
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def media(*args):
        return [], []

    async def hooks(*args):
        return None

    async def enrich(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    monkeypatch.setattr(llm_adapter, "collect_media_urls", media)
    monkeypatch.setattr(native_request, "prepare_request", hooks)
    adapter = LLMAdapter(SimpleNamespace(), integrations=SimpleNamespace(context_for_request=enrich))
    caller = asyncio.create_task(adapter._run_native_agent(SimpleNamespace(), "hello", umo="group"))
    await asyncio.wait_for(started.wait(), 1)
    caller.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    assert not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_hub_raw_transport_failure_is_optional(monkeypatch):
    client = SelfLearningHubClient("http://localhost")
    client._status = "available"

    async def broken_request(*args, **kwargs):
        raise OSError("transport failed")

    monkeypatch.setattr(client, "_request", broken_request)
    assert await client.context(group_id="group", user_id="user", query="hello") == {}
    assert client._detail == "transport_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("transport"), RuntimeError("integration")])
async def test_optional_context_error_still_generates_reply(monkeypatch, error):
    async def media(*args):
        return [], []

    async def hooks(*args):
        return None

    async def enrich(**kwargs):
        raise error

    async def reply(**kwargs):
        return "reply delivered"

    monkeypatch.setattr(llm_adapter, "collect_media_urls", media)
    monkeypatch.setattr(native_request, "prepare_request", hooks)
    context = SimpleNamespace(tool_loop_agent=reply)
    adapter = LLMAdapter(context, configured_provider_id="provider",
                         integrations=SimpleNamespace(context_for_request=enrich))
    assert await adapter.run_native_agent(SimpleNamespace(), "hello", umo="group") == "reply delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_session_getter_internal_typeerror_is_not_retried(legacy):
    calls = []

    def getter(umo=None):
        calls.append(umo)
        if umo is not None:
            raise TypeError("host getter failed internally")
        return "wrong global provider"

    context = SimpleNamespace(**{("get_using_provider" if legacy else "get_current_chat_provider_id"): getter})
    adapter = LLMAdapter(context)
    with pytest.raises(TypeError, match="failed internally"):
        if legacy:
            await adapter.generate(prompt="hello", umo="session", system_prompt="")
        else:
            await adapter.resolve_provider_id("session")
    assert calls == ["session"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_zero_argument_host_getters_remain_supported(legacy):
    async def reply(**kwargs):
        return "legacy reply"

    context = (SimpleNamespace(get_using_provider=lambda: SimpleNamespace(text_chat=reply))
               if legacy else SimpleNamespace(get_current_chat_provider_id=lambda: "provider"))
    adapter = LLMAdapter(context)
    if legacy:
        assert await adapter.generate(prompt="hello", umo="session", system_prompt="") == "legacy reply"
    else:
        assert await adapter.resolve_provider_id("session") == "provider"
