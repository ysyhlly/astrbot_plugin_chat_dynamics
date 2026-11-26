"""Real native runner with synthetic provider responses and no platform sends."""

import json
from unittest.mock import AsyncMock

import pytest

from .test_persona_agent import host_fixture as host_fixture
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.reply_failure import ModelReplyFailure


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,text", [("text", "好的。"), ("silent", "")])
async def test_native_choice_without_visual_plugin(host_fixture, mode, text):
    ctx, event, calls = host_fixture

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(
            role="assistant",
            completion_text=json.dumps(
                {
                    "reply_mode": mode,
                    "text": text,
                    "meme_id": None,
                    "narration": "安安暂时没有回应。" if mode == "silent" else "",
                }
            ),
        )

    ctx.provider.text_chat = chat
    bridge = AstrBotAgentBridge(ctx)
    partial = AsyncMock()
    out = await bridge.generate(
        event, (event,), "prompt", await bridge.snapshot(event), "provider", on_partial_text=partial
    )
    assert out.text == text and out.reply_mode == ("narration" if mode == "silent" else mode)
    assert out.requested_reply_mode == mode
    assert out.narration == ("（安安暂时没有回应。）" if mode == "silent" else "")
    assert len(calls) == 1 and not event.sent and not ctx.saved
    assert "本轮回应方式" in str(calls[0])
    assert "只有当前回合因明确 @ 机器人而触发强制回应时" in str(calls[0])
    partial.assert_not_awaited()
    assert not out.stream_prefix and out.completion_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        '{"reply_mode":"silent","text":"你好"}',
        '{"reply_mode":"unknown","text":""}',
        '{"reply_mode":"text","text":""}',
        '{"reply_mode":"meme_only","text":"","meme_id":1}',
        '{"reply_mode":"silent",',
        '{"reply_mode":"silent","text":""}',
        '{"reply_mode":"silent","text":"","narration":null}',
        '{"reply_mode":"silent","text":"","narration":"   "}',
        '{"reply_mode":"silent","text":"","narration":"（）"}',
        '{"reply_mode":"text","text":"你好","narration":"旁白"}',
        '{"reply_mode":"poke","text":""}',
    ],
)
async def test_invalid_choice_never_becomes_silence(host_fixture, completion):
    ctx, event, _ = host_fixture
    ctx.provider.text_chat = AsyncMock(return_value=LLMResponse(role="assistant", completion_text=completion))
    bridge = AstrBotAgentBridge(ctx)
    with pytest.raises(ModelReplyFailure):
        await bridge.generate(event, (event,), "prompt", await bridge.snapshot(event), "provider")
    assert not event.sent and not ctx.saved


@pytest.mark.asyncio
async def test_sent_tool_effect_cannot_be_classified_as_silence(host_fixture):
    from astrbot.core.tools.message_tools import SendMessageToUserTool

    ctx, event, calls = host_fixture
    ctx.tools["send_message_to_user"] = SendMessageToUserTool()
    ctx.persona_manager.personas_v3[0]["tools"] = ["send_message_to_user"]
    ctx.send_message = AsyncMock(return_value=True)

    async def chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["send_message_to_user"],
                tools_call_args=[{"messages": [{"type": "plain", "text": "已回答"}]}],
                tools_call_ids=["offline"],
            )
        return LLMResponse(role="assistant", completion_text='{"reply_mode":"silent","text":"","narration":"角色没有再说话。"}')

    ctx.provider.text_chat = chat
    bridge = AstrBotAgentBridge(ctx)
    out = await bridge.generate(event, (event,), "prompt", await bridge.snapshot(event), "provider")
    assert out.reply_mode != "silent"
    assert out.reply_reason == "tool_effect_blocks_silence"
    assert len(calls) == 2
    ctx.send_message.assert_awaited_once()
    assert not event.sent and not ctx.saved


@pytest.mark.asyncio
async def test_silence_with_legacy_visual_plan(host_fixture, monkeypatch):
    from types import SimpleNamespace
    from astrbot.core.pipeline import context_utils

    ctx, event, _ = host_fixture

    async def legacy_hook(event, event_type, request):
        event._stealer_joint_meme_plan_v1 = SimpleNamespace(candidates=[], resolve=lambda raw: ("", []))
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", legacy_hook)
    ctx.provider.text_chat = AsyncMock(
        return_value=LLMResponse(role="assistant", completion_text='{"reply_mode":"silent","text":"","narration":"角色没有说话。"}')
    )
    bridge = AstrBotAgentBridge(ctx)
    out = await bridge.generate(event, (event,), "prompt", await bridge.snapshot(event), "provider")
    assert out.reply_mode == "narration" and out.reply_reason == "model_silent_narration"
    assert out.narration == "（角色没有说话。）"


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_native_poke_is_capability_gated_without_sending(host_fixture, private):
    from types import SimpleNamespace

    ctx, event, calls = host_fixture
    event.get_platform_name = lambda: "aiocqhttp"
    event.get_sender_id = lambda: "12345"
    event.get_self_id = lambda: "42"
    event.get_group_id = lambda: "" if private else "67890"
    event.bot = SimpleNamespace(call_action=AsyncMock())

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(role="assistant", completion_text='{"reply_mode":"poke","text":"","meme_id":null}')

    ctx.provider.text_chat = chat
    bridge = AstrBotAgentBridge(ctx)
    out = await bridge.generate(event, (event,), "prompt", await bridge.snapshot(event), "provider")
    assert out.reply_mode == out.requested_reply_mode == "poke"
    assert out.text == out.narration == "" and not out.chains
    assert "text|poke|poke_text|silent" in str(calls[0])
    event.bot.call_action.assert_not_called()
    assert not event.sent and not ctx.saved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "narration", ["安安没有说话。", "（安安没有说话。）", "(安安没有说话。)", "（(安安没有说话。)）"]
)
async def test_narration_is_normalized_once(host_fixture, narration):
    ctx, event, _ = host_fixture
    ctx.provider.text_chat = AsyncMock(
        return_value=LLMResponse(
            role="assistant",
            completion_text=json.dumps({"reply_mode": "silent", "text": "", "narration": narration}),
        )
    )
    bridge = AstrBotAgentBridge(ctx)
    out = await bridge.generate(event, (event,), "prompt", await bridge.snapshot(event), "provider")
    assert out.narration == "（安安没有说话。）"
    assert out.text == "" and not out.chains
