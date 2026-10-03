"""Real SDK regressions for auxiliary model budgets and continued media access."""
import asyncio
import json

import pytest
from PIL import Image as PILImage
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import At as SDKAt, File, Image
from astrbot.core.provider.entities import LLMResponse
from .test_persona_agent import host_fixture as host_fixture, Event
from .test_call_reply_chain import install_tool_response
from astrbot_plugin_chat_dynamics.tests.test_jev_decision_layer import jev_plugin as jev_plugin
from astrbot_plugin_chat_dynamics.tests.test_persona_model import flush, drain
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.media_archive import MediaArchive

@pytest.mark.asyncio
async def test_skills_like_model_requery_obeys_reply_deadline(host_fixture):
    ctx, event, calls = host_fixture
    ctx.config['provider_settings']['tool_schema_mode'] = 'skills_like'
    class Tool(FunctionTool):
        async def call(self, context, **kwargs):
            return 'done'
    install_tool_response(ctx, calls, Tool(name='allowed', description='audit', parameters={
        'type': 'object', 'properties': {'x': {'type': 'string'}}, 'required': ['x']}))
    original = ctx.provider.text_chat
    entered = asyncio.Event()
    async def delayed_requery(**kwargs):
        if len(calls) == 1:
            entered.set()
            await asyncio.sleep(.12)
        return await original(**kwargs)
    ctx.provider.text_chat = delayed_requery
    bridge = AstrBotAgentBridge(ctx)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge.generate(event, (event,), '执行工具',
            await bridge.snapshot(event), 'provider', reply_timeout=.03), 1)
    assert entered.is_set() and len(calls) == 1 and not ctx.saved

@pytest.mark.asyncio
async def test_summary_model_obeys_reply_deadline(host_fixture, monkeypatch):
    ctx, event, calls = host_fixture
    ctx.config['provider_settings']['llm_compress_provider_id'] = 'provider'
    ctx.config['provider_settings']['context_limit_reached_strategy'] = 'llm_compress'
    ctx.provider.provider_config['max_context_tokens'] = 8000
    ctx.conv.history = json.dumps([{'role': 'user', 'content': '前文' * 18000},
                                  {'role': 'assistant', 'content': '收到'}])
    summary_calls = []
    async def summarize(**kwargs):
        calls.append(kwargs)
        if 'Generate a summary of our previous conversation history.' in str(kwargs.get('contexts')):
            summary_calls.append(kwargs)
            await asyncio.sleep(.12)
            return LLMResponse(role='assistant', completion_text='摘要')
        return LLMResponse(role='assistant', completion_text='回复')
    ctx.provider.text_chat = summarize
    # The fixture provider is a transport double, not a registered Provider subclass.
    # Resolve that fixture directly; the SDK's actual ContextManager and compressor still run.
    from astrbot.core import astr_main_agent
    async def resolve_compressor(*args, **kwargs):
        return ctx.provider
    monkeypatch.setattr(astr_main_agent, '_get_compress_provider', resolve_compressor)
    bridge = AstrBotAgentBridge(ctx)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge.generate(event, (event,), '当前问题',
            await bridge.snapshot(event), 'provider', reply_timeout=.03), 1)
    assert summary_calls and len(calls) == 1 and not ctx.saved

@pytest.mark.asyncio
async def test_attachment_tool_can_read_file_in_next_turn(host_fixture, jev_plugin, tmp_path):
    ctx, event, calls = host_fixture
    p, _ = jev_plugin
    p.media_archive = MediaArchive(tmp_path / 'archive')
    source = tmp_path / 'picture.png'
    picture = PILImage.new('RGB', (2, 1))
    picture.putdata([(255, 0, 0), (0, 255, 0)])
    picture.save(source)
    event.message_obj.message = [SDKAt(qq='bot'), Image.fromFileSystem(str(source))]
    original_ref = event.message_obj.message[1].file
    event.message_str = '请用图片工具读取第一像素'
    reads, model_count, attachment = [], 0, None
    class ReadFile(FunctionTool):
        async def call(self, context, path, page):
            try:
                with PILImage.open(path) as image:
                    result = str(image.getpixel((page - 1, 0)))
            except OSError:
                result = 'FILE_MISSING'
            reads.append((path, page, result))
            return result
    ctx.tools['allowed'] = ReadFile(name='allowed', description='read page', parameters={
        'type': 'object', 'properties': {'path': {'type': 'string'}, 'page': {'type': 'integer'}},
        'required': ['path', 'page']})
    async def model(**kwargs):
        nonlocal model_count, attachment
        calls.append(kwargs)
        model_count += 1
        if model_count == 1:
            texts = []
            for message in kwargs['contexts']:
                content = message.get('content') if isinstance(message, dict) else message.content
                if isinstance(content, list):
                    texts.extend(part.get('text', '') if isinstance(part, dict) else getattr(part, 'text', '') for part in content)
            texts.extend(getattr(part, 'text', '') for part in kwargs.get('extra_user_content_parts', []))
            attachment = next(text.split('path ', 1)[1].rsplit(']', 1)[0] for text in texts if '[Image Attachment:' in text)
        if model_count in (1, 3):
            return LLMResponse(role='assistant', completion_text='', tools_call_name=['allowed'],
                tools_call_args=[{'path': attachment, 'page': 1 if model_count == 1 else 2}], tools_call_ids=[f'call{model_count}'])
        return LLMResponse(role='assistant', completion_text='已读取')
    ctx.provider.text_chat = model
    p.persona_engine.bridge = AstrBotAgentBridge(ctx)
    try:
        await p.on_group_message(event)
        assert event.message_obj.message[1].file == original_ref
        await flush(p, event)
        await drain(p)
        assert reads and reads[0][2] == '(255, 0, 0)', 'first-turn file control failed'
        assert source.exists() and attachment != str(source), 'owned-copy control failed'
        followup = Event()
        followup.message_obj.message_id = 'm2'
        followup.message_obj.message = [SDKAt(qq='bot')]
        followup.message_str = '继续用图片工具读取同一图片第二像素'
        await p.on_group_message(followup)
        await flush(p, followup)
        await drain(p)
        assert len(reads) == 2, f'follow-up tool control failed: {reads!r}'
        assert reads[1][2] == '(0, 255, 0)', f'first turn deleted the file still referenced in history: {reads!r}'
    finally:
        await p.terminate()

@pytest.mark.asyncio
@pytest.mark.parametrize('uri', [False, True])
async def test_real_sdk_local_file_is_accepted_at_ingress(host_fixture, jev_plugin, tmp_path, uri):
    ctx, event, _ = host_fixture
    p, _ = jev_plugin
    p.media_archive = MediaArchive(tmp_path / 'archive')
    path = tmp_path / '文件 document.txt'
    path.write_text('uploaded document')
    original_ref = path.as_uri() if uri else str(path)
    event.message_obj.message = [SDKAt(qq='bot'), File(name='document.txt', file=original_ref)]
    p.persona_engine.bridge = AstrBotAgentBridge(ctx)
    try:
        await p.on_group_message(event)
        assert event.message_obj.message[1].file_ == original_ref
        await flush(p, event)
        await drain(p)
        assert event.sent, 'explicit file request should receive a reply'
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_context_token_limit_preserves_actual_current_request(host_fixture):
    ctx, event, calls = host_fixture
    ctx.provider.provider_config['max_context_tokens'] = 8000
    ctx.conv.history = json.dumps([{'role': 'user', 'content': '前文' * 18000},
                                  {'role': 'assistant', 'content': '收到'}])
    bridge = AstrBotAgentBridge(ctx)
    result = await bridge.generate(event, (event,), 'CURRENT_ACTIVE_REQUEST',
        await bridge.snapshot(event), 'provider', history_text='CURRENT_ACTIVE_REQUEST')
    assert calls and 'CURRENT_ACTIVE_REQUEST' in str(calls[-1]['contexts'])
    assert result.turn_start is not None


@pytest.mark.asyncio
async def test_overlapping_turns_do_not_share_or_mutate_provider_budget(host_fixture):
    ctx, event, calls = host_fixture
    async def slow(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(.08)
        return LLMResponse(role='assistant', completion_text='独立回复')
    ctx.provider.text_chat = slow
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    results = await asyncio.gather(
        bridge.generate(event, (event,), '短预算', persona, 'provider', reply_timeout=.03),
        bridge.generate(event, (event,), '长预算', persona, 'provider', reply_timeout=.3),
        return_exceptions=True)
    assert isinstance(results[0], asyncio.TimeoutError)
    assert results[1].text == '独立回复' and ctx.provider.text_chat is slow
