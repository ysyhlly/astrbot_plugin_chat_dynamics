import pytest
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.jev_decision import decision_from_answers
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_narrative_coordination import turn
from .test_persona_model import flush, drain
from .test_plugin_lifecycle import MockEvent, Reply


def test_missing_settings_preserve_old_thresholds_and_supplement_policy():
    cfg, _ = parse_runtime_config({})
    assert (cfg.quote_reply_threshold, cfg.name_reply_threshold) == (35, 70)
    result = decision_from_answers(turn('supplement'), answers(
        recipient={'type': 'choice', 'choice': 'bot', 'confidence': .69}),
        quote_recipient_floor=0, name_recipient_floor=0)
    assert result.action == 'ignore'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,confidence,reply', [('quote', .4499, False), ('quote', .45, True),
                                                 ('name', .4499, False), ('name', .45, True)])
async def test_saved_threshold_reaches_actual_decision(jev_plugin, kind, confidence, reply):
    p, bridge = jev_plugin
    try:
        panel = await p.save_config_values({'quote_reply_threshold': 45, 'name_reply_threshold': 45,
                                           'jev_min_confidence': .6, 'bot_names': ['安安']})
        assert panel['effective']['quote_reply_threshold'] == 45
        assert panel['effective']['name_reply_threshold'] == 45
        quote = Reply('old-bot')
        quote.sender_id = 'bot_42'
        event = MockEvent('你怎么看' if kind == 'quote' else '安安，你怎么看',
                          components=[quote] if kind == 'quote' else [], message_id='threshold-test')
        p.jev.payload = answers(recipient={'type': 'choice', 'choice': 'bot', 'confidence': confidence})
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bool(event.replies_sent) is reply
        assert bool(bridge.requests) is reply
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['quote_reply_threshold', 'name_reply_threshold'])
@pytest.mark.parametrize('value', [-1, 101, True, float('nan')])
async def test_invalid_threshold_does_not_write_config(jev_plugin, key, value):
    p, _ = jev_plugin
    try:
        before = dict(p.config)
        with pytest.raises(ValueError):
            await p.save_config_values({key: value})
        assert dict(p.config) == before
    finally:
        await p.terminate()


@pytest.mark.parametrize('quote_floor,name_floor,quote_reply,name_reply', [(.45, .8, True, False), (.8, .45, False, True)])
def test_two_controls_are_independent(quote_floor, name_floor, quote_reply, name_reply):
    payload = answers(recipient={'type': 'choice', 'choice': 'bot', 'confidence': .5})
    for kind, expected in [('quote', quote_reply), ('name', name_reply)]:
        result = decision_from_answers(turn(kind), payload, quote_recipient_floor=quote_floor,
                                       name_recipient_floor=name_floor)
        assert (result.action == 'reply') is expected
