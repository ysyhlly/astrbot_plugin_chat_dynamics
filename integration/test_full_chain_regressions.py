"""Real SDK media lifetime, model deadlines and agent history regressions."""
import asyncio
import json
import wave

import pytest

from .test_persona_agent import host_fixture as host_fixture
from .test_call_reply_chain import install_tool_response
from astrbot_plugin_chat_dynamics.tests.test_jev_decision_layer import jev_plugin as jev_plugin
from astrbot_plugin_chat_dynamics.tests.test_persona_model import flush, drain
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.message.components import At as SDKAt, Record

@pytest.mark.asyncio
async def test_max_steps_control_message_not_persisted_as_user(host_fixture):
    ctx, event, calls = host_fixture
    ctx.config["provider_settings"]["max_agent_step"] = 1
    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            return "done"
    install_tool_response(ctx, calls, Tool(name="allowed", description="audit", parameters={"type": "object", "properties": {}}))
    bridge = AstrBotAgentBridge(ctx)
    output = await bridge.generate(event, (event,), "请执行工具", await bridge.snapshot(event), "provider", history_text="真正的用户请求")
    assert len(calls) == 2
    assert await bridge.commit(event, output, "真实送达回复")
    assert ToolLoopAgentRunner.MAX_STEPS_REACHED_PROMPT not in json.dumps(ctx.saved[-1], ensure_ascii=False)

@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup', [False, True])
async def test_deferred_agent_keeps_host_temporary_audio_alive(host_fixture, jev_plugin, tmp_path, cleanup):
    ctx, event, calls = host_fixture
    p, _ = jev_plugin
    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(8000)
        audio_file.writeframes(b"\0\0" * 800)
    audio = Record.fromFileSystem(str(path))
    event.message_obj.message = [SDKAt(qq="bot"), audio]
    event.message_str = "帮我听一下这段语音"
    event.track_temporary_local_file(str(path))
    ctx.provider.provider_config["modalities"].append("audio")
    p.persona_engine.bridge = AstrBotAgentBridge(ctx)
    try:
        await p.on_group_message(event)
        assert path.exists() and not calls
        assert event.message_obj.message[1] is audio
        leases = [lease for lease in p._media_leases if lease.alive]
        assert len(leases) == 1
        # This is AstrBot PipelineScheduler.execute()'s finally action.
        if cleanup:
            event.cleanup_temporary_local_files()
        await flush(p, event)
        await drain(p)
        assert calls, "owned Agent did not reach the provider"
        contexts = [message if isinstance(message, dict) else message.model_dump() for message in calls[0]["contexts"]]
        assert "audio_url" in json.dumps(contexts, default=str), "audio was silently removed before the model call"
        assert not leases[0].alive, "completed turns must release their media copy"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_real_model_request_timeout_cancels_provider(host_fixture):
    ctx, event, calls = host_fixture
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def hang(**kwargs):
        calls.append(kwargs)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    ctx.provider.text_chat = hang
    bridge = AstrBotAgentBridge(ctx)
    with pytest.raises(asyncio.TimeoutError):
        await bridge.generate(event, (event,), "回答问题", await bridge.snapshot(event),
                              "provider", reply_timeout=.03)
    assert entered.is_set() and cancelled.is_set() and not ctx.saved and not event.sent


@pytest.mark.asyncio
async def test_reply_deadline_excludes_tool_execution(host_fixture):
    ctx, event, calls = host_fixture
    executed = []
    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            await asyncio.sleep(.08)
            executed.append("done")
            return "done"
    install_tool_response(ctx, calls, Tool(name="allowed", description="slow tool",
                                          parameters={"type": "object", "properties": {}}))
    bridge = AstrBotAgentBridge(ctx)
    output = await asyncio.wait_for(bridge.generate(
        event, (event,), "执行工具", await bridge.snapshot(event), "provider", reply_timeout=.04), 1)
    assert executed == ["done"] and output.text == "工具执行完成。" and len(calls) == 2


@pytest.mark.asyncio
async def test_reply_deadline_is_cumulative_across_model_rounds(host_fixture):
    ctx, event, calls = host_fixture
    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            return "done"
    install_tool_response(ctx, calls, Tool(name="allowed", description="tool",
                                          parameters={"type": "object", "properties": {}}))
    original = ctx.provider.text_chat
    async def slow(**kwargs):
        await asyncio.sleep(.04)
        return await original(**kwargs)
    ctx.provider.text_chat = slow
    bridge = AstrBotAgentBridge(ctx)
    with pytest.raises(asyncio.TimeoutError):
        await bridge.generate(event, (event,), "执行工具", await bridge.snapshot(event),
                              "provider", reply_timeout=.06)
    assert len(calls) == 1 and not ctx.saved


@pytest.mark.asyncio
async def test_real_user_text_matching_runner_control_is_preserved(host_fixture):
    ctx, event, calls = host_fixture
    control = ToolLoopAgentRunner.MAX_STEPS_REACHED_PROMPT
    history = json.loads(ctx.conv.history)
    history.insert(0, {"role": "user", "content": control})
    ctx.conv.history = json.dumps(history)
    ctx.config["provider_settings"]["max_agent_step"] = 1
    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            return "done"
    install_tool_response(ctx, calls, Tool(name="allowed", description="tool",
                                          parameters={"type": "object", "properties": {}}))
    bridge = AstrBotAgentBridge(ctx)
    output = await bridge.generate(event, (event,), control, await bridge.snapshot(event),
                                   "provider", history_text=control)
    assert await bridge.commit(event, output, "送达文本")
    assert len([message for message in ctx.saved[-1]
                if message["role"] == "user" and (message["content"] == control
                    or isinstance(message["content"], list) and any(
                        part.get("text") == control for part in message["content"]))]) == 2
