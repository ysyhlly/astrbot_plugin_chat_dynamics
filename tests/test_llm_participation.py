import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.dashboard import jev_snapshot
from astrbot_plugin_chat_dynamics.core.decision_status import DecisionStatusWebAPI
from astrbot_plugin_chat_dynamics.core.llm_decision import LLMDecisionClient, parse_answers
from astrbot_plugin_chat_dynamics.core.provider_budget import ProviderBudget
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_jev_turn_completion import completion_plugin as completion_plugin, tick
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent
from .test_strong_wake_delivery import quoted_event


QUESTIONS = {
    'action': {'type': 'choice', 'criteria': {'ignore': 'silent', 'reply': 'answer'}, 'instructions': 'Choose action'},
    'join': {'type': 'noul', 'instructions': 'Should speak?'},
    'rating': {'type': 'score', 'criteria': ['low', 'high'], 'instructions': 'Rate'},
}


def answers_for(questions, **choices):
    preferred = {'completion': 'complete', 'action': 'reply', 'state': 'focused', 'length': 'brief',
                 'reason': 'addressed_question', 'recipient': 'bot', 'communication_intent': 'task',
                 'expression_mode': 'literal', 'turn_update': 'unclear', 'context_need': 'ready'}
    preferred.update(choices)
    result = {}
    for key, spec in questions.items():
        kind = spec['type']
        if kind == 'noul':
            result[key] = {'type': kind, 'noul': preferred.get(key, .95)}
            continue
        offered = list(spec['criteria']) if kind == 'choice' else [str(i) for i in range(len(spec['criteria']))]
        selected = preferred.get(key, offered[0])
        if selected not in offered:
            selected = offered[0]
        probabilities = {label: (1.0 if len(offered) == 1 else .9 if label == selected else .1 / (len(offered) - 1)) for label in offered}
        result[key] = {'type': kind, kind: selected if kind == 'choice' else float(selected),
                       'confidence': .95, 'probabilities': probabilities}
    return {'answers': result}


def client_fixture(handler=None, **settings):
    cfg, _ = parse_runtime_config({'participation_backend': 'llm', 'decision_llm_provider': 'judge', **settings})
    provider = NS(model_name='judge-model', text_chat=AsyncMock(), provider_config={'id': 'judge', 'api_key': 'SECRET'})

    async def generate(**kwargs):
        if handler:
            return await handler(kwargs)
        prompt = json.loads(kwargs['prompt'])
        return NS(role='assistant', completion_text=json.dumps(answers_for(prompt['questions'])))

    ctx = NS(get_provider_by_id=lambda _: provider, llm_generate=AsyncMock(side_effect=generate))
    host = NS(_runtime_config=cfg, context=ctx, _shutting_down=False, jev=NS(evaluate=AsyncMock()))
    return LLMDecisionClient(host), host, provider


def test_strict_shared_choice_boolean_and_score_contract():
    envelope = answers_for(QUESTIONS)
    assert parse_answers(json.dumps(envelope), QUESTIONS) == envelope['answers']
    assert parse_answers('```json\n' + json.dumps(envelope) + '\n```', QUESTIONS) == envelope['answers']


@pytest.mark.parametrize('mutation', [
    'outsider_choice', 'missing_question', 'extra_question', 'missing_type', 'boolean_confidence',
    'nan', 'missing_probability', 'wrong_sum', 'choice_not_maximum', 'extra_text', 'bad_noul',
])
def test_invalid_model_decisions_cannot_enter_the_shared_pipeline(mutation):
    envelope = answers_for(QUESTIONS)
    item = envelope['answers']['action']
    if mutation == 'outsider_choice':
        item['choice'] = 'publish_everything'
    elif mutation == 'missing_question':
        envelope['answers'].pop('join')
    elif mutation == 'extra_question':
        envelope['answers']['secret_request'] = {'type': 'noul', 'noul': 1}
    elif mutation == 'missing_type':
        item.pop('type')
    elif mutation == 'boolean_confidence':
        item['confidence'] = True
    elif mutation == 'nan':
        item['confidence'] = float('nan')
    elif mutation == 'missing_probability':
        item['probabilities'].pop('ignore')
    elif mutation == 'wrong_sum':
        item['probabilities']['ignore'] = .9
    elif mutation == 'choice_not_maximum':
        item['choice'] = 'ignore'
    elif mutation == 'extra_text':
        item['reply_text'] = 'must never be sent'
    else:
        envelope['answers']['join']['noul'] = -1
    with pytest.raises(Exception):
        parse_answers(json.dumps(envelope), QUESTIONS)


def test_json_is_never_dug_out_of_prose_and_duplicate_keys_are_rejected():
    with pytest.raises(ValueError):
        parse_answers('Here is my answer: ' + json.dumps(answers_for(QUESTIONS)), QUESTIONS)
    with pytest.raises(ValueError):
        parse_answers('{"answers":{},"answers":{}}', QUESTIONS)


@pytest.mark.asyncio
async def test_one_llm_call_no_jev_fallback_and_redacted_runtime_status():
    client, host, _ = client_fixture()
    result = await client.evaluate(state={'conversation': {'text': '测试问题'}}, questions=QUESTIONS, umo='platform:group:one')
    assert result['action']['choice'] == 'reply'
    host.context.llm_generate.assert_awaited_once()
    host.jev.evaluate.assert_not_awaited()
    assert client.snapshot()['protocol'] == 'chat_json'
    assert client.snapshot()['available'] and client.snapshot()['calls'] == 1
    assert 'SECRET' not in str(client.snapshot())


@pytest.mark.asyncio
async def test_session_model_fallback_uses_the_full_current_umo_each_time():
    client, host, _ = client_fixture(decision_llm_provider='')
    host.context.get_current_chat_provider_id = AsyncMock(side_effect=lambda umo: 'model-for-' + umo)
    for umo in ('platform:private:alice', 'platform:group:room'):
        assert await client.evaluate(state={}, questions=QUESTIONS, umo=umo)
        assert host.context.llm_generate.call_args.kwargs['chat_provider_id'] == 'model-for-' + umo
    assert [c.args for c in host.context.get_current_chat_provider_id.await_args_list] == [
        ('platform:private:alice',), ('platform:group:room',)]
    host.jev.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['provider', 'settings', 'config', 'closed'])
async def test_replaced_model_or_configuration_cannot_return_a_decision(mutation):
    client, host, provider = client_fixture()

    async def generate(**kwargs):
        if mutation == 'provider':
            host.context.get_provider_by_id = lambda _: NS(model_name='new', text_chat=AsyncMock())
        elif mutation == 'settings':
            provider.provider_config['model'] = 'replacement'
        elif mutation == 'config':
            host._runtime_config = replace(host._runtime_config, decision_backend='jev')
        else:
            await client.close()
        return NS(role='assistant', completion_text=json.dumps(answers_for(QUESTIONS)))

    host.context.llm_generate.side_effect = generate
    assert await client.evaluate(state={}, questions=QUESTIONS, umo='room') is None
    assert not client.snapshot()['available']
    host.jev.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_while_queued_prevents_the_old_call_from_starting():
    client, host, _ = client_fixture()
    budget = ProviderBudget(capacity=2)
    host.context._chat_dynamics_provider_budget = budget
    async with budget.slot('judge', 'routing'), budget.slot('judge', 'routing'):
        task = asyncio.create_task(client.evaluate(state={}, questions=QUESTIONS, umo='room'))
        for _ in range(10):
            await asyncio.sleep(0)
            if budget.waiters:
                break
        assert budget.waiters
        host._runtime_config = replace(host._runtime_config, decision_backend='jev')
    assert await task is None
    host.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_and_cancellation_remain_bounded_without_jev_retry():
    async def slow(kwargs):
        await asyncio.sleep(10)

    client, host, _ = client_fixture(slow)
    assert await client.evaluate(state={}, questions=QUESTIONS, umo='room', timeout=.01) is None
    assert client.snapshot()['detail'] == 'timeout'
    assert client.snapshot()['failures'] == 1
    task = asyncio.create_task(client.evaluate(state={}, questions=QUESTIONS, umo='room'))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    host.jev.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_provider_and_oversized_input_never_start_a_model_call():
    client, host, _ = client_fixture()
    host.context.get_provider_by_id = lambda _: NS(rerank=AsyncMock())
    assert await client.evaluate(state={}, questions=QUESTIONS, umo='room') is None
    assert await client.evaluate(state={'text': 'x' * 30000}, questions=QUESTIONS, umo='room') is None
    host.context.llm_generate.assert_not_awaited()
    host.jev.evaluate.assert_not_awaited()


def install_llm(p, **preferences):
    p.config.update(participation_backend='llm', decision_llm_provider='chat-judge', topic_reranker_enabled=False)
    p._sync_runtime_from_config()
    provider = NS(model_name='judge', text_chat=AsyncMock(), provider_config={'id': 'chat-judge'})
    p.context.get_provider_by_id = lambda _: provider

    async def generate(**kwargs):
        question_payload = json.loads(kwargs['prompt'])['questions']
        return NS(role='assistant', completion_text=json.dumps(answers_for(question_payload, **preferences)))

    p.context.llm_generate = AsyncMock(side_effect=generate)
    return p.context.llm_generate


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,action,expected_calls,expected_reply', [
    ('at', 'ignore', 0, True), ('quote_bot', 'reply', 1, True), ('quote_bot', 'ignore', 1, False),
])
async def test_actual_ingress_uses_llm_while_preserving_wake_rules(jev_plugin, kind, action, expected_calls, expected_reply):
    p, bridge = jev_plugin
    calls = install_llm(p, action=action)
    event = quoted_event(kind)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert calls.await_count == expected_calls
        assert not p.jev.calls
        assert bool(event.replies_sent) == bool(bridge.requests) == expected_reply
        if expected_calls:
            runtime = p._sessions[event.unified_msg_origin]
            assert runtime.model_diagnostic['backend'] == 'llm'
            assert runtime.model_diagnostic['reason_code'].startswith('llm_')
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_llm_failure_keeps_ambient_silent_and_does_not_call_jev(jev_plugin):
    p, bridge = jev_plugin
    calls = install_llm(p)
    calls.return_value = NS(role='assistant', completion_text='{"action":"reply"}')
    calls.side_effect = None
    try:
        event = quoted_event('quote_bot')
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not bridge.requests and not event.replies_sent and not p.jev.calls
        assert p.llm_decision.snapshot()['detail'] == 'invalid_decision_output'
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_waiting_for_input_then_complete_works_through_llm(completion_plugin):
    p = completion_plugin
    calls = install_llm(p, completion='wait')
    first = MockEvent('先等我说完，我的问题是', message_id='first', is_at_or_wake_command=True)
    second = MockEvent('程序提示权限不足，怎么解决', message_id='second')
    try:
        await p.on_group_message(first)
        await tick(p)
        assert calls.await_count == 1 and not first.replies_sent
        assert p.debounce.get_pending_count() == 1 and not p.jev.calls
        install_llm(p, completion='complete')
        await p.on_group_message(second)
        await tick(p)
        assert second.replies_sent and not first.replies_sent and not p.jev.calls
        assert not p.debounce.is_active(first.unified_msg_origin, first.sender_id)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_backend_switch_preserves_both_selections_and_status(jev_plugin, offline_web_responses):
    p, _ = jev_plugin
    try:
        install_llm(p)
        p.jev.is_systemone_router = True
        chat = NS(text_chat=AsyncMock())
        native = NS(is_systemone_provider=True, get_model=lambda: 'jev')
        p.context.get_provider_by_id = lambda pid: native if pid == 'native' else chat
        p.config['decision_provider'] = 'native'
        await p.save_config_values({'participation_backend': 'llm', 'decision_llm_provider': 'chat-judge'})
        status = (await DecisionStatusWebAPI(p).status())['data']
        assert status['backend'] == 'llm' and status['provider_id'] == 'chat-judge'
        assert jev_snapshot(p)['backend'] == 'llm' and jev_snapshot(p)['enabled']
        await p.save_config_values({'participation_backend': 'jev'})
        assert p._runtime_config.decision_provider_id == 'native'
        assert p._runtime_config.decision_llm_provider == 'chat-judge'
        assert (await DecisionStatusWebAPI(p).status())['data']['backend'] == 'jev'
        p.context.get_provider_by_id = lambda _: native
        with pytest.raises(ValueError, match='普通聊天模型'):
            await p.save_config_values({'participation_backend': 'llm'})
        assert p._runtime_config.decision_backend == 'jev'
    finally:
        await p.terminate()


def test_default_and_retired_keys_keep_native_decision_selected():
    cfg, _ = parse_runtime_config({'decision_backend': 'llm', 'decision_mode': 'legacy'})
    assert cfg.decision_backend == 'jev' and cfg.decision_mode == 'persona_model'
    cfg, _ = parse_runtime_config({'participation_backend': 'llm'})
    assert cfg.decision_backend == 'llm' and cfg.decision_llm_provider == ''


@pytest.mark.asyncio
async def test_rule_attribution_remains_independent_from_llm_participation(jev_plugin):
    p, _ = jev_plugin
    p.config['rule_topic_routing_enabled'] = True
    calls = install_llm(p)
    try:
        event = MockEvent('有人知道机箱散热风扇怎么安装吗', message_id='ordinary')
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert calls.await_count == 1 and not p.jev.calls
        questions = json.loads(calls.call_args.kwargs['prompt'])['questions']
        assert 'action' in questions and 'recipient' not in questions and 'topic' not in questions
        assert not any(key.startswith('addressee_') for key in questions)
        assert p._sessions[event.unified_msg_origin].jev_decision['recipient_source'] == 'rules'
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['switch', 'stop'])
async def test_switch_or_member_stop_cancels_inflight_llm_and_prevents_delivery(jev_plugin, operation):
    p, bridge = jev_plugin
    calls = install_llm(p)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return NS(role='assistant', completion_text=json.dumps(answers_for(json.loads(kwargs['prompt'])['questions'])))

    calls.side_effect = generate
    event = quoted_event('quote_bot')
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await asyncio.wait_for(entered.wait(), 1)
        if operation == 'switch':
            p.config['participation_backend'] = 'jev'
            p._sync_runtime_from_config()
        else:
            stop = MockEvent('/dynamics_stop', sender_id=event.sender_id, group_id=event.group_id, message_id='stop')
            await p.cmd_dynamics_stop(stop)
        release.set()
        await drain(p)
        assert not event.replies_sent and not bridge.requests and not p.jev.calls
    finally:
        release.set()
        await p.terminate()
