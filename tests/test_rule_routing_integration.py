from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.jev_decision import ADDRESSEE_PREFIX
from astrbot_plugin_chat_dynamics.core.discussion_reranker import DiscussionReranker
from astrbot_plugin_chat_dynamics.core.topic_reranker import TopicReranker
from .test_jev_decision_layer import JevDouble, answers
from . import test_jev_decision_layer

from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent
from astrbot_plugin_chat_dynamics.core.dashboard import scene_replay_snapshot

legacy_jev_plugin = test_jev_decision_layer.jev_plugin


@pytest.fixture
def jev_plugin(legacy_jev_plugin):
    p, bridge = legacy_jev_plugin
    p.config['rule_topic_routing_enabled'] = True
    p._sync_runtime_from_config()
    return p, bridge


@pytest.mark.asyncio
async def test_participation_call_has_no_attribution_questions_and_ignores_extra_answers(jev_plugin):
    p, bridge = jev_plugin
    p.jev = JevDouble(answers(topic={'type': 'choice', 'choice': 'NEW', 'confidence': .99},
        recipient={'type': 'choice', 'choice': 'bot', 'confidence': .99}))
    try:
        event = MockEvent('有人知道机箱散热风扇怎么安装吗', message_id='rules-1')
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert p.jev.calls
        questions = p.jev.calls[0]['questions']
        assert 'recipient' not in questions and 'topic' not in questions
        assert not any(q.startswith(ADDRESSEE_PREFIX) for q in questions)
        assert 'action' in questions
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.jev_decision['recipient_source'] == 'rules'
        assert runtime.jev_addressees['rules-1']['kind'] == 'unclear'
        assert runtime.dag.nodes['rules-1'].metadata['routing']['routing_mode'] == 'rules'
        if bridge.requests:
            rows = bridge.requests[0][0]['conversation_map']['messages']
            assert next(row for row in rows if row['message_id'] == 'rules-1')['addressee']['kind'] == 'unclear'
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_batch_uses_native_rerank_outside_lock_with_independent_title_model(jev_plugin):
    p, bridge = jev_plugin
    try:
        p.config['discussion_reranker_provider'] = 'native-test'
        p._sync_runtime_from_config()
        event = MockEvent('电脑显卡散热方案', message_id='a')
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        runtime = p._sessions[event.unified_msg_origin]
        n = runtime.dag.add_message('b', 'another', '机箱风扇安装方案', timestamp=runtime.dag.nodes['a'].timestamp + 1)
        p._route_message(runtime, n)
        async def rank(**kwargs):
            assert not runtime.state_lock.locked()
            return [SimpleNamespace(index=i, relevance_score=.9) for i, _ in enumerate(kwargs['documents'])]
        provider = SimpleNamespace(rerank=AsyncMock(side_effect=rank))
        p.context.get_provider_by_id = lambda pid: provider if pid == 'native-test' else None
        assert isinstance(p._topic_reranker(), DiscussionReranker)
        assert isinstance(p._topic_reranker(for_display=True), TopicReranker)
        await p.topic_batches.process(runtime, [(n, 0)], is_current=lambda: True)
        provider.rerank.assert_awaited_once()
        assert n.metadata['routing']['topic_id'] == 'a'
        assert n.metadata['routing']['addressee_ids'] == []
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_rules_can_roll_back_without_changing_provider_type(jev_plugin):
    p, _ = jev_plugin
    try:
        p.config['rule_topic_routing_enabled'] = False
        p._sync_runtime_from_config()
        assert p.thread_router.rule_enabled is False
        assert not isinstance(p._topic_reranker(), DiscussionReranker)
        event = MockEvent('有人知道机箱散热风扇怎么安装吗', message_id='legacy-1')
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert any(q.startswith(ADDRESSEE_PREFIX) for q in p.jev.calls[0]['questions'])
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_representative_does_not_leak_hidden_message_body(jev_plugin):
    p, _ = jev_plugin
    try:
        event = MockEvent('我的电脑显卡风扇需要更换', message_id='private-label')
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        p.console_show_message_content = False
        blocks = scene_replay_snapshot(p)['topic_blocks']
        assert blocks and all('显卡' not in b['topic_title'] for b in blocks)
        assert all('需要更换' not in b['topic_title'] for b in blocks)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_config_rejects_chat_provider_and_out_of_range_rank_scores(jev_plugin):
    p, _ = jev_plugin
    try:
        p.context.get_provider_by_id = lambda pid: SimpleNamespace(generate=AsyncMock())
        panel = p.config_panel if hasattr(p, 'config_panel') else None
        from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel
        panel = ConfigPanel(p)
        with pytest.raises(ValueError, match='原生重排序'):
            await panel.save_config_values({'discussion_reranker_provider': 'chat'})
        with pytest.raises(ValueError):
            await panel.save_config_values({'discussion_rerank_threshold': 1.2})
    finally:
        await p.terminate()
