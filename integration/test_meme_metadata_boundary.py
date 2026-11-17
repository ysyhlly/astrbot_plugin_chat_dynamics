"""Real host runner and message chains preserve the visible/media boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_persona_agent import host_fixture as host_fixture
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.pacer import PacingShaper
from astrbot_plugin_chat_dynamics.core.persona_engine import delivery_fragments
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult, chain_plain_text
from astrbot_plugin_visual_media.core.tools.joint_reply import JointMemePlan


CAPTION = "晚安喽，被窝盖好。"
NOTE = '[我发送的表情包资料：{"id":2,"desc":"白发少女举牌温柔道晚安"}]'


def test_plain_text_and_partial_receipts_do_not_include_metadata():
    chain = MessageChain([Plain(CAPTION), Image(file="/offline.png")])
    chain._cd_delivery_text_v1 = CAPTION + "\n" + NOTE
    receipt = SendResult(True, message_id="sent")
    assert chain_plain_text(chain) == CAPTION
    assert receipt.delivered_text(chain, media_placeholder=False) == CAPTION
    assert receipt.delivered_text(chain) == CAPTION + "\n" + NOTE
    partial = SendResult(True, parts=((0, ("text-sent",)),))
    assert partial.delivered_text(chain) == CAPTION
    assert delivery_fragments([chain], CAPTION, PacingShaper()) == [chain]


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_native_generation_and_commit_store_only_clean_prose(host_fixture, monkeypatch, structured):
    from astrbot.core.pipeline import context_utils

    ctx, event, calls = host_fixture
    fake = SimpleNamespace(
        plugin_config=SimpleNamespace(llm_joint_reply=True, auto_send_meme=True),
        is_send_enabled_for_event=lambda event: True,
        meme_selector=SimpleNamespace(is_path_allowed_for_event=lambda path, event: True),
    )

    async def attach_plan(agent_event, event_type, request):
        agent_event._stealer_joint_meme_plan_v1 = JointMemePlan(fake, agent_event, [{"path": "/offline.png"}] * 3)
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", attach_plan)
    raw = CAPTION + "\\\n" + NOTE
    if structured:
        raw = json.dumps({"reply_mode": "text", "text": raw, "meme_id": None})
    ctx.provider.text_chat = AsyncMock(return_value=LLMResponse(role="assistant", completion_text=raw))
    bridge = AstrBotAgentBridge(ctx)
    out = await bridge.generate(event, (event,), "晚安喽", await bridge.snapshot(event), "provider")
    assert out.text == CAPTION and not out.chains
    assert out.reply_mode == "text"
    assert delivery_fragments(out.chains, out.text, PacingShaper()) == [CAPTION]
    assert await bridge.commit(event, out, CAPTION)
    assert ctx.saved[-1][-1]["content"] == CAPTION
    assert "表情包资料" not in json.dumps(ctx.saved[-1], ensure_ascii=False)
    assert not event.sent
