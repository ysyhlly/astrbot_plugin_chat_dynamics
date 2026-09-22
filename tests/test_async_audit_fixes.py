"""Reload/caller cancellation races and optional-context failure boundaries."""

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request
from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
from astrbot_plugin_chat_dynamics.core.integrations import typesafe
from astrbot_plugin_chat_dynamics.core.integrations.laya import LayaClient
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
@pytest.mark.parametrize("client_type, operation", [
    (typesafe.SystemOneClient, "evaluate"), (LayaClient, "evaluate"), (LayaClient, "request_json")])
@pytest.mark.parametrize("action", ["configure", "close"])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_decision_reload_preserves_simultaneous_caller_cancel(
        monkeypatch, client_type, operation, action, cancel_caller):
    started = asyncio.Event()

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            started.set()
            await asyncio.Event().wait()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(typesafe.aiohttp, "ClientSession", Session)
    client = client_type(base_url="http://localhost:8000")
    request = (client.request_json("/health", method="GET") if operation == "request_json" else
               client.evaluate(state="hello", questions={"join": {"type": "noul", "instructions": "Speak?"}}))
    caller = asyncio.create_task(request)
    await asyncio.wait_for(started.wait(), 1)
    if cancel_caller:
        caller.cancel()
    if action == "configure":
        client.configure(enabled=False)
    else:
        await client.close()
    if cancel_caller:
        with pytest.raises(asyncio.CancelledError):
            await caller
    else:
        assert await caller is None


@pytest.mark.asyncio
async def test_laya_service_request_timeout_cleans_up_transport(monkeypatch):
    cancelled = asyncio.Event()

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(typesafe.aiohttp, "ClientSession", Session)
    client = LayaClient(base_url="http://localhost:8000")
    assert await client.request_json("/health", method="GET", timeout=.01) is None
    assert cancelled.is_set()
    assert not client._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type, operation", [
    (typesafe.SystemOneClient, "evaluate"), (LayaClient, "evaluate"), (LayaClient, "request_json")])
async def test_close_waits_for_cancelled_request_transport_cleanup(monkeypatch, client_type, operation):
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(typesafe.aiohttp, "ClientSession", Session)
    client = client_type(base_url="http://localhost:8000")
    request = (client.request_json("/health", method="GET") if operation == "request_json" else
               client.evaluate(state="hello", questions={"join": {"type": "noul", "instructions": "Speak?"}}))
    caller = asyncio.create_task(request)
    await asyncio.wait_for(started.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait_for(cleaning.wait(), 1)
    assert client._tasks
    closing = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 1)
    assert cleaned.is_set()
    assert not client._tasks


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
