"""Real SDK tool loop with offline provider: no draft is an early output."""
import asyncio
import pytest
from .test_persona_agent import host_fixture as host_fixture
from .test_call_reply_chain import install_tool_response
from astrbot.core.agent.tool import FunctionTool
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge


@pytest.mark.asyncio
async def test_real_tool_round_only_publishes_final_output_and_stage_times(host_fixture):
    ctx, event, calls = host_fixture
    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            return 'offline tool result'
    install_tool_response(ctx, calls, Tool(name='allowed', description='offline', parameters={'type':'object','properties':{}}))
    bridge = AstrBotAgentBridge(ctx)
    rows, ready = [], []
    def stage(name, seconds, outcome): rows.append((name, seconds, outcome))
    async def output_ready(output):
        assert output.text == '工具执行完成。'
        assert await output.native_sources_current()
        ready.append(output)
    output = await bridge.generate(event, (event,), '执行这个工具', await bridge.snapshot(event), 'provider',
                                   on_stage=stage, on_output_ready=output_ready, history_text='真实请求')
    assert ready == [output] and len(calls) == 2
    names = [r[0] for r in rows]
    assert names.count('model_round') == 2 and names.count('model_first_content') == 2
    assert {'tool_execution', 'response_hooks', 'context_compaction', 'request_hooks', 'agent_build', 'agent_finalize'} <= set(names)
    assert 'UNSENT_TOOL_DRAFT' not in repr(rows) and not event.sent
    assert await bridge.commit(event, output, '工具执行完成。')


@pytest.mark.asyncio
async def test_real_final_output_precedes_optional_cleanup(host_fixture):
    ctx, event, calls = host_fixture
    bridge = AstrBotAgentBridge(ctx)
    ready, release = asyncio.Event(), asyncio.Event()
    async def on_ready(output): ready.set()
    async def stage(name, seconds, outcome):
        if name == 'agent_finalize':
            await release.wait()
    task = asyncio.create_task(bridge.generate(event, (event,), '普通请求', await bridge.snapshot(event), 'provider',
                                              on_stage=stage, on_output_ready=on_ready))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert not task.done() and len(calls) == 1
        release.set()
        assert (await task).text
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
