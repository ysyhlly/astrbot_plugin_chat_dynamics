"""The mood calibration has its own backend, and degrades to "keep the reading".

The turn decision and the mood read are configured independently on purpose — one
runs every turn, the other once every couple of minutes — so what is pinned here is
that independence, that both backends judge identical evidence, and that a
low-confidence or unavailable read never invents a mood.
"""

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from .test_jev_decision_layer import JevDouble
from .test_plugin_lifecycle import _plugin

KEY = "g/1"
TEXT = "有人在吗"


def vibe_answer(choice="chill_fade", confidence=0.9):
    return {"vibe": {"type": "choice", "choice": choice,
                     "confidence": confidence, "probabilities": {choice: 1.0}}}


def test_the_mood_backend_is_configured_apart_from_the_turn_decision():
    """A decision model for one read and a chat model for the other is a real ask."""
    config, warnings = parse_runtime_config({
        "decision_mode": "persona_model",
        "decision_backend": "model",
        "vibe_backend": "laya",
        "vibe_min_confidence": 0.7,
    })
    assert config.decision_backend == "model"
    assert config.vibe_backend == "laya"
    assert config.vibe_min_confidence == 0.7
    assert not [warning for warning in warnings if "vibe" in warning]


def test_an_unknown_mood_backend_falls_back_to_the_chat_model():
    config, warnings = parse_runtime_config({"vibe_backend": "ouija"})
    assert config.vibe_backend == "llm"
    assert any("vibe_backend is invalid" in warning for warning in warnings)

    clamped, warnings = parse_runtime_config({"vibe_min_confidence": 0.01})
    assert clamped.vibe_min_confidence == 0.55
    assert any("vibe_min_confidence" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_only_one_mood_backend_keeps_the_laya_service_reachable():
    """The turn decision and the mood read share one client; either one justifies it."""
    turn_only = _plugin({"decision_mode": "persona_model", "decision_backend": "laya"})
    mood_only = _plugin({"vibe_backend": "laya"})
    neither = _plugin({})
    try:
        assert turn_only.laya.snapshot()["configured"] is True
        assert mood_only.laya.snapshot()["configured"] is True
        assert neither.laya.snapshot()["status"] == "disabled"
    finally:
        for plugin in (turn_only, mood_only, neither):
            await plugin.terminate()


@pytest.mark.asyncio
async def test_laya_reads_the_mood_and_the_chat_model_is_not_asked():
    plugin = _plugin({"vibe_backend": "laya", "vibe_llm_enabled": True})
    asked = []

    async def refuse(**kwargs):
        asked.append(kwargs)
        raise AssertionError("the chat model must not be consulted behind a Laya vibe backend")

    plugin._generate_llm = refuse
    plugin.laya = JevDouble(vibe_answer("chill_fade", 0.9))
    try:
        mode = await plugin._classify_vibe(KEY, TEXT)
        assert mode is GroupChatMode.CHILL_FADE
        assert not asked
        assert plugin._vibe_source() == "laya"
        call = plugin.laya.calls[0]
        assert set(call["questions"]) == {"vibe"}
        assert set(call["questions"]["vibe"]["criteria"]) == {
            "fast_banter", "serious_inquiry", "chill_fade"}
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_both_backends_judge_the_same_evidence():
    """A disagreement must be one of judgement, never one of input."""
    plugin = _plugin({"vibe_backend": "laya"})
    plugin.laya = JevDouble(vibe_answer())
    captured = {}

    async def record(context_prompt=None, **kwargs):
        captured["prompt"] = context_prompt
        return "chill_fade"

    plugin._generate_llm = record
    try:
        plugin.telemetrics.record_message(KEY, "第一条消息", plugin.time_service.time())
        evidence = await plugin._vibe_evidence(KEY, TEXT)
        await plugin._classify_vibe(KEY, TEXT)
        await plugin._classify_vibe_with_llm(KEY, TEXT)

        state = plugin.laya.calls[0]["state"]
        assert state["recent_messages"] == evidence["recent_messages"]
        for message in evidence["recent_messages"]:
            assert message in captured["prompt"]
        assert str(evidence["telemetrics"].mpm) in captured["prompt"]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    vibe_answer("chill_fade", 0.1),          # below the floor
    vibe_answer("", 0.9),                    # outside the closed vocabulary
    {"vibe": {"type": "noul", "noul": 0.5}},  # wrong answer type
    {"vibe": {"type": "choice", "choice": "chill_fade"}},  # no confidence at all
    None,                                     # the service could not answer
])
async def test_a_read_that_cannot_be_trusted_invents_no_mood(answer):
    """None means "keep the reading you had"; it is never a negative answer."""
    plugin = _plugin({"vibe_backend": "laya"})
    plugin.laya = JevDouble(answer)
    try:
        assert await plugin._classify_vibe(KEY, TEXT) is None
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_the_chat_model_still_reads_the_mood_by_default():
    plugin = _plugin({"vibe_backend": "llm", "vibe_llm_enabled": True})
    plugin.laya = JevDouble(vibe_answer())

    async def classify(**kwargs):
        return "serious_inquiry"

    plugin._generate_llm = classify
    try:
        assert plugin._vibe_source() == "llm"
        assert await plugin._classify_vibe(KEY, TEXT) is GroupChatMode.SERIOUS_INQUIRY
        assert plugin.laya.calls == []
    finally:
        await plugin.terminate()
