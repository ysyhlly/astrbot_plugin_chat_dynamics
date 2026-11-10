"""Exercise managed reply instructions through the real SDK/provider boundary."""
import json
from types import SimpleNamespace

import pytest
from .test_persona_agent import host_fixture as host_fixture
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.narrative import REPLY_GUIDANCE, _narrative_scope_key
from astrbot_plugin_chat_dynamics.core.turn_decision import TurnContext, MessageSnapshot, TurnDecision, reply_prompt


@pytest.mark.asyncio
async def test_managed_reply_gets_one_system_guidance_and_keeps_current_request(host_fixture):
    context, event, calls = host_fixture
    bridge = AstrBotAgentBridge(context)
    persona = await bridge.snapshot(event)
    scope = {'platform': 'test', 'umo': event.unified_msg_origin, 'persona_id': 'quiet', 'instance_id': 'quiet'}
    managed = {
        'scope': scope, 'purpose': 'reply', 'effective_core_version': 'v1', 'speaker_id': 'u', 'captured_at': 100,
        'character': {'core_prompt': persona.prompt, 'group_rules': '', 'fingerprint': 'stable'},
        'snapshot': {'managed': True, 'availability': 'available', 'revision': 1, 'humanlike': {
            'preferences': [{'id': 'reading', 'text': '偏爱悬疑短篇', 'status': 'active', 'origin': 'personal'}],
            'goals': [{'id': 'chapter', 'text': '想读完下一章', 'status': 'planned', 'expires_at': 300}],
            'moods': [{'subject_id': 'u', 'text': '刚被逗笑的轻松余韵', 'updated_at': 90, 'expires_at': 300}],
            'nicknames': [{'subject_id': 'u', 'nickname': '小存档', 'status': 'active'},
                          {'subject_id': 'other', 'nickname': '不应泄漏的昵称', 'status': 'active'}],
        }},
        'host_history_snapshot': {'conversation_id': persona.conversation_id, 'snapshot_fingerprint': 'fixed',
                                  'rows': json.loads(context.conv.history)},
        'evidence_view': {'schema_version': 1, 'available': True, 'purpose': 'reply', 'card_version': 'v1',
                          'scope_key': _narrative_scope_key(scope), 'correction_revision': 0,
                          'atoms': [], 'slots': {}, 'history_projection': {}, 'revocations': []},
    }
    event._extras['_narrative_managed_v1'] = managed
    turn = TurnContext(event.unified_msg_origin, 'u', '现在预算改为800元，仍然离线',
                       (MessageSnapshot('m', 'u', '现在预算改为800元，仍然离线'),), (),
                       0, 0, 0, True, wake_kind='at')
    prompt = reply_prompt(
        turn,
        TurnDecision.fallback(turn, "at_mandatory"),
        interaction_frame={
            "session_key": turn.session_key,
            "speaker_id": turn.author,
            "trigger_message_id": "m",
            "answers": {
                "communication_intent": {"type": "choice", "choice": "task", "confidence": 0.95},
                "expression_mode": {"type": "choice", "choice": "mixed", "confidence": 0.9},
                "turn_update": {"type": "choice", "choice": "corrects_previous", "confidence": 0.95},
            },
        },
        reply_guidance=REPLY_GUIDANCE,
        character_context={},
        include_reply_guidance=False,
    )
    notifier = SimpleNamespace(evidence_current=lambda _: True)
    await bridge.generate(event, (event,), prompt, persona, 'provider', narrative_override=notifier)
    inputs = [{key: call.get(key) for key in ('prompt', 'contexts', 'system_prompt')} for call in calls]
    serialized = json.dumps(inputs, ensure_ascii=False)
    assert '现在预算改为800元' in serialized
    system = calls[0].get('system_prompt', '') + ''.join(
        row['content'] for row in calls[0].get('contexts', [])
        if row.get('role') == 'system' and isinstance(row.get('content'), str))
    assert system.count(REPLY_GUIDANCE) == 1
    assert 'reply_guidance' not in json.loads(prompt)
    assert persona.prompt in system
    for expected in ('偏爱悬疑短篇', '想读完下一章', '刚被逗笑的轻松余韵', '小存档'):
        assert expected in serialized
    assert '不应泄漏的昵称' not in serialized
    assert "interaction_understanding" in serialized and "corrects_previous" in serialized
