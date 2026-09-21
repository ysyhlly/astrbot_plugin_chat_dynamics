"""LLM adapter chooses exactly one companion enrichment owner per request."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import aiohttp
import pytest

from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request
from astrbot_plugin_chat_dynamics.core.integrations.registry import IntegrationRegistry
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent


def adapter(monkeypatch, integrations, prepared=None):
    monkeypatch.setattr(native_request, "prepare_request", AsyncMock(return_value=prepared))
    monkeypatch.setattr(llm_adapter, "collect_media_urls", AsyncMock(return_value=([], [])))
    loop = AsyncMock(return_value=NS(completion_text="reply"))
    result = llm_adapter.LLMAdapter(NS(tool_loop_agent=loop), integrations=integrations)
    result.resolve_provider_id = AsyncMock(return_value="provider")
    return result, loop


@pytest.mark.asyncio
async def test_prepared_native_request_never_queries_hub(monkeypatch):
    integrations = NS(context_for_request=AsyncMock(side_effect=AssertionError("duplicate Hub query")))
    request = NS(prompt="native prompt", system_prompt="native hints", contexts=[], func_tool=None, image_urls=[])
    client, loop = adapter(monkeypatch, integrations, request)
    assert await client.run_native_agent(MockEvent("hello"), "raw query") == "reply"
    integrations.context_for_request.assert_not_awaited()
    assert loop.call_args.kwargs["prompt"] == "native prompt"
    assert loop.call_args.kwargs["system_prompt"] == "native hints"


@pytest.mark.asyncio
async def test_blank_native_request_uses_the_caller_reply_instruction(monkeypatch):
    request = NS(prompt='{"current_turn":"hello"}', system_prompt="", contexts=[], func_tool=None, image_urls=[])
    client, loop = adapter(monkeypatch, integrations=None, prepared=request)
    assert await client.run_native_agent(
        MockEvent("hello"), "raw query", system_prompt="可见回复就是要发到群里的那句话。",
    ) == "reply"
    assert loop.call_args.kwargs["system_prompt"] == "可见回复就是要发到群里的那句话。"
    assert loop.call_args.kwargs["prompt"] == '{"current_turn":"hello"}'


@pytest.mark.asyncio
async def test_bypassed_native_pipeline_queries_hub_once_with_original_event_query(monkeypatch):
    integrations = NS(context_for_request=AsyncMock(return_value={"social": "context"}))
    client, loop = adapter(monkeypatch, integrations)
    event = MockEvent("hello")
    assert await client.run_native_agent(event, "raw query", vibe_hint="vibe") == "reply"
    integrations.context_for_request.assert_awaited_once_with(event=event, query="raw query", native_hooks=False)
    prompt = loop.call_args.kwargs["prompt"]
    assert prompt.startswith("raw query")
    assert prompt.count('"social": "context"') == 1
    assert "untrusted background data" in prompt
    assert "system_prompt" not in loop.call_args.kwargs


@pytest.mark.parametrize("mode", ["absent", "disabled", "transport_failure"])
@pytest.mark.asyncio
async def test_unavailable_hub_preserves_native_agent_fallback(monkeypatch, mode):
    registry = None if mode == "absent" else IntegrationRegistry()
    if mode == "transport_failure":
        registry.configure(enabled=True, hub_url="http://127.0.0.1:1")
        monkeypatch.setattr(registry.hub, "_request", AsyncMock(side_effect=aiohttp.ClientConnectionError("private error")))
    client, loop = adapter(monkeypatch, registry)
    assert await client.run_native_agent(MockEvent("hello"), "raw query") == "reply"
    assert loop.call_args.kwargs["prompt"] == "raw query"
    if registry:
        if mode == "transport_failure":
            assert registry.hub.snapshot()["error_code"] == "transport_error"
            assert "private error" not in str(registry.snapshot())
        await registry.close()
