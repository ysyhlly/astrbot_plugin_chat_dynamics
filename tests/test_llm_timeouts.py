"""Total request deadlines cancel stalled stages without poisoning later calls."""

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter, LLMUnavailable
from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["modern", "legacy", "lookup", "legacy_lookup", "native", "media", "hooks"])
async def test_timeout_cancels_each_stage_and_next_call_succeeds(monkeypatch, stage):
    cancelled = asyncio.Event()
    stalled = True

    async def result(*args, **kwargs):
        if stalled:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return "ok"

    async def good(*args, **kwargs):
        return "ok"

    async def media(*args):
        if stage == "media":
            await result()
        return [], []

    async def hooks(*args):
        if stage == "hooks":
            await result()
        return None

    monkeypatch.setattr(llm_adapter, "collect_media_urls", media)
    monkeypatch.setattr(native_request, "prepare_request", hooks)
    provider = SimpleNamespace(text_chat=result if stage == "legacy" else good)

    async def legacy_lookup(*args):
        if stage == "legacy_lookup":
            await result()
        return provider

    ctx = SimpleNamespace(
        llm_generate=result if stage == "modern" else good,
        get_current_chat_provider_id=result if stage == "lookup" else good,
        tool_loop_agent=result if stage == "native" else good,
    )
    if stage in {"legacy", "legacy_lookup"}:
        ctx = SimpleNamespace(get_using_provider=legacy_lookup)
    adapter = LLMAdapter(ctx, reply_timeout=0.01, tool_agent_timeout=0.01)

    async def invoke():
        if stage in {"native", "media", "hooks"}:
            return await adapter.run_native_agent(SimpleNamespace(), "hi", umo="group")
        return await adapter.generate(prompt="hi", umo="group", system_prompt="")

    with pytest.raises(LLMUnavailable, match="timed out"):
        await invoke()
    assert cancelled.is_set()
    stalled = False
    assert await invoke() == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_external_cancellation_propagates(monkeypatch, native):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def media(*args):
        return [], []

    async def hooks(*args):
        return None

    monkeypatch.setattr(llm_adapter, "collect_media_urls", media)
    monkeypatch.setattr(native_request, "prepare_request", hooks)
    adapter = LLMAdapter(SimpleNamespace(llm_generate=hang, tool_loop_agent=hang), "provider")
    coroutine = (adapter.run_native_agent(SimpleNamespace(), "hi") if native else
                 adapter.generate(prompt="hi", umo="group", system_prompt=""))
    task = asyncio.create_task(coroutine)
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.parametrize("field,default,maximum", [("reply_timeout", 60, 300), ("tool_agent_timeout", 120, 600)])
def test_timeout_configuration_bounds(field, default, maximum):
    for invalid in (0, 4.9, maximum + 1, float("inf"), float("nan")):
        config, warnings = parse_runtime_config({field: invalid})
        assert getattr(config, field) == default
        assert warnings
    for valid in (5, maximum):
        config, warnings = parse_runtime_config({field: valid, "decision_timeout": 1})
        assert getattr(config, field) == valid
        assert config.decision_timeout == 1
        assert not warnings


def test_configure_updates_independent_deadlines():
    adapter = LLMAdapter(None)
    assert (adapter.reply_timeout, adapter.tool_agent_timeout) == (60, 120)
    adapter.configure("", reply_timeout=30, tool_agent_timeout=240)
    assert (adapter.reply_timeout, adapter.tool_agent_timeout) == (30, 240)
