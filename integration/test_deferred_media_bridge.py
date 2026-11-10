"""The real host response hook receives the delivery capability, not a raw send bypass."""
import pytest
from .test_persona_agent import host_fixture as host_fixture
from astrbot.core import astr_agent_hooks
from astrbot.core.pipeline import context_utils
from astrbot.core.star.star_handler import EventType
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge


@pytest.mark.asyncio
async def test_real_response_hook_gets_deferred_delivery_and_original_user_text(host_fixture, monkeypatch):
    context, original, calls = host_fixture
    capability = object()
    seen = []

    async def hook(event, kind, *args, **kwargs):
        if kind == EventType.OnLLMResponseEvent:
            seen.append(event)
        return False

    monkeypatch.setattr(astr_agent_hooks, 'call_event_hook', hook)
    monkeypatch.setattr(context_utils, 'call_event_hook', hook)
    bridge = AstrBotAgentBridge(context)
    output = await bridge.generate(original, (original,), 'INTERNAL PLAN', await bridge.snapshot(original),
                                   'provider', deferred_reply_media=capability)
    assert calls and output.text
    assert len(seen) == 1 and seen[0] is not original
    assert seen[0]._cd_deferred_reply_media_v1 is capability
    assert seen[0]._cd_original_user_text_v1 == original.message_str
    assert seen[0]._cd_original_user_text_v1 != 'INTERNAL PLAN'
    assert not hasattr(original, '_cd_deferred_reply_media_v1') and not original.sent
