"""Poke / 戳一戳 is a social tap, not a media attachment."""

from __future__ import annotations

import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.media_gate import MediaAirGate, detect_media_kinds
from astrbot_plugin_chat_dynamics.core.poke import PokeReplyPolicy, next_poke_streak
from astrbot_plugin_chat_dynamics.core.platform_bridge import (
    build_poke_chain,
    is_poke_placeholder,
    parse_group_event,
)
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.tests.test_platform_bridge import FakeEvent
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin


class Poke:
    def __init__(self, qq: str):
        self.qq = qq
        self.id = qq

    def target_id(self) -> str:
        return str(self.qq)


def test_parse_poke_is_not_media_and_fills_social_text():
    event = FakeEvent("", self_id="bot", comps=[Poke("bot")])
    parsed = parse_group_event(event)
    assert parsed.has_media is False
    assert parsed.media_only is False
    assert parsed.has_poke is True
    assert parsed.poke_at_bot is True
    assert parsed.poke_target_id == "bot"
    assert "戳一戳" in parsed.text
    assert "[媒体附件]" not in parsed.text
    assert is_poke_placeholder(parsed.text) is True


def test_parse_poke_at_others_is_not_addressed():
    event = FakeEvent("", self_id="bot", comps=[Poke("someone-else")])
    parsed = parse_group_event(event)
    assert parsed.has_poke is True
    assert parsed.poke_at_bot is False
    assert parsed.has_media is False
    assert "别人" in parsed.text


def test_outline_poke_is_not_media():
    event = FakeEvent("")
    event.get_message_outline = lambda: "[戳一戳]"
    parsed = parse_group_event(event)
    assert parsed.has_poke is True
    assert parsed.has_media is False


def test_media_gate_does_not_treat_poke_as_image():
    has_image, has_voice = detect_media_kinds(
        has_media=True, media_component_types=["Poke"], text="[戳一戳]"
    )
    assert has_image is False
    assert has_voice is False
    verdict = MediaAirGate().evaluate(
        text="[戳一戳] 有人戳了你",
        has_media=False,
        media_component_types=["Poke"],
    )
    assert verdict.reason_code == "no_media"
    assert verdict.allow_speak is True


def test_poke_policy_never_sends_text_and_poke_back_together():
    for seed in range(60):
        for streak in (1, 3, 4, 8):
            decision = PokeReplyPolicy(rng=random.Random(seed)).decide(
                at_bot=True,
                presence="lively",
                streak=streak,
                vibe=GroupChatMode.FAST_BANTER,
            )
            assert not (decision.text and decision.poke_back), (seed, streak, decision)


def test_poke_policy_varies_with_streak_and_presence():
    first = PokeReplyPolicy(rng=random.Random(1)).decide(at_bot=True, presence="sensible", streak=1)
    repeat = PokeReplyPolicy(rng=random.Random(1)).decide(at_bot=True, presence="lively", streak=4)
    spam = PokeReplyPolicy(rng=random.Random(2)).decide(at_bot=True, presence="sensible", streak=8)
    others = PokeReplyPolicy().decide(at_bot=False)
    assert first.speak is True
    assert first.reason == "poke_ack"
    assert first.text == ""
    assert repeat.speak is True
    assert repeat.reason == "poke_repeat"
    assert others.speak is False
    assert spam.reason in {"poke_spam", "poke_annoyed"}


def test_poke_policy_lively_banter_can_poke_back_only():
    hits = 0
    for seed in range(40):
        decision = PokeReplyPolicy(rng=random.Random(seed)).decide(
            at_bot=True,
            presence="lively",
            streak=1,
            vibe=GroupChatMode.FAST_BANTER,
        )
        if decision.reason == "poke_back_only":
            hits += 1
            assert decision.poke_back is True
            assert decision.text == ""
    assert hits >= 1


def test_poke_streak_resets_after_idle_window():
    store: dict[tuple[str, str], tuple[int, float]] = {}
    assert next_poke_streak(store, session_id="s", user_id="u", now=10.0) == 1
    assert next_poke_streak(store, session_id="s", user_id="u", now=20.0) == 2
    assert next_poke_streak(store, session_id="s", user_id="u", now=200.0) == 1


def test_build_poke_chain_carries_target():
    chain = build_poke_chain("user_9")
    names = [type(item).__name__.lower() for item in getattr(chain, "chain", [])]
    assert "poke" in names


@pytest.mark.asyncio
async def test_plugin_replies_to_poke_at_bot_without_media_path():
    plugin = _plugin({"pipeline_mode": "filter", "presence_knob": "lively"})
    plugin.poke_policy = PokeReplyPolicy(rng=random.Random(0))
    event = MockEvent(
        "",
        group_id="poke-bot",
        message_id="poke-1",
        self_id="bot_42",
        components=[Poke("bot_42")],
    )
    await plugin.on_group_message(event)
    assert event.call_llm is True
    assert event.is_stopped is True
    assert plugin._metrics.get("poke_seen", 0) >= 1
    assert plugin._metrics.get("media_seen", 0) == 0
    assert event.replies_sent
    assert len(event.replies_sent) == 1
    assert not any("媒体附件" in item for item in event.replies_sent)
    assert plugin._metrics.get("poke_replied", 0) >= 1
    await plugin.on_group_message(event)
    assert len(event.replies_sent) == 1


@pytest.mark.asyncio
async def test_plugin_ignores_poke_at_others():
    plugin = _plugin({"pipeline_mode": "filter"})
    event = MockEvent(
        "",
        group_id="poke-others",
        message_id="poke-2",
        self_id="bot_42",
        components=[Poke("someone")],
    )
    await plugin.on_group_message(event)
    assert event.call_llm is True
    assert event.replies_sent == []
    assert plugin._metrics.get("poke_seen", 0) >= 1
    assert plugin._metrics.get("poke_replied", 0) == 0
    assert plugin.debounce.get_pending_count(event.unified_msg_origin) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "empty", "error", "reset", "send_failed"])
async def test_poke_text_uses_reply_llm_and_commits_only_on_success(outcome):
    plugin = _plugin({"pipeline_mode": "filter", "presence_knob": "sensible", "reply_provider": "poke-model"})
    plugin.poke_policy = PokeReplyPolicy(rng=random.Random(0))
    event = MockEvent("", group_id="poke-llm", message_id="poke-generated", self_id="bot_42",
                      components=[Poke("bot_42")])
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        if outcome == "error":
            raise RuntimeError("provider unavailable")
        if outcome == "reset":
            plugin._sessions[event.unified_msg_origin].epoch += 1
        return SimpleNamespace(completion_text="" if outcome == "empty" else "刚刚在想你说的事呢。")

    plugin.context.llm_generate = generate
    if outcome == "send_failed":
        event.send = AsyncMock(return_value=False)
    await plugin.on_group_message(event)
    assert len(calls) == 1
    assert calls[0]["chat_provider_id"] == "poke-model"
    assert "连续第 1 次" in calls[0]["prompt"]
    runtime = plugin._sessions[event.unified_msg_origin]
    if outcome == "success":
        assert event.replies_sent == ["刚刚在想你说的事呢。"]
        assert runtime.last_bot_node.text == "刚刚在想你说的事呢。"
        await plugin.on_group_message(event)
        assert len(calls) == 1
    else:
        assert not event.replies_sent
        assert runtime.last_bot_node is None
        assert not plugin.arbiter.has_bot_spoken(event.unified_msg_origin)
    await plugin.terminate()
