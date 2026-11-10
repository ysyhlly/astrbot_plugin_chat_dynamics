import asyncio
from types import SimpleNamespace as NS
import pytest
from .test_persona_agent import host_fixture as host_fixture, PrivateEvent
from astrbot.api.message_components import Plain
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge, PersonaChanged
from astrbot_plugin_chat_dynamics.core.native_memory_bridge import install_native_builder_bridge
from astrbot_plugin_chat_dynamics.core.stream_reply import output_fragments
from astrbot_plugin_chat_dynamics.core.early_reply import final_fragments
from astrbot_plugin_chat_dynamics.core.persona_engine import delivery_fragments
from astrbot_plugin_chat_dynamics.core.private_provider_trace import PrivateProviderTrace
from astrbot_plugin_chat_dynamics.tests.test_jev_decision_layer import jev_plugin as jev_plugin


def private_event(text="你好"):
    event = PrivateEvent()
    event.message_str = text
    event.message_obj.message = [Plain(text)]
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("real_memory_tool", [False, True])
async def test_sentence_precedes_model_finish_and_tail_is_not_replayed(host_fixture, real_memory_tool):
    ctx, _, calls = host_fixture
    event = private_event()
    if real_memory_tool:
        from data.plugins.astrbot_plugin_livingmemory.core.tools.memory_search_tool import MemorySearchTool
        from astrbot.core.provider.func_tool_manager import FunctionToolManager, _PermissionGuardedTool
        from astrbot.core.agent.tool import ToolSet

        raw = MemorySearchTool()
        raw.context, raw.config_manager, raw.memory_engine = ctx, NS(), NS()
        manager = object.__new__(FunctionToolManager)
        manager.func_list = [raw]
        ctx.tools["recall_long_term_memory"] = _PermissionGuardedTool(raw, manager)
        ctx.persona_manager.personas_v3[0]["tools"].append("recall_long_term_memory")
        ctx.get_llm_tool_manager = lambda: NS(
            get_func=lambda name: ctx.tools.get(name), get_full_tool_set=lambda: ToolSet(list(ctx.tools.values()))
        )
    ready, release = asyncio.Event(), asyncio.Event()
    published = []

    async def stream(**kwargs):
        calls.append(kwargs)
        assert not kwargs["func_tool"]
        yield LLMResponse(role="assistant", completion_text="你好呀。", is_chunk=True)
        await release.wait()
        yield LLMResponse(role="assistant", completion_text="接着聊吧。", is_chunk=True)
        yield LLMResponse(role="assistant", completion_text="你好呀。接着聊吧。")

    ctx.provider.text_chat_stream = stream

    async def partial(output):
        published.append(output)
        ready.set()

    bridge = AstrBotAgentBridge(ctx)
    cleanup = install_native_builder_bridge(NS(_shutting_down=False, narrative=None))
    task = asyncio.create_task(
        bridge.generate(
            event,
            (event,),
            "你好",
            await bridge.snapshot(event),
            "provider",
            history_text="你好",
            on_partial_text=partial,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert not task.done() and len(calls) == 1 and not event.sent
        early = published[0]
        early.completion_task = task
        fragments = []
        iterator = output_fragments(early, None, 4, delivery_fragments, final_fragments, fragments)
        assert await anext(iterator) == (0, "你好呀。")
        tail = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        assert not tail.done()
        release.set()
        assert await tail == (1, "接着聊吧。")
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        assert await task is early
        assert await bridge.commit(event, early, "\n\n".join(fragments))
        assert ctx.saved[-1][-1]["content"] == "你好呀。\n\n接着聊吧。"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        cleanup.uninstall()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool_request", "meme_plan"])
async def test_tool_or_meme_turn_has_no_early_stream(host_fixture, monkeypatch, kind):
    ctx, _, calls = host_fixture
    event = private_event("帮我查询天气" if kind == "tool_request" else "你好")

    async def forbidden(**kwargs):
        raise AssertionError("unsafe streaming")

    ctx.provider.text_chat_stream = forbidden
    if kind == "meme_plan":

        async def hook(incoming, _kind, req):
            incoming._stealer_joint_meme_plan_v1 = NS(candidates=[{"id": 1}], resolve=lambda text: (text, []))
            return False

        from astrbot.core.pipeline import context_utils

        monkeypatch.setattr(context_utils, "call_event_hook", hook)
    seen = []

    async def partial(output):
        seen.append(output)

    bridge = AstrBotAgentBridge(ctx)
    cleanup = install_native_builder_bridge(NS(_shutting_down=False, narrative=None))
    try:
        output = await bridge.generate(
            event, (event,), event.message_str, await bridge.snapshot(event), "provider", on_partial_text=partial
        )
        assert output.text and not seen and len(calls) == 1
    finally:
        cleanup.uninstall()


@pytest.mark.asyncio
async def test_failure_after_prefix_keeps_only_delivered_history_without_retry(host_fixture):
    ctx, _, calls = host_fixture
    event = private_event()
    published = []

    async def stream(**kwargs):
        calls.append(kwargs)
        yield LLMResponse(role="assistant", completion_text="你好呀。", is_chunk=True)
        raise RuntimeError("offline stream interrupted")

    ctx.provider.text_chat_stream = stream

    async def partial(output):
        published.append(output)

    bridge = AstrBotAgentBridge(ctx)
    cleanup = install_native_builder_bridge(NS(_shutting_down=False, narrative=None))
    try:
        with pytest.raises(Exception):
            await bridge.generate(
                event,
                (event,),
                "你好",
                await bridge.snapshot(event),
                "provider",
                history_text="你好",
                on_partial_text=partial,
            )
        assert len(calls) == 1 and len(published) == 1
        assert await bridge.commit(event, published[0], published[0].stream_prefix)
        assert "offline stream interrupted" not in str(ctx.saved[-1])
        assert not event.sent
    finally:
        cleanup.uninstall()


@pytest.mark.asyncio
async def test_changed_persona_cannot_publish_the_first_sentence(host_fixture):
    ctx, _, calls = host_fixture
    event = private_event()
    published = []
    valid = True
    bridge = AstrBotAgentBridge(ctx)
    original_current = bridge.current

    async def current(*args):
        return valid and await original_current(*args)

    bridge.current = current

    async def stream(**kwargs):
        calls.append(kwargs)
        yield LLMResponse(role="assistant", completion_text="你好呀。", is_chunk=True)
        yield LLMResponse(role="assistant", completion_text="你好呀。")

    async def stage(name, *args):
        nonlocal valid
        if name == "model_first_content":
            valid = False

    async def partial(output):
        published.append(output)

    ctx.provider.text_chat_stream = stream
    cleanup = install_native_builder_bridge(NS(_shutting_down=False, narrative=None))
    try:
        with pytest.raises(PersonaChanged):
            await bridge.generate(
                event,
                (event,),
                "你好",
                await bridge.snapshot(event),
                "provider",
                on_stage=stage,
                on_partial_text=partial,
            )
        assert not published and not event.sent and not ctx.saved
    finally:
        cleanup.uninstall()


@pytest.mark.asyncio
async def test_normal_sdk_stream_close_is_not_a_provider_error():
    records = []
    closed = asyncio.Event()

    async def stream(**kwargs):
        try:
            yield LLMResponse(role="assistant", completion_text="hi", is_chunk=True)
            yield LLMResponse(role="assistant", completion_text="hi")
        finally:
            closed.set()

    trace = PrivateProviderTrace(
        NS(text_chat_stream=stream),
        NS(provider_call=lambda *args: {}, emit_call=lambda record, result: records.append(result)),
    )
    iterator = trace.text_chat_stream()
    await anext(iterator)
    await anext(iterator)
    await iterator.aclose()
    assert records == ["returned"] and closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_after_first", [False, True])
async def test_real_persona_pipeline_sends_prefix_before_final_and_commits_once(
    host_fixture, jev_plugin, cancel_after_first
):
    from dataclasses import replace
    from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
    from astrbot_plugin_chat_dynamics.core.turn_decision import TurnContext, MessageSnapshot
    from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult, chain_plain_text

    ctx, _, calls = host_fixture
    plugin, _ = jev_plugin
    event = private_event()
    plugin._runtime_config = replace(plugin._runtime_config, private_chat_enabled=True, max_fragments=4)
    plugin.private_chat_enabled = True
    plugin.persona_engine.bridge = AstrBotAgentBridge(ctx)
    runtime = plugin._get_or_create_runtime(
        event.unified_msg_origin,
        group_id="",
        umo=event.unified_msg_origin,
        bot_id=event.get_self_id(),
        scope_kind="private",
    )
    now = plugin.time_service.time()
    author = event.get_sender_id()
    mid = str(event.message_obj.message_id)
    runtime.dag.add_message(msg_id=mid, user_id=author, text="你好", timestamp=now)
    turn = TurnContext(
        runtime.session_key,
        author,
        "你好",
        (MessageSnapshot(mid, author, "你好"),),
        (),
        runtime.epoch,
        runtime.user_revisions.get(author, 0),
        now,
        True,
        wake_kind="private",
        bot_id=runtime.bot_id,
    )
    item = ModelTurn(turn, (event,), {}, False, private_direct=True)
    first_sent, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def stream(**kwargs):
        calls.append(kwargs)
        yield LLMResponse(role="assistant", completion_text="你好呀。", is_chunk=True)
        await release.wait()
        yield LLMResponse(role="assistant", completion_text="欢迎回来。", is_chunk=True)
        yield LLMResponse(role="assistant", completion_text="你好呀。欢迎回来。")

    async def provider(*args, **kwargs):
        return "provider"

    async def send(_runtime, _event, fragment, **kwargs):
        if kwargs.get("native_source_validator"):
            assert await kwargs["native_source_validator"]()
        delivered.append(fragment if isinstance(fragment, str) else chain_plain_text(fragment))
        first_sent.set()
        return SendResult(True, str(len(delivered)))

    async def sleep(delay):
        await asyncio.sleep(0)

    ctx.provider.text_chat_stream = stream
    plugin.llm.resolve_provider_id = provider
    plugin._send_owned = send
    plugin.time_service.sleep = sleep
    cleanup = install_native_builder_bridge(NS(_shutting_down=False, narrative=None))
    task = asyncio.create_task(plugin.persona_engine.process(runtime, item))
    try:
        await asyncio.wait_for(first_sent.wait(), 2)
        assert delivered == ["你好呀。"] and not task.done() and not ctx.saved
        if cancel_after_first:
            runtime.cancel_owned_turns(author)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert delivered == ["你好呀。"] and len(calls) == 1 and len(ctx.saved) == 1
            assert ctx.saved[-1][-1]["content"] == "你好呀。"
            return
        release.set()
        await asyncio.wait_for(task, 3)
        assert delivered == ["你好呀。", "欢迎回来。"] and len(calls) == 1 and len(ctx.saved) == 1
        assert ctx.saved[-1][-1]["content"] == "你好呀。\n\n欢迎回来。"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if cleanup is not None:
            cleanup.uninstall()
        await plugin.terminate()


@pytest.mark.asyncio
async def test_confirmation_keeps_the_original_tool_capabilities(host_fixture):
    ctx, _, calls = host_fixture
    event = private_event("好的")

    async def forbidden(**kwargs):
        raise AssertionError("confirmation must keep tools")

    ctx.provider.text_chat_stream = forbidden

    async def partial(output):
        raise AssertionError("confirmation is not a tool-free greeting")

    bridge = AstrBotAgentBridge(ctx)
    cleanup = install_native_builder_bridge(NS(_shutting_down=False, narrative=None))
    try:
        await bridge.generate(
            event, (event,), "好的", await bridge.snapshot(event), "provider", on_partial_text=partial
        )
        assert calls[0]["func_tool"].get_tool("allowed") is not None
    finally:
        if cleanup is not None:
            cleanup.uninstall()
