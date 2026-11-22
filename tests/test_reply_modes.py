"""Owned response choices terminate without accidental fallback or extra media."""

from unittest.mock import AsyncMock
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot_plugin_chat_dynamics.core.agent_bridge import AgentOutput
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, Reply
from .test_private_chat_chain import private_event, private_plugin as private_plugin


@pytest.mark.parametrize("mode", ["text", "meme_only", "text_meme", "silent"])
def test_reply_choice_survives_runtime_restart(mode):
    from .test_runtime_persistence import plugin
    from astrbot_plugin_chat_dynamics.core.runtime_persistence import export_runtime_state, restore_runtime_state

    source = plugin(100)
    choice = {
        "requested": mode,
        "actual": mode,
        "no_reply": mode == "silent",
        "delivered": mode != "silent",
        "reason": "model_silent" if mode == "silent" else "model_selected_" + mode,
    }
    source._registry.get_or_create("room").dag.add_message(
        "request", "user", "hello", timestamp=95, metadata={"reply_choice": choice}
    )
    target = plugin(100)
    restore_runtime_state(target, export_runtime_state(source))
    assert target._registry.get("room").dag.get_node("request").metadata["reply_choice"] == choice


@pytest.mark.parametrize("wake", ["at", "quote", "legacy", "none"])
def test_valid_ignore_survives_wake_admission(wake):
    from .test_jev_decision import turn
    from astrbot_plugin_chat_dynamics.core.jev_decision import decision_from_answers

    context = replace(turn("m1"), wake_kind=wake)
    result = decision_from_answers(context, answers(action="ignore", state="observing"))
    assert result.action == "ignore"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["group", "at", "quote", "private"])
@pytest.mark.parametrize("mode", ["text", "meme_only", "text_meme", "silent", "narration", "poke"])
async def test_response_modes_own_delivery(scope, mode, request):
    p, bridge = request.getfixturevalue("private_plugin" if scope == "private" else "jev_plugin")
    event = private_event() if scope == "private" else MockEvent("这件事你怎么看", message_id="mode-turn")
    if scope == "at":
        event = MockEvent("这件事你怎么看", message_id="mode-turn", components=[At("bot_42")])
    elif scope == "quote":
        quote = Reply("old-bot")
        quote.sender_id = "bot_42"
        event = MockEvent("这件事你怎么看", message_id="mode-turn", components=[quote])
    channels = []
    text = "确实很有意思。" if mode in {"text", "text_meme"} else ""
    chains = []
    if mode in {"meme_only", "text_meme"}:
        chains = [MessageChain(([Plain(text)] if text else []) + [Image(file="/tmp/offline-meme.png")])]
    if mode in {"narration", "poke"}:
        event.sender_id = "123456789"
        event.get_sender_id = lambda: "123456789"
    action = None
    if mode == "poke":
        event.get_platform_name = lambda: "aiocqhttp"
        if hasattr(event, "group_id"):
            event.group_id = "" if scope == "private" else "987654321"
        action = AsyncMock(return_value={"status": "ok", "message_id": "poke-mode"})
        event.bot = SimpleNamespace(call_action=action)
    if mode == "narration":
        output = AgentOutput(
            "",
            "conv",
            "[]",
            [],
            reply_mode="narration",
            requested_reply_mode="silent",
            reply_reason="model_silent_narration",
            narration="（安安正在熟睡中）",
        )
    else:
        output = AgentOutput(
            text if mode == "text" else "",
            "conv",
            "[]",
            [],
            chains=chains,
            reply_mode=mode,
            requested_reply_mode=mode,
            reply_reason="model_silent" if mode == "silent" else "model_selected_" + mode,
        )

    async def generate(*args, **kwargs):
        channels.append(kwargs["deferred_reply_media"])
        return output

    bridge.generate = generate
    event.send = AsyncMock(wraps=event.send)
    try:
        await (p.on_private_message(event) if scope == "private" else p.on_group_message(event))
        await flush(p, event)
        await drain(p)
        runtime = p._sessions[event.unified_msg_origin]
        node = runtime.dag.get_node(event.message_id)
        assert channels, "The admitted turn must reach the reply agent"
        narration_visible = mode == "narration" and scope == "at"
        narration_suppressed = mode == "narration" and not narration_visible
        expected_mode = "silent" if mode == "silent" or narration_suppressed else mode
        assert bool(event.replies_sent) == (mode not in {"silent", "poke"} and not narration_suppressed)
        assert node.metadata["reply_choice"]["requested"] == ("silent" if mode == "narration" else mode)
        assert node.metadata["reply_choice"]["actual"] == expected_mode
        assert node.metadata["reply_choice"]["delivered"] == (mode != "silent" and not narration_suppressed)
        if mode == "narration" and narration_suppressed:
            choice = node.metadata["reply_choice"]
            assert choice["reason"] == "narration_requires_at"
            assert choice["no_reply"] is True
            assert choice["intentional_nonverbal"] is False
            assert not bridge.commits
            assert event.send.await_count == 0
            assert node.metadata["outcome"]["final_outcome"] == "suppressed"
            assert node.metadata["outcome"]["suppression_reason"] == "narration_requires_at"
            assert not p._metrics.get("llm_reply_failed", 0)
        elif mode in {"narration", "poke"}:
            assert node.metadata["reply_choice"]["intentional_nonverbal"] is True
            assert node.metadata["reply_choice"]["no_reply"] is False
            assert bridge.commits
            assert runtime.last_bot_node is None
            if mode == "narration":
                assert event.send.await_count == 1
                components = event.send.call_args.args[0].chain
                assert [type(component).__name__ for component in components] == ["Plain"]
                assert components[0].text == "（安安正在熟睡中）"
            else:
                if scope == "private":
                    action.assert_awaited_once_with("send_poke", user_id="123456789")
                else:
                    action.assert_awaited_once_with("send_poke", user_id="123456789", group_id="987654321")
        if mode == "silent" or narration_suppressed:
            assert not bridge.commits
            assert runtime.last_bot_node is None
            assert not runtime.ambient_openings
            assert node.metadata["outcome"]["final_outcome"] == "suppressed"
            assert node.metadata["outcome"]["suppression_reason"] == (
                "narration_requires_at" if narration_suppressed else "model_silent"
            )
            assert not p._metrics.get("llm_reply_failed", 0)
        else:
            assert node.metadata["outcome"]["delivered"]
        effect = AsyncMock(return_value=True)
        assert not await channels[0].send(MessageChain([Plain("unexpected")]), effect)
        effect.assert_not_awaited()
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["meme_only", "text_meme"])
async def test_candidate_expiring_at_send_preserves_only_selected_text(jev_plugin, mode):
    p, bridge = jev_plugin
    text = "贴切。" if mode == "text_meme" else ""
    chain = MessageChain(([Plain(text)] if text else []) + [Image(file="/tmp/missing.png")])
    chain._cd_media_validate_v1 = lambda: False
    chain._cd_meme_fallback_text_v1 = text
    bridge.generate = AsyncMock(
        return_value=AgentOutput("", "conv", "[]", [], chains=[chain], reply_mode=mode, requested_reply_mode=mode)
    )
    event = MockEvent("来个表情", message_id="expired", components=[At("bot_42"), Reply("earlier")])
    event.send = AsyncMock(wraps=event.send)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        choice = p._sessions[event.unified_msg_origin].dag.get_node("expired").metadata["reply_choice"]
        assert choice["actual"] == ("text" if text else "silent")
        assert choice["reason"] == "meme_unavailable"
        assert bool(event.replies_sent) == bool(text)
        if text:
            assert text in str(event.replies_sent)
            assert "我发送的表情包" not in str(bridge.commits)
            assert event.send.await_count == 1
            components = event.send.call_args.args[0].chain
            assert [type(component).__name__ for component in components] == ["Reply", "Plain"]
            assert components[0].id == "expired"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_model_selected_poke_uses_onebot_action_for_current_sender(jev_plugin):
    p, bridge = jev_plugin
    event = MockEvent("你好", sender_id="123456789", group_id="987654321", message_id="model-poke")
    event.get_platform_name = lambda: "aiocqhttp"
    action = AsyncMock(return_value={"status": "ok", "message_id": "poke-1"})
    event.bot = SimpleNamespace(call_action=action)
    bridge.generate = AsyncMock(
        return_value=AgentOutput(
            "", "conv", "[]", [], reply_mode="poke", requested_reply_mode="poke", reply_reason="model_selected_poke"
        )
    )
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        action.assert_awaited_once_with("send_poke", user_id="123456789", group_id="987654321")
        assert not event.replies_sent
        node = p._sessions[event.unified_msg_origin].dag.get_node(event.message_id)
        assert node.metadata["reply_choice"]["delivered"] is True
        assert node.metadata["reply_choice"]["actual"] == "poke"
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "components,is_at,admitted",
    [
        ([At("human_99")], False, False),  # The other recipient is rejected before reply generation.
        ([type("AtAll", (), {})()], False, True),
        ([], False, True),  # Nickname/soft wake is not a forced @.
        ([], True, True),  # A host wake flag without a real @ remains a soft wake.
    ],
)
async def test_no_reply_narration_is_suppressed_without_forced_bot_at(jev_plugin, components, is_at, admitted):
    p, bridge = jev_plugin
    event = MockEvent(
        "安安，最近忙吗？",
        sender_id="123456789",
        group_id="987654321",
        message_id="narration-no-forced-at",
        components=components,
        is_at_or_wake_command=is_at,
    )
    event.send = AsyncMock(wraps=event.send)
    bridge.generate = AsyncMock(
        return_value=AgentOutput(
            "",
            "conv",
            "[]",
            [],
            reply_mode="narration",
            requested_reply_mode="silent",
            reply_reason="model_silent_narration",
            narration="（安安暂时没有回应）",
        )
    )
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node(event.message_id)
        if not admitted:
            bridge.generate.assert_not_awaited()
            assert "reply_choice" not in node.metadata
            assert not bridge.commits and not event.replies_sent
            event.send.assert_not_awaited()
            return
        choice = node.metadata["reply_choice"]
        assert choice["requested"] == choice["actual"] == "silent"
        assert choice["reason"] == "narration_requires_at"
        assert choice["no_reply"] is True and not choice["delivered"]
        assert not bridge.commits and not event.replies_sent
        event.send.assert_not_awaited()
        assert node.metadata["outcome"]["final_outcome"] == "suppressed"
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "sender", "group", "change_sender_after_admission"),
    [
        ("other", "123456789", "987654321", False),
        ("aiocqhttp", "１２３４５６７８９", "987654321", False),
        ("aiocqhttp", "123456789", "９８７６５４３２１", False),
        ("aiocqhttp", "123456789", "987654321", True),
    ],
)
async def test_invalid_model_poke_target_fails_closed_without_text_fallback(
    jev_plugin, platform, sender, group, change_sender_after_admission
):
    p, bridge = jev_plugin
    event = MockEvent("你好", sender_id=sender, group_id=group, components=[At("bot_42")])
    current_sender = [sender]
    event.get_sender_id = lambda: current_sender[0]
    event.get_platform_name = lambda: platform
    action = AsyncMock(return_value={"status": "ok", "message_id": "unused"})
    event.bot = SimpleNamespace(call_action=action)

    async def generate(*args, **kwargs):
        if change_sender_after_admission:
            current_sender[0] = "987654321"
        return AgentOutput(
            "", "conv", "[]", [], reply_mode="poke", requested_reply_mode="poke", reply_reason="model_selected_poke"
        )

    bridge.generate = generate
    event.send = AsyncMock(wraps=event.send)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        action.assert_not_awaited()
        event.send.assert_not_awaited()
        node = p._sessions[event.unified_msg_origin].dag.get_node(event.message_id)
        assert not event.replies_sent
        assert node.metadata["reply_choice"]["intentional_nonverbal"] is True
        assert node.metadata["reply_choice"]["delivered"] is False
        assert node.metadata["outcome"]["final_outcome"] == "delivery_failed"
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["at", "private"])
@pytest.mark.parametrize("mode", ["narration", "poke"])
async def test_failed_nonverbal_send_does_not_trigger_mandatory_text_fallback(request, scope, mode):
    p, bridge = request.getfixturevalue("private_plugin" if scope == "private" else "jev_plugin")
    event = (
        private_event(sender="123456789")
        if scope == "private"
        else MockEvent("你好", sender_id="123456789", components=[At("bot_42")])
    )
    if scope == "at":
        event.group_id = "987654321"
    event.get_sender_id = lambda: "123456789"
    event.get_platform_name = lambda: "aiocqhttp"
    event.send = AsyncMock(side_effect=RuntimeError("offline"))
    action = AsyncMock(side_effect=RuntimeError("offline"))
    event.bot = SimpleNamespace(call_action=action)
    output = AgentOutput(
        "",
        "conv",
        "[]",
        [],
        reply_mode="narration" if mode == "narration" else "poke",
        requested_reply_mode="silent" if mode == "narration" else "poke",
        reply_reason="model_silent_narration" if mode == "narration" else "model_selected_poke",
        narration="（安安暂时没有回应）" if mode == "narration" else "",
    )
    bridge.generate = AsyncMock(return_value=output)
    try:
        await (p.on_private_message(event) if scope == "private" else p.on_group_message(event))
        await flush(p, event)
        await drain(p)
        node = p._sessions[event.unified_msg_origin].dag.get_node(event.message_id)
        if mode == "narration" and scope == "private":
            assert node.metadata["reply_choice"]["actual"] == "silent"
            assert node.metadata["reply_choice"]["reason"] == "narration_requires_at"
            assert node.metadata["reply_choice"]["no_reply"] is True
            assert node.metadata["reply_choice"]["delivered"] is False
            assert node.metadata["outcome"]["final_outcome"] == "suppressed"
            assert event.send.await_count == 0
            assert not event.replies_sent
            assert not bridge.commits
            assert not p._metrics.get("llm_reply_failed", 0)
            return
        assert node.metadata["reply_choice"]["intentional_nonverbal"] is True
        assert node.metadata["reply_choice"]["delivered"] is False
        assert node.metadata["outcome"]["final_outcome"] == "delivery_failed"
        assert not event.replies_sent
        assert action.await_count == (1 if mode == "poke" else 0)
        assert event.send.await_count == (0 if mode == "poke" else 1)
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["narration", "poke"])
@pytest.mark.parametrize("evidence_current", [True, False])
async def test_nonverbal_keeps_evidence_guard_without_speech_receipts(jev_plugin, mode, evidence_current):
    from astrbot_plugin_chat_dynamics.core.platform_bridge import build_plain_chain, build_poke_chain

    p, _ = jev_plugin
    event = MockEvent("你好", sender_id="123456789", group_id="987654321")
    event.get_platform_name = lambda: "aiocqhttp"
    event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"status": "ok"}))
    event._extras = {"_narrative_managed_v1": {"character_id": "test"}, "_chat_dynamics_nonverbal_send_v1": mode}
    original_narrative = getattr(p, "narrative", None)
    guard = SimpleNamespace(
        evidence_current_async=AsyncMock(return_value=evidence_current), begin_send_async=AsyncMock()
    )
    p.narrative = guard
    runtime = p._registry.get_or_create(event.unified_msg_origin)
    chain = build_poke_chain("123456789") if mode == "poke" else build_plain_chain("（安安暂时没有回应）")
    try:
        sent = await p._send_owned(runtime, event, chain)
        assert sent.success == evidence_current
        guard.evidence_current_async.assert_awaited_once_with(event._extras["_narrative_managed_v1"])
        guard.begin_send_async.assert_not_awaited()
        assert bool(event.replies_sent) == (evidence_current and mode == "narration")
        assert event.bot.call_action.await_count == (1 if evidence_current and mode == "poke" else 0)
        if not evidence_current:
            assert sent.error == "boundary_policy_cancelled"
    finally:
        p.narrative = original_narrative
        await p.terminate()


@pytest.mark.asyncio
async def test_empty_reply_is_failure_and_keeps_at_fallback(jev_plugin):
    p, bridge = jev_plugin
    bridge.generate = AsyncMock(return_value=AgentOutput("", "conv", "[]", [], reply_mode="text"))
    event = MockEvent("你好", components=[At("bot_42")])
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert event.replies_sent
        assert p._metrics["llm_reply_failed"]
        choice = p._sessions[event.unified_msg_origin].dag.get_node(event.message_id).metadata["reply_choice"]
        assert choice["delivered"] and choice["actual"] == "text"
        assert choice["reason"] == "at_reply_fallback"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_valid_decision_ignore_is_not_replaced_by_reply(jev_plugin):
    p, bridge = jev_plugin
    p.jev.payload = answers(action="ignore", state="observing")
    quote = Reply("old-bot")
    quote.sender_id = "bot_42"
    event = MockEvent("不用回复了", components=[quote])
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not bridge.requests and not event.replies_sent
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "text_meme"])
async def test_newer_diagnostic_during_send_does_not_lose_receipt(jev_plugin, mode):
    p, bridge = jev_plugin
    event = MockEvent("这件事你怎么看", message_id="receipt-race", components=[At("bot_42")])
    chain = MessageChain([Plain("已经收到了。"), Image(file="/tmp/expired.png")])
    chain._cd_media_validate_v1 = lambda: False
    chain._cd_meme_fallback_text_v1 = "已经收到了。"
    bridge.generate = AsyncMock(return_value=AgentOutput(
        "已经收到了。" if mode == "text" else "", "conv", "[]", [],
        chains=[] if mode == "text" else [chain], reply_mode=mode, requested_reply_mode=mode,
    ))
    original_send = event.send
    newer_diagnostic = {"reason_code": "newer_turn"}

    async def send_and_replace_diagnostic(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        p._sessions[event.unified_msg_origin].model_diagnostic = newer_diagnostic
        return result

    event.send = send_and_replace_diagnostic
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        runtime = p._sessions[event.unified_msg_origin]
        node = runtime.dag.get_node("receipt-race")
        assert len(event.replies_sent) == 1
        assert node.metadata["outcome"]["delivered"]
        assert node.metadata["reply_choice"]["delivered"]
        assert node.metadata["reply_choice"]["actual"] == "text"
        assert bridge.commits
        assert runtime.model_diagnostic is newer_diagnostic
        assert "reply_choice" not in newer_diagnostic
        assert not p._metrics.get("send_failed", 0)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_text_meme_splits_text_and_image_into_separate_messages(jev_plugin):
    from astrbot_plugin_chat_dynamics.core.persona_engine import split_text_meme_chains

    p, bridge = jev_plugin
    caption = "别突然上手捏脸啊，脸都要被你揉扁了。"
    meme_note = '[表情历史：{"desc":"捏脸表情"}]'
    chain = MessageChain([Plain(caption), Image(file="/tmp/pinch.png")])
    chain._cd_delivery_text_v1 = f"{caption}\n{meme_note}"
    confirmed = AsyncMock()
    chain._cd_media_success_v1 = confirmed
    chain._cd_media_validate_v1 = lambda: True
    chain._cd_meme_fallback_text_v1 = caption

    # Respect max_fragments=1 without splitting.
    assert split_text_meme_chains([chain], p.pacer, max_fragments=1) == [chain]

    # Multi-paragraph caption splits prose paragraphs first, then pure image bubble.
    multi = MessageChain([Plain("第一段。\n\n第二段。"), Image(file="/tmp/pinch.png")])
    split_multi = split_text_meme_chains([multi], p.pacer, max_fragments=5)
    assert split_multi[:2] == ["第一段。", "第二段。"]
    assert [type(c).__name__ for c in split_multi[2].chain] == ["Image"]

    bridge.generate = AsyncMock(
        return_value=AgentOutput(
            "",
            "conv",
            "[]",
            [],
            chains=[chain],
            reply_mode="text_meme",
            requested_reply_mode="text_meme",
            reply_reason="model_selected_text_meme",
        )
    )
    event = MockEvent("捏捏脸", message_id="pinch-msg", components=[At("bot_42"), Reply("earlier")])
    event.send = AsyncMock(wraps=event.send)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)

        assert event.send.await_count == 2
        first_components = event.send.call_args_list[0].args[0].chain
        second_components = event.send.call_args_list[1].args[0].chain
        assert [type(c).__name__ for c in first_components] == ["Reply", "Plain"]
        assert first_components[1].text == caption
        assert [type(c).__name__ for c in second_components] == ["Image"]
        confirmed.assert_awaited_once()

        # History commit records caption once + meme note without duplicating caption.
        assert bridge.commits
        committed_reply = str(bridge.commits[-1])
        assert committed_reply.count(caption) == 1
        assert meme_note in committed_reply

        runtime = p._sessions[event.unified_msg_origin]
        choice = runtime.dag.get_node("pinch-msg").metadata["reply_choice"]
        assert choice["actual"] == "text_meme"
        assert choice["delivered"] is True
    finally:
        await p.terminate()

