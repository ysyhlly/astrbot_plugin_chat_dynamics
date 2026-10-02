"""Real ingress, Jev, owned generation and delivery across strong wake races."""
import asyncio
from dataclasses import replace

import pytest

from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import answers, jev_plugin as _jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, Reply

jev_plugin = _jev_plugin


def low_confidence_answers():
    payload = answers()
    payload['action']['confidence'] = 0.2
    return payload


def quoted_event(kind, *, message_id='request'):
    quote = Reply('evicted-quote')
    quote.sender_id = 'bot_42' if kind == 'quote_bot' else 'peer'
    components = [At('bot_42')] if kind in {'at', 'at_quote_peer'} else []
    if kind != 'at':
        components.append(quote)
    return MockEvent('请帮我解释这个问题', message_id=message_id, components=components)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['at', 'quote_bot', 'at_quote_peer'])
@pytest.mark.parametrize('decline', ['other_recipient', 'low_value_chatter', 'join'])
async def test_confirmed_wake_survives_participation_misclassification(jev_plugin, kind, decline):
    p, bridge = jev_plugin
    p.jev.payload = (answers(join={'type': 'noul', 'noul': 0.1}) if decline == 'join'
                     else answers(action='ignore', state='observing', reason=decline))
    event = quoted_event(kind)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        if kind == 'quote_bot':
            assert p.jev.calls[0]['state']['conversation']['wake_kind'] == 'quote'
        else:
            assert not p.jev.calls
        assert len(bridge.requests) == 1
        assert event.replies_sent
        node = p._sessions[event.unified_msg_origin].dag.get_node(event.message_id)
        assert node.metadata['outcome']['final_outcome'] == 'delivered'
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,reason', [('quote_peer', 'other_recipient'), ('at_quote_peer', 'boundary_or_sensitive')])
async def test_human_quote_stays_ambient_and_real_at_always_replies(jev_plugin, kind, reason):
    p, bridge = jev_plugin
    p.jev.payload = answers(action='ignore', state='observing', reason=reason)
    event = quoted_event(kind)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bool(bridge.requests) == bool(event.replies_sent) == (kind == 'at_quote_peer')
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['success', 'timeout', 'new_request', 'stop'])
async def test_slow_explicit_request_is_not_lost_to_same_author_chatter(jev_plugin, ending):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_first():
        if len(bridge.requests) == 1:
            entered.set()
            await release.wait()
    bridge.before_reply = slow_first
    first = quoted_event('at_quote_peer', message_id='first')
    try:
        # The production incident entered generation via low-confidence fallback.
        p.jev.payload = low_confidence_answers()
        if ending == 'timeout':
            p._runtime_config = replace(p._runtime_config, tool_agent_timeout=0.1)
        await p.on_group_message(first)
        await flush(p, first)
        await asyncio.wait_for(entered.wait(), 2)
        assert bridge.requests[0][0]['response_plan']['reason_code'] == 'at_mandatory'
        runtime = p._sessions[first.unified_msg_origin]
        revision = runtime.user_revisions[first.sender_id]
        # The author's next message targets a peer, not the bot or the active request.
        followup = quoted_event('quote_peer', message_id='chatter')
        p.jev.payload = low_confidence_answers()
        await p.on_group_message(followup)
        await flush(p, followup)
        assert runtime.user_revisions[first.sender_id] == revision
        if ending == 'new_request':
            newer = quoted_event('at', message_id='newer')
            await p.on_group_message(newer)
            await flush(p, newer)
            assert runtime.user_revisions[first.sender_id] == revision
        elif ending == 'stop':
            stop = MockEvent('/dynamics_stop', message_id='stop', is_admin_user=False)
            await p.cmd_dynamics_stop(stop)
            assert runtime.user_revisions[first.sender_id] > revision
        if ending != 'timeout':
            release.set()
        await drain(p)
        outcome = runtime.dag.get_node('first').metadata['outcome']
        if ending == 'success':
            assert first.replies_sent and not followup.replies_sent
            assert len(bridge.requests) == 1
            assert outcome['final_outcome'] == 'delivered'
        elif ending == 'timeout':
            assert first.replies_sent and not followup.replies_sent
            assert outcome['final_outcome'] == 'delivered'
            assert outcome['suppression_reason'] == 'at_reply_fallback'
            assert '没能生成完整回复' in first.replies_sent[0]
            assert len(first.replies_sent) == 1
        else:
            if ending == 'new_request':
                assert first.replies_sent and newer.replies_sent
            else:
                assert not first.replies_sent
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_queued_explicit_request_survives_its_authors_chatter(jev_plugin):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    async def block_head():
        if len(bridge.requests) == 1:
            entered.set()
            await release.wait()
    bridge.before_reply = block_head
    head = MockEvent('另一人的请求', sender_id='other', message_id='head', is_at_or_wake_command=True)
    queued = quoted_event('at_quote_peer', message_id='queued')
    chatter = quoted_event('quote_peer', message_id='chatter')
    try:
        await p.on_group_message(head)
        await flush(p, head)
        await asyncio.wait_for(entered.wait(), 2)
        await p.on_group_message(queued)
        await flush(p, queued)
        runtime = p._sessions[queued.unified_msg_origin]
        revision = runtime.user_revisions[queued.sender_id]
        p.jev.payload = low_confidence_answers()
        await p.on_group_message(chatter)
        await flush(p, chatter)
        assert runtime.user_revisions[queued.sender_id] == revision
        release.set()
        await drain(p)
        assert head.replies_sent and queued.replies_sent and not chatter.replies_sent
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['at', 'quote_bot'])
@pytest.mark.parametrize('ending', ['success', 'new_request', 'stop', 'reset', 'cancel', 'unload'])
async def test_waiting_wake_survives_chatter_but_respects_cancellation(jev_plugin, kind, ending):
    p, bridge = jev_plugin
    entered, release, waiting_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def block_head():
        if len(bridge.requests) == 1:
            entered.set()
            await release.wait()

    bridge.before_reply = block_head
    head = MockEvent('另一人的请求', sender_id='head-user', message_id='head', components=[At('bot_42')])
    waiting = quoted_event(kind, message_id='waiting')
    chatter = quoted_event('quote_peer', message_id='chatter')
    waiters = []
    terminated = False
    try:
        await p.on_group_message(head)
        await flush(p, head)
        await asyncio.wait_for(entered.wait(), 2)
        runtime = p._sessions[head.unified_msg_origin]
        # Saturate the normal nine permits: one active request and eight queued requests.
        for index in range(8):
            queued = MockEvent('请回复这个请求', sender_id=f'queued-user-{index}',
                               message_id=f'queued-{index}', components=[At('bot_42')])
            await p.on_group_message(queued)
            await flush(p, queued)
        assert runtime.model_admission.locked()
        acquire = runtime.model_admission.acquire

        async def observe_acquire():
            if runtime.model_admission.locked():
                waiting_entered.set()
            return await acquire()

        runtime.model_admission.acquire = observe_acquire
        await p.on_group_message(waiting)
        task = asyncio.create_task(flush(p, waiting))
        waiters.append(task)
        await asyncio.wait_for(waiting_entered.wait(), 2)
        assert not task.done()
        revision = runtime.user_revisions[waiting.sender_id]
        p.jev.payload = low_confidence_answers()
        await p.on_group_message(chatter)
        assert runtime.user_revisions[waiting.sender_id] == revision

        newer = None
        if ending == 'new_request':
            newer = quoted_event(kind, message_id='newer')
            waiting_entered.clear()
            await p.on_group_message(newer)
            waiters.append(asyncio.create_task(flush(p, newer)))
            await asyncio.wait_for(waiting_entered.wait(), 2)
        elif ending == 'stop':
            await p.cmd_dynamics_stop(MockEvent('/dynamics_stop', message_id='stop', is_admin_user=False))
        elif ending == 'reset':
            await p._reset_session_state_async(runtime.session_key)
        elif ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == 'unload':
            await p.terminate()
            terminated = True
        p.jev.payload = answers()
        release.set()
        await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), 3)
        await drain(p)

        expected_reply = ending == 'success' or (ending == 'new_request' and kind == 'at')
        assert bool(waiting.replies_sent) == expected_reply
        assert not chatter.replies_sent
        if newer is not None:
            assert newer.replies_sent
        assert not p.persona_engine.has_pending_explicit_request(runtime, waiting.sender_id)
        assert not runtime.model_waiting
        assert not runtime.model_queue
        assert runtime.model_admission._value == 9
    finally:
        release.set()
        for task in waiters:
            if not task.done():
                task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        if not terminated:
            await p.terminate()


@pytest.mark.asyncio
async def test_cancel_before_queue_commit_releases_acquired_admission(jev_plugin):
    p, _bridge = jev_plugin
    event = quoted_event('at')
    runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.group_id,
                                       umo=event.unified_msg_origin, bot_id=event.self_id)
    turn = TurnContext(runtime.session_key, event.sender_id, event.message_str,
                       (MessageSnapshot(event.message_id, event.sender_id, event.message_str),), (),
                       runtime.epoch, 0, p.time_service.time(), explicit=True, wake_kind='at')
    item = ModelTurn(turn, (event,), {}, False)
    task = None
    try:
        async with runtime.state_lock:
            task = asyncio.create_task(p.persona_engine.submit(runtime, item))
            await asyncio.sleep(0)
            assert runtime.model_admission._value == 8
            assert p.persona_engine.has_pending_explicit_request(runtime, event.sender_id)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not runtime.model_queue
        assert not runtime.model_waiting
        assert runtime.model_admission._value == 9
        assert not p.persona_engine.has_pending_explicit_request(runtime, event.sender_id)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await p.terminate()
