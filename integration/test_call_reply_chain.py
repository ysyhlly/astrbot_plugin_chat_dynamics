"""Real AstrBot runner regressions for tools, media and delivered history boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from .test_persona_agent import host_fixture as host_fixture
from astrbot_plugin_chat_dynamics.tests.test_jev_decision_layer import jev_plugin as jev_plugin
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse

@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3])
async def test_real_sdk_paragraph_reply_sends_and_commits_complete_prose(host_fixture, jev_plugin, limit):
    from dataclasses import replace
    from astrbot_plugin_chat_dynamics.tests.test_paragraph_sending import PARAGRAPHS, REPLY

    ctx, event, calls = host_fixture
    p, _ = jev_plugin
    p._runtime_config = replace(p._runtime_config, max_fragments=limit)
    p.persona_engine.bridge = AstrBotAgentBridge(ctx)
    runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.get_group_id(),
                                       umo=event.unified_msg_origin, bot_id=event.get_self_id())
    delivered, delays = [], []

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(role="assistant", completion_text=REPLY)

    async def provider(*args, **kwargs):
        return "provider"

    async def sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    async def remember(result, text):
        assert result.success
        delivered.append(text)

    ctx.provider.text_chat = chat
    p.llm.resolve_provider_id = provider
    p.time_service.sleep = sleep
    try:
        reply = await p._speak_poke_with_persona(runtime, event, prompt="请回应这个拥抱", history_text="抱一下",
                                                 current=lambda: True, remember=remember)
        expected = [REPLY] if limit == 1 else PARAGRAPHS
        assert [chain.get_plain_text() for chain in event.sent] == delivered == expected
        assert reply == REPLY and delays == [pytest.approx(1.2)] * (limit - 1)
        assert len(calls) == 1 and len(ctx.saved) == 1
        history = ctx.saved[-1]
        assert any(message["role"] == "user" and "抱一下" in str(message["content"]) for message in history)
        assert history[-1]["role"] == "assistant" and history[-1]["content"] == REPLY
    finally:
        await p.terminate()

def install_tool_response(ctx, calls, tool):
    ctx.tools["allowed"] = tool

    async def chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(role="assistant", completion_text="UNSENT_TOOL_DRAFT", tools_call_name=["allowed"],
                               tools_call_args=[{}], tools_call_ids=["call1"])
        return LLMResponse(role="assistant", completion_text="工具执行完成。")

    ctx.provider.text_chat = chat

@pytest.mark.asyncio
async def test_real_sdk_yielded_tool_result_reaches_delivery(host_fixture):
    from astrbot.core.message.message_event_result import MessageEventResult

    ctx, event, calls = host_fixture
    executed = []

    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            executed.append(True)
            yield MessageEventResult().message("工具直出文件结果")

    install_tool_response(ctx, calls, Tool(name="allowed", description="offline tool",
                                         parameters={"type": "object", "properties": {}}))
    bridge = AstrBotAgentBridge(ctx)
    output = await bridge.generate(event, (event,), "请执行工具", await bridge.snapshot(event), "provider")
    assert executed and calls
    assert any("工具直出文件结果" in chain.get_plain_text() for chain in output.chains)

@pytest.mark.asyncio
async def test_real_sdk_tool_keyword_send_reaches_delivery(host_fixture):
    ctx, event, calls = host_fixture
    executed = []

    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            executed.append(True)
            await context.context.event.send(message=MessageChain().message("关键字发送结果"))
            return "发送完成"

    install_tool_response(ctx, calls, Tool(name="allowed", description="offline tool",
                                         parameters={"type": "object", "properties": {}}))
    bridge = AstrBotAgentBridge(ctx)
    output = await bridge.generate(event, (event,), "请执行工具", await bridge.snapshot(event), "provider")
    assert executed and len(calls) == 2
    assert any("关键字发送结果" in chain.get_plain_text() for chain in output.chains)

@pytest.mark.asyncio
async def test_real_sdk_tool_image_does_not_persist_internal_prompt(host_fixture, monkeypatch):
    from astrbot.core.agent.runners import tool_loop_agent_runner as runner_module
    from mcp.types import CallToolResult, ImageContent

    ctx, event, calls = host_fixture
    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            return CallToolResult(content=[ImageContent(type="image", data="iVBORw0KGgo=", mimeType="image/png")])

    install_tool_response(ctx, calls, Tool(name="allowed", description="offline image tool",
                                         parameters={"type": "object", "properties": {}}))
    monkeypatch.setattr(runner_module.tool_image_cache, "save_image", lambda **kwargs: SimpleNamespace(
        tool_name="allowed", file_path="offline-image.png", mime_type="image/png"))
    monkeypatch.setattr(runner_module.tool_image_cache, "get_image_base64_by_path",
                        lambda *_args: ("iVBORw0KGgo=", "image/png"))
    bridge = AstrBotAgentBridge(ctx)
    output = await bridge.generate(event, (event,), "INTERNAL_RESPONSE_PLAN_AND_CONTEXT", await bridge.snapshot(event),
                                   "provider", history_text="原始用户请求")
    assert len(calls) == 2
    assert await bridge.commit(event, output, "真实送达回复")
    saved = json.dumps(ctx.saved[-1], ensure_ascii=False)
    assert "原始用户请求" in saved and "真实送达回复" in saved
    assert "INTERNAL_RESPONSE_PLAN_AND_CONTEXT" not in saved
    assert "UNSENT_TOOL_DRAFT" not in saved
    assert "之前已经送达" in saved
    history = ctx.saved[-1]
    assert output.turn_start == next(i + 1 for i, m in enumerate(history)
                                    if m["role"] == "user" and "原始用户请求" in str(m["content"]))
    assert any(m["role"] == "user" and "image_url" in str(m["content"])
               and "原始用户请求" not in str(m["content"]) for m in history)
    assert any(m["role"] == "tool" and m["tool_call_id"] == "call1" for m in history)

@pytest.mark.asyncio
async def test_real_sdk_mixed_final_response_preserves_media(host_fixture):
    from astrbot.core.message.components import Image

    ctx, event, calls = host_fixture
    chain = MessageChain().message("图片已生成")
    chain.chain.append(Image.fromBase64("iVBORw0KGgo="))

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(role="assistant", completion_text="图片已生成", result_chain=chain)

    ctx.provider.text_chat = chat
    bridge = AstrBotAgentBridge(ctx)
    output = await bridge.generate(event, (event,), "生成图片", await bridge.snapshot(event), "provider")
    assert calls
    assert any(any(type(c).__name__ == "Image" for c in result.chain) for result in output.chains)
    from astrbot_plugin_chat_dynamics.core.persona_engine import delivery_fragments
    pacer = SimpleNamespace(persona_fragments=lambda _: ["图片", "已生成"])
    assert delivery_fragments(output.chains, output.text, pacer) == [chain]

@pytest.mark.asyncio
async def test_poke_commit_cannot_overwrite_a_concurrent_delivered_turn(host_fixture, jev_plugin):
    ctx, event, calls = host_fixture
    p, _ = jev_plugin
    bridge = AstrBotAgentBridge(ctx)
    p.persona_engine.bridge = bridge
    runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.get_group_id(),
                                       umo=event.unified_msg_origin, bot_id=event.get_self_id())
    committing, release = asyncio.Event(), asyncio.Event()

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(role="assistant", completion_text="POKE_DELIVERED" if len(calls) == 1 else "NEXT_DELIVERED")

    async def update(umo, cid, **kwargs):
        serialized = json.dumps(kwargs["history"])
        if "POKE_DELIVERED" in serialized and "NEXT_DELIVERED" not in serialized:
            committing.set()
            await release.wait()
        ctx.saved.append(kwargs["history"])
        ctx.conv.history = serialized

    async def provider(*args, **kwargs):
        return "provider"

    async def remember(*args):
        pass

    async def next_turn():
        async with bridge.session_lock(runtime.session_key):
            output = await bridge.generate(event, (event,), "另一条已送达请求", await bridge.snapshot(event),
                                           "provider", history_text="另一条原文")
            assert await bridge.commit(event, output, "NEXT_DELIVERED")

    ctx.provider.text_chat = chat
    ctx.conversation_manager.update_conversation = update
    p.llm.resolve_provider_id = provider
    poke_task = asyncio.create_task(p._speak_poke_with_persona(runtime, event, prompt="戳一戳", history_text="戳一戳原文",
                                                               current=lambda: True, remember=remember))
    next_task = None
    try:
        await asyncio.wait_for(committing.wait(), 2)
        next_task = asyncio.create_task(next_turn())
        await asyncio.sleep(.05)
        release.set()
        await asyncio.wait_for(asyncio.gather(poke_task, next_task), 3)
        assert "POKE_DELIVERED" in ctx.conv.history
        assert "NEXT_DELIVERED" in ctx.conv.history
    finally:
        release.set()
        await asyncio.gather(poke_task, *( [next_task] if next_task else []), return_exceptions=True)
        await p.terminate()

@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "stop", "reset", "unload"])
async def test_real_sdk_cancellation_stops_tool_task_and_later_tools(host_fixture, jev_plugin, ending):
    from collections import deque
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent

    ctx, event, calls = host_fixture
    p, _ = jev_plugin
    p.persona_engine.bridge = bridge = AstrBotAgentBridge(ctx)
    runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.get_group_id(),
                                      umo=event.unified_msg_origin, bot_id=event.get_self_id())
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects = []
    receipts = deque()

    class BlockedTool(FunctionTool):
        async def call(self, context, **kwargs):
            entered.set()
            try:
                await release.wait()
                effects.append("old tool resumed")
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "finished"

    class LaterTool(FunctionTool):
        async def call(self, context, **kwargs):
            effects.append("later tool executed")
            return "finished"

    ctx.tools["allowed"] = BlockedTool(name="allowed", description="blocked", parameters={"type": "object"})
    ctx.tools["later"] = LaterTool(name="later", description="later", parameters={"type": "object"})
    ctx.persona_manager.personas_v3[0]["tools"].append("later")

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(role="assistant", completion_text="", tools_call_name=["allowed", "later"],
                           tools_call_args=[{}, {}], tools_call_ids=["first", "second"])

    ctx.provider.text_chat = chat
    persona = await bridge.snapshot(event)
    task = asyncio.create_task(p.persona_engine.run_owned(
        runtime, event.get_sender_id(),
        lambda: p.persona_engine.generate(event, (event,), "执行工具", persona, "provider", execution_log=receipts),
        timeout=5))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if ending == "stop":
            stop = MockEvent("/dynamics_stop", group_id=event.get_group_id(), sender_id=event.get_sender_id())
            stop.unified_msg_origin = event.unified_msg_origin
            await p.cmd_dynamics_stop(stop)
        elif ending == "reset":
            await p._reset_session_state_async(runtime.session_key)
        elif ending == "unload":
            await p.terminate()
        else:
            task.cancel()
        await asyncio.wait_for(cancelled.wait(), 2)
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert not effects and not ctx.saved and not event.sent
        assert len(calls) == 1
        assert [(r["tool"], r["status"]) for r in receipts] == [("allowed", "started")]
        assert not runtime.owned_turn_tasks
        assert p.llm.provider_budget.diagnostics()["active"] == 0
    finally:
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.terminate()


@pytest.mark.asyncio
async def test_real_sdk_generation_shares_provider_capacity(host_fixture, jev_plugin):
    ctx, event, calls = host_fixture
    p, _ = jev_plugin
    p.persona_engine.bridge = bridge = AstrBotAgentBridge(ctx)
    release = asyncio.Event()
    capacity = p.llm.provider_budget.capacity

    async def chat(**kwargs):
        calls.append(kwargs)
        await release.wait()
        return LLMResponse(role="assistant", completion_text="已完成")

    ctx.provider.text_chat = chat
    persona = await bridge.snapshot(event)
    tasks = [asyncio.create_task(p.persona_engine.generate(event, (event,), "请求", persona, "provider"))
             for _ in range(capacity + 2)]
    try:
        for _ in range(1000):
            if len(calls) == capacity and p.llm.provider_budget.diagnostics()["queued"] == 2:
                break
            await asyncio.sleep(.001)
        assert len(calls) == capacity
        assert p.llm.provider_budget.diagnostics()["active"] == capacity
        release.set()
        outputs = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert len(calls) == capacity + 2 and len(outputs) == capacity + 2
        assert p.llm.provider_budget.diagnostics()["active"] == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await p.terminate()
@pytest.mark.asyncio
async def test_poke_cancellation_commits_delivered_head_under_host_lock(host_fixture, jev_plugin):
    ctx, event, _ = host_fixture
    p, _ = jev_plugin
    p.persona_engine.bridge = bridge = AstrBotAgentBridge(ctx)
    runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.get_group_id(),
                                      umo=event.unified_msg_origin, bot_id=event.get_self_id())
    entered = asyncio.Event()
    block = asyncio.Event()
    p.pacer = SimpleNamespace(persona_fragments=lambda text: ["已送达首段", "未送达尾段"])

    async def remember(*args):
        entered.set()
        await block.wait()

    async def provider(*args):
        return "provider"

    p.llm.resolve_provider_id = provider
    task = asyncio.create_task(p.persona_engine.run_owned(
        runtime, "u", lambda: p._speak_poke_with_persona(
            runtime, event, prompt="戳一戳", history_text="用户戳一戳",
            current=lambda: True, remember=remember), timeout=5))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runtime.cancel_owned_turns("u")
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert len(event.sent) == 1
        saved = json.dumps(json.loads(ctx.conv.history), ensure_ascii=False)
        assert "已送达首段" in saved and "未送达尾段" not in saved
        assert "未送达第二段" not in saved and "用户戳一戳" in saved
        assert not runtime.owned_turn_tasks
        async with bridge.session_lock(runtime.session_key):
            assert ctx.saved
    finally:
        task.cancel()
        block.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.terminate()
