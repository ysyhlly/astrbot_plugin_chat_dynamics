"""Verify attributed facts survive the real SDK builder into provider inputs."""
import json
from dataclasses import replace
import pytest
from .test_persona_agent import host_fixture as host_fixture
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.turn_decision import reply_prompt, TurnDecision
from astrbot_plugin_chat_dynamics.tests.test_group_context import notice
from astrbot_plugin_chat_dynamics.tests.test_context_retrieval import route
from astrbot_plugin_chat_dynamics.tests.test_persona_explicit_context import _runtime, _snapshot
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG


@pytest.mark.asyncio
async def test_group_events_and_topic_constraints_reach_real_provider(host_fixture):
    context, event, calls = host_fixture
    dag = ConversationDAG('room')
    route(dag.add_message('old', 'alice', '预算只有500元，必须离线运行', timestamp=100), 'task')
    current = route(dag.add_message('now', 'u', '新来的朋友问这个方案怎么改', timestamp=500), 'task')
    runtime = _runtime(dag)
    runtime.group_events = (notice(),)
    turn = _snapshot(runtime, current).context
    turn = replace(turn, wake_kind='at')
    prompt = reply_prompt(turn, TurnDecision.fallback(turn, 'at_mandatory'))
    bridge = AstrBotAgentBridge(context)
    await bridge.generate(event, (event,), prompt, await bridge.snapshot(event), 'provider')
    serialized = json.dumps([{key: call.get(key) for key in ("contexts", "prompt", "system_prompt")}
                             for call in calls], ensure_ascii=False)
    assert 'member_joined' in serialized and '12345' in serialized
    assert '预算只有500元' in serialized and '必须离线运行' in serialized
    assert 'not necessarily that newcomer' in serialized


@pytest.mark.asyncio
async def test_onebot_adapter_preserves_join_fact_for_context():
    from types import SimpleNamespace
    from aiocqhttp import Event as OneBotEvent
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot_plugin_chat_dynamics.core.platform_bridge import parse_group_event
    from astrbot_plugin_chat_dynamics.core.group_context import member_notice
    raw = OneBotEvent.from_payload({'post_type': 'notice', 'notice_type': 'group_increase',
                                    'sub_type': 'invite', 'group_id': 123, 'user_id': 456,
                                    'operator_id': 789, 'self_id': 999, 'time': 1000})
    adapter = AiocqhttpAdapter.__new__(AiocqhttpAdapter)
    message = await adapter._convert_handle_notice_event(raw)
    event = AstrMessageEvent('', message, SimpleNamespace(id='qq', name='aiocqhttp'), '123')
    fact = member_notice(event, parse_group_event(event), now=100, wall_time=1000)
    assert fact.kind == 'member_joined' and fact.member_id == '456'
    assert fact.operator_id == '789' and event.message_str == ''
