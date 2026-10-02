"""@ must reply; quote/name wake only requires Jev to identify the bot."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.platform_bridge import parse_group_event
from .test_jev_decision_layer import answers, jev_plugin as _jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, Reply

jev_plugin = _jev_plugin


def event_for(kind):
    quote = Reply('old-bot')
    quote.sender_id = 'bot_42'
    if kind == 'at':
        return MockEvent('帮我看看', components=[At('bot_42')])
    if kind == 'quote':
        return MockEvent('你怎么看', components=[quote])
    if kind == 'platform':
        return MockEvent('帮我看看', is_at_or_wake_command=True)
    return MockEvent('小助手，你怎么看' if kind == 'name' else '小助手昨天回复过这件事')


@pytest.mark.asyncio
@pytest.mark.parametrize('recipient,confidence,reply', [('bot', 0.35, True), ('bot', 0.34, False), ('other', 0.99, False), ('unclear', 0.99, False)])
@pytest.mark.parametrize('kind', ['quote', 'name', 'platform'])
async def test_soft_wake_only_needs_a_bot_recipient(jev_plugin, recipient, confidence, reply, kind):
    p, bridge = jev_plugin
    await p.save_config_values({'bot_names': ['小助手']})
    p.jev.payload = answers(action='ignore', state='observing', reason='low_value_chatter',
                            join={'type': 'noul', 'noul': 0.01},
                            recipient={'type': 'choice', 'choice': recipient, 'confidence': confidence})
    event = event_for(kind)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(p.jev.calls) == 1
        assert 'recipient' in p.jev.calls[0]['questions']
        assert p.jev.calls[0]['state']['conversation']['reply_required'] is False
        assert bool(event.replies_sent) == bool(bridge.requests) == reply
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['小助手下午好', '我想问小助手一个问题'])
async def test_full_nickname_wake_is_not_limited_to_local_vocative_phrases(jev_plugin, text):
    p, bridge = jev_plugin
    p.jev.payload = answers(action='ignore', join={'type': 'noul', 'noul': 0.01})
    event = MockEvent(text)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert p.jev.calls[0]['state']['conversation']['wake_kind'] == 'name'
        assert len(bridge.requests) == len(event.replies_sent) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_name_mentioned_in_third_person_is_not_mandatory(jev_plugin):
    p, bridge = jev_plugin
    await p.save_config_values({'bot_names': ['小助手']})
    p.jev.payload = answers(recipient={'type': 'choice', 'choice': 'other', 'confidence': 0.9})
    event = event_for('subject')
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(p.jev.calls) == 1
        assert p.jev.calls[0]['state']['conversation']['wake_kind'] == 'name'
        assert not event.replies_sent and not bridge.requests
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['', '你好', '随便说一句'])
async def test_at_replies_even_without_a_question_or_message_body(jev_plugin, text):
    p, bridge = jev_plugin
    event = MockEvent(text, components=[At('bot_42')])
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not p.jev.calls
        assert len(event.replies_sent) == len(bridge.requests) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unavailable', 'slow', 'ignore_boundary', 'recipient_other'])
async def test_at_never_waits_for_or_is_vetoed_by_jev(jev_plugin, failure):
    p, bridge = jev_plugin
    p.jev.payload = None if failure == 'unavailable' else answers(action='ignore', reason='boundary_or_sensitive',
                         recipient={'type': 'choice', 'choice': 'other', 'confidence': 1.0})
    p.jev.delay = 10 if failure == 'slow' else 0
    event = event_for('at')
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not p.jev.calls
        assert event.replies_sent and len(bridge.requests) == 1
        assert bridge.requests[0][0]['response_plan']['reason_code'] == 'at_mandatory'
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('restriction', ['cool', 'sleep', 'gate_error', 'media_denied'])
async def test_at_replies_under_quiet_gates_without_granting_media_access(jev_plugin, restriction):
    p, bridge = jev_plugin
    event = event_for('at')
    runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.group_id,
                                        umo=event.unified_msg_origin, bot_id=event.self_id)
    if restriction == 'cool':
        p.arbiter.trigger_cooling(runtime.session_key, duration_seconds=120, current_time=p.time_service.time())
    elif restriction == 'sleep':
        await p.save_config_values({'daily_rhythm_enabled': True, 'rhythm_force_sleep': True, 'rhythm_allow_wake': False})
    else:
        evaluate = p.decision_gate.evaluate
        def gate(**kwargs):
            if restriction == 'gate_error':
                raise RuntimeError('gate error')
            return replace(evaluate(**kwargs), should_speak=False, request_understand=True, reason_code='media_privacy')
        p.decision_gate.evaluate = gate
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(event.replies_sent) == 1
        if restriction in {'gate_error', 'media_denied'}:
            assert runtime.request_media_understand is False
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exception', 'empty', 'timeout', 'snapshot'])
async def test_at_generation_failure_gets_exactly_one_fallback(jev_plugin, failure):
    p, bridge = jev_plugin
    event = event_for('at')
    if failure == 'empty':
        bridge.response = ''
    elif failure == 'timeout':
        p._runtime_config = replace(p._runtime_config, reply_timeout=0.05, tool_agent_timeout=5)
        async def slow():
            await asyncio.sleep(10)
        bridge.before_reply = slow
    else:
        async def failed(*args, **kwargs):
            raise RuntimeError('generation error')
        if failure == 'snapshot':
            bridge.snapshot = failed
        else:
            bridge.generate = failed
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(event.replies_sent) == 1
        assert '没能生成完整回复' in event.replies_sent[0]
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.model_diagnostic['reason_code'] == 'at_reply_fallback'
        assert runtime.dag.get_node(event.message_id).metadata['outcome']['delivered'] is True
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['at', 'quote', 'name'])
async def test_wake_in_shadow_mode_never_sends(jev_plugin, kind):
    p, bridge = jev_plugin
    await p.save_config_values({'shadow_mode': True})
    event = event_for(kind)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not event.replies_sent and not bridge.requests
        assert bool(p.jev.calls) == (kind != 'at')
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['reset', 'unload'])
async def test_at_cancelled_by_reset_or_unload_does_not_send_fallback(jev_plugin, ending):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked():
        entered.set()
        await release.wait()
    bridge.before_reply = blocked
    event = event_for('at')
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await asyncio.wait_for(entered.wait(), 1.0)
        if ending == 'reset':
            await p._reset_session_state_async(event.unified_msg_origin)
        else:
            await p.terminate()
        release.set()
        await drain(p)
        assert not event.replies_sent
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('fallback_succeeds', [True, False])
async def test_at_failed_delivery_attempts_one_fallback_and_records_result(jev_plugin, fallback_succeeds):
    p, bridge = jev_plugin
    attempts = []
    async def send(runtime, event, text, **kwargs):
        attempts.append(text)
        success = fallback_succeeds and len(attempts) == 2
        return SimpleNamespace(success=success, message_id='fallback' if success else None)
    p._send_owned = send
    event = event_for('at')
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(attempts) == 2
        assert '没能生成完整回复' in attempts[-1]
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.dag.get_node(event.message_id).metadata['outcome']['delivered'] == fallback_succeeds
        assert runtime.model_diagnostic['reason_code'] == ('at_reply_fallback' if fallback_succeeds else 'at_fallback_send_failed')
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_at_partial_delivery_does_not_send_duplicate_fallback(jev_plugin):
    p, bridge = jev_plugin
    p.pacer.persona_fragments = lambda text: ['第一段', '第二段']
    p.time_service.sleep = lambda seconds: asyncio.sleep(0)
    attempts = []
    async def send(runtime, event, text, **kwargs):
        attempts.append(text)
        return SimpleNamespace(success=len(attempts) == 1, message_id='sent' if len(attempts) == 1 else None)
    p._send_owned = send
    event = event_for('at')
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert attempts == ['第一段', '第二段']
        assert p._sessions[event.unified_msg_origin].dag.get_node(event.message_id).metadata['outcome']['delivered']
    finally:
        await p.terminate()


def test_textual_at_and_at_all_do_not_claim_real_bot_at():
    class AtAll:
        pass
    for event in [MockEvent('@bot_42'), MockEvent('大家看看', components=[AtAll()]), MockEvent('看看', components=[At('peer')])]:
        parsed = parse_group_event(event)
        assert 'bot_42' not in parsed.platform_mentions
