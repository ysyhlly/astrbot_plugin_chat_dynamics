import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.platform_bridge import build_plain_chain
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent, At


async def delivered_turn(plugin, bridge, *, sender='alice', message='m1'):
    original_generate = bridge.generate
    channels = []

    async def capture(*args, **kwargs):
        channels.append(kwargs['deferred_reply_media'])
        return await original_generate(*args, **kwargs)

    bridge.generate = capture
    event = MockEvent('帮我看看', sender_id=sender, message_id=message, components=[At('bot_42')])
    await plugin.on_group_message(event)
    await flush(plugin, event)
    await drain(plugin)
    bridge.generate = original_generate
    return event, channels[0]


@pytest.mark.asyncio
async def test_delayed_media_uses_original_event_once_after_primary_send(jev_plugin):
    p, bridge = jev_plugin
    try:
        event, channel = await delivered_turn(p, bridge)
        assert event.replies_sent and await channel.wait_ready()
        assert channel.reply_text == event.replies_sent[0]
        effects = []

        async def operation(original):
            assert original is event
            assert p._owned_send_events
            effects.append('image')
            return 'qq_sticker'

        assert await channel.send(build_plain_chain('[测试表情]'), operation)
        assert not await channel.send(build_plain_chain('[重复]'), operation)
        assert effects == ['image']
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['same_user', 'reset', 'new_bot_reply', 'expired'])
async def test_stale_parent_never_sends_deferred_media(jev_plugin, reason):
    p, bridge = jev_plugin
    effects = []
    try:
        event, channel = await delivered_turn(p, bridge)
        runtime = p._sessions[event.unified_msg_origin]
        if reason == 'same_user':
            runtime.user_revisions[event.sender_id] += 1
        elif reason == 'reset':
            runtime.epoch += 1
        elif reason == 'new_bot_reply':
            runtime.last_bot_node = NS(msg_id='newer-reply')
        else:
            channel._ready_at -= 46

        async def operation(original):
            effects.append(True)
            return 'sent'

        assert not await channel.send(build_plain_chain('[测试表情]'), operation)
        assert not effects
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_primary_failure_does_not_authorize_meme_after_fallback(jev_plugin):
    p, bridge = jev_plugin
    bridge.response = ''
    try:
        event, channel = await delivered_turn(p, bridge)
        assert event.replies_sent  # mandatory failure receipt is not a normal reply
        assert not await channel.wait_ready()
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_media_failure_is_not_counted_or_retried(jev_plugin):
    p, bridge = jev_plugin
    try:
        _, channel = await delivered_turn(p, bridge)
        called = []

        async def operation(_):
            called.append(True)
            raise OSError('send uncertain')

        assert not await channel.send(build_plain_chain('[测试表情]'), operation)
        assert not await channel.send(build_plain_chain('[测试表情]'), operation)
        assert called == [True]
        assert p._metrics.get('deferred_media_sent', 0) == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_new_message_during_source_validation_prevents_effect(jev_plugin):
    p, bridge = jev_plugin
    try:
        event, channel = await delivered_turn(p, bridge)
        runtime = p._sessions[event.unified_msg_origin]
        checks, effects = [], []

        async def validate():
            checks.append(True)
            if len(checks) == 2:
                runtime.user_revisions[event.sender_id] += 1
            return True

        async def operation(_):
            effects.append(True)
            return 'sent'

        channel._source_check = validate
        assert not await channel.send(build_plain_chain('[测试表情]'), operation)
        assert not effects
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_stealer_other_speaker_does_not_cancel_owned_pending_task():
    from astrbot_plugin_stealer.core.events.meme_sender_engine import MemeSenderEngine
    engine = MemeSenderEngine(NS())
    source = MockEvent('hello', sender_id='alice')
    source._cd_deferred_reply_media_v1 = object()
    task = asyncio.create_task(asyncio.sleep(30))
    try:
        engine.schedule_auto_emoji_task(source, task)
        assert not engine.cancel_pending_auto_emoji(MockEvent('插话', sender_id='bob'))
        assert not task.cancelling()
        assert engine.cancel_pending_auto_emoji(MockEvent('补充', sender_id='alice'))
        assert task.cancelling()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stealer_standard_fallback_leaves_capture_event(jev_plugin, monkeypatch):
    from astrbot_plugin_stealer.core.search.meme_smart_select_service import MemeSmartSelectService
    from astrbot_plugin_stealer.core.search import meme_smart_select_service as module
    p, bridge = jev_plugin
    try:
        event, channel = await delivered_turn(p, bridge)
        clone = copy.copy(event)
        captured = []

        async def capture(_):
            captured.append(True)

        clone.send = capture
        clone._stealer_passive_delivery_v1 = channel
        selector = MemeSmartSelectService(NS(send_meme_as_gif=True), None, None, None)

        async def no_sticker(*args, **kwargs):
            return False

        async def encode(_):
            return 'R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='

        monkeypatch.setattr(module, 'send_qq_image_as_sticker', no_sticker)
        monkeypatch.setattr(selector, '_try_send_telegram_sticker', no_sticker)
        monkeypatch.setattr(selector, '_encode_emoji', encode)
        assert await selector.send_emoji_message(clone, '/synthetic.gif') == 'chat_dynamics_deferred'
        assert not captured
        assert len(event.replies_sent) == 2
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_active_meme_tool_does_not_wait_for_primary_delivery(monkeypatch):
    from astrbot_plugin_stealer.core.search.meme_smart_select_service import MemeSmartSelectService
    from astrbot_plugin_stealer.core.search import meme_smart_select_service as module
    event = MockEvent('主动发一张表情')
    event._cd_deferred_reply_media_v1 = object()
    event._chat_dynamics_owned_request = True
    selector = MemeSmartSelectService(NS(send_meme_as_gif=True), None, None, None)

    async def no_telegram(*args, **kwargs):
        return False

    async def sticker(target, *args, **kwargs):
        assert target is event
        return True

    monkeypatch.setattr(selector, '_try_send_telegram_sticker', no_telegram)
    monkeypatch.setattr(module, 'send_qq_image_as_sticker', sticker)
    assert await selector.send_emoji_message(event, '/synthetic.gif') == 'qq_sticker'


@pytest.mark.asyncio
async def test_qq_timeout_is_not_treated_as_a_safe_image_fallback(tmp_path):
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    from astrbot_plugin_stealer.core.events.emoji_delivery import send_qq_image_as_sticker
    event = AiocqhttpMessageEvent.__new__(AiocqhttpMessageEvent)
    event.message_obj = NS(raw_message={'group_id': 1})
    attempts = []

    async def parse(_):
        return [{'type': 'image', 'data': {}}]

    async def send(*args):
        attempts.append(True)
        raise TimeoutError('unknown transport outcome')

    event._parse_onebot_json = parse
    event.bot = NS(send=send)
    path = tmp_path / 'image.gif'
    path.write_bytes(b'GIF89a')
    with pytest.raises(TimeoutError):
        await send_qq_image_as_sticker(event, str(path), plugin=NS(send_meme_as_qq_sticker=True, send_meme_as_gif=False))
    assert attempts == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered', [True, False])
async def test_passive_selection_waits_for_successful_primary(delivered):
    from astrbot_plugin_stealer.core.events.meme_sender_engine import MemeSenderEngine
    ready = asyncio.Event()
    selected = []

    class Channel:
        async def wait_ready(self):
            await ready.wait()
            return delivered

        async def is_current(self):
            return delivered

    channel = Channel()

    async def select(event, *args, **kwargs):
        assert event._stealer_passive_delivery_v1 is channel
        selected.append(True)
        return True

    plugin = NS(plugin_config=NS(enable_jev=False), meme_selector=NS(try_send_emoji=select),
                enable_natural_emotion_analysis=False, meme_send_delay=0, meme_send_char_delay=0)
    engine = MemeSenderEngine(plugin)
    event = MockEvent('用户原文')
    event._chat_dynamics_owned_request = True
    event._cd_deferred_reply_media_v1 = channel
    engine.emoji_turn_state(event).set_llm_reply('模型回复')
    task = asyncio.create_task(engine.async_analyze_and_send_emoji(event, '模型回复', []))
    try:
        await asyncio.sleep(0)
        assert not selected and not task.done()
        ready.set()
        await asyncio.wait_for(task, 2)
        assert bool(selected) is delivered
        assert not hasattr(event, '_stealer_passive_delivery_v1')
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_member_stop_cancels_an_in_progress_media_effect(jev_plugin):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []
    task = None
    try:
        event, channel = await delivered_turn(p, bridge)

        async def operation(_):
            entered.set()
            await release.wait()
            effects.append(True)
            return 'sent'

        task = asyncio.create_task(channel.send(build_plain_chain('[测试表情]'), operation))
        await asyncio.wait_for(entered.wait(), 2)
        await p.cmd_dynamics_stop(MockEvent('/dynamics_stop', sender_id=event.sender_id, message_id='stop'))
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not effects and task.cancelled()
    finally:
        release.set()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await p.terminate()
