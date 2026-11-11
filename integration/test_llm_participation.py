"""Exercise the actual SDK Context.llm_generate path with an offline provider."""
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from .test_persona_agent import host_fixture as host_fixture
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.provider import Provider
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.llm_decision import LLMDecisionClient


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_actual_sdk_chat_completion_is_a_decision_without_tools_history_or_send(host_fixture, failed):
    ctx, event, _ = host_fixture
    assert 'llm_generate' not in ctx.__dict__, 'exercise the inherited SDK method'
    provider = Mock(spec=Provider)
    provider.provider_config = {'id': 'judge', 'model': 'offline'}
    provider.provider_settings = {}
    provider.model_name = 'offline'
    payload = {'answers': {
        'action': {'type': 'choice', 'choice': 'reply', 'confidence': .9, 'probabilities': {'ignore': .1, 'reply': .9}},
        'join': {'type': 'noul', 'noul': .9},
    }}
    provider.text_chat = AsyncMock(return_value=LLMResponse(
        role='err' if failed else 'assistant', completion_text='provider diagnostic' if failed else json.dumps(payload)))
    ctx.provider = provider
    ctx.provider_manager = NS(get_provider_by_id=AsyncMock(return_value=provider))
    cfg, _ = parse_runtime_config({'participation_backend': 'llm', 'decision_llm_provider': 'judge'})
    host = NS(context=ctx, _runtime_config=cfg, _shutting_down=False, jev=NS(evaluate=AsyncMock()))
    client = LLMDecisionClient(host)
    try:
        result = await client.evaluate(state={'conversation': {'text': '机箱风扇如何安装'}}, questions={
            'action': {'type': 'choice', 'criteria': {'ignore': 'silent', 'reply': 'answer'}, 'instructions': 'Choose action'},
            'join': {'type': 'noul', 'instructions': 'Should speak?'},
        }, umo=event.unified_msg_origin)
        assert (result is None) is failed
        if not failed:
            assert result == payload['answers']
        provider.text_chat.assert_awaited_once()
        sent = provider.text_chat.call_args.kwargs
        assert sent['func_tool'] is None and sent['contexts'] is None
        assert json.loads(sent['prompt'])['state']['conversation']['text'] == '机箱风扇如何安装'
        assert not ctx.saved and not event.sent
        host.jev.evaluate.assert_not_awaited()
    finally:
        await client.close()
