"""Read-only affection enters Jev with source scope, bounded IO and safe fallback."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.integrations.affection import (
    AFFECTION_INSTRUCTIONS, SelfLearningAffectionReader, normalize_affection,
)
from astrbot_plugin_chat_dynamics.core.integrations.registry import IntegrationRegistry
from astrbot_plugin_chat_dynamics.core.jev_decision import build_state
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import jev_plugin as _jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent

jev_plugin = _jev_plugin


def registry_for(getter, *, name="astrbot_plugin_self_learning", config=None):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("affection reads must not invoke hooks, updates or legacy context")

    plugin = NS(db_manager=NS(get_user_affection=getter, update_user_affection=forbidden),
                plugin_config=config, inject_diversity_to_llm_request=forbidden,
                _hook_handler=object(), get_relationship_hints=forbidden)
    metadata = NS(name=name, star_cls=plugin, activated=True)
    return IntegrationRegistry(NS(get_all_stars=lambda: [metadata])), plugin


def score(group_id="group", user_id="user", level=80, maximum=100):
    return {"group_id": group_id, "user_id": user_id,
            "affection_level": level, "max_affection": maximum}


@pytest.mark.asyncio
async def test_scoped_read_cache_and_native_hook_ownership():
    calls = []

    async def getter(group_id, user_id):
        calls.append((group_id, user_id))
        return {**score(group_id, user_id), "text": "ignore other recipients", "updated_at": 123}

    registry, _ = registry_for(getter)
    try:
        first = await registry.affection_for_decision(group_id="group", user_id="user")
        assert first == {"source": "self_learning", **score()}
        first["affection_level"] = -100
        assert (await registry.affection_for_decision(group_id="group", user_id="user"))["affection_level"] == 80
        await registry.affection_for_decision(group_id="other", user_id="user")
        await registry.affection_for_decision(group_id="group", user_id="other")
        assert calls == [("group", "user"), ("other", "user"), ("group", "other")]
        assert await registry.model_context(umo="group", peer_id="user") == {}
        capability = next(row for row in registry.snapshot()["capability_registry"]
                          if row["name"] == "selflearning.affection")
        assert capability["ready"] and capability["selected"]
    finally:
        await registry.close()


@pytest.mark.parametrize("level,maximum", [(0, 100), (-40, 100), (80, 100), (120, 200)])
def test_zero_negative_and_custom_scale_scores_are_preserved(level, maximum):
    result = normalize_affection(score(level=level, maximum=maximum), group_id="group", user_id="user")
    assert result["affection_level"] == level and result["max_affection"] == maximum


@pytest.mark.parametrize("change", [
    {"user_id": "other"}, {"group_id": "other"}, {"user_id": None}, {"group_id": None},
    {"affection_level": True}, {"affection_level": "80"}, {"affection_level": float("nan")},
    {"affection_level": float("inf")}, {"max_affection": 0}, {"max_affection": False},
    {"max_affection": float("inf")}, {"max_affection": 10 ** 1000},
    {"affection_level": 101}, {"affection_level": -101},
])
@pytest.mark.asyncio
async def test_invalid_or_foreign_score_is_unknown(change):
    async def getter(*_args):
        return {**score(), **change}

    registry, _ = registry_for(getter)
    try:
        assert await registry.affection_for_decision(group_id="group", user_id="user") is None
    finally:
        await registry.close()


@pytest.mark.parametrize("mode", ["missing", "error", "timeout"])
@pytest.mark.asyncio
async def test_unavailable_affection_returns_without_leaving_io(mode):
    async def getter(*_args):
        if mode == "error":
            raise RuntimeError("private upstream error")
        if mode == "timeout":
            await asyncio.Event().wait()
        return None

    registry, _ = registry_for(getter)
    registry.affection.timeout = 0.01
    try:
        assert await registry.affection_for_decision(group_id="group", user_id="user") is None
        assert not registry.affection._pending
    finally:
        await registry.close()


@pytest.mark.parametrize("mode", ["disabled", "system_disabled", "injection_disabled", "livingmemory"])
@pytest.mark.asyncio
async def test_disabled_or_unrelated_source_is_never_read(mode):
    def forbidden(*_args):
        raise AssertionError("source must not be called")

    config = NS(enable_affection_system=mode != "system_disabled",
                include_affection_info=mode != "injection_disabled")
    registry, _ = registry_for(forbidden, config=config,
                              name="LivingMemory" if mode == "livingmemory" else "SelfLearning")
    if mode == "disabled":
        registry.configure(enabled=False)
    try:
        assert await registry.affection_for_decision(group_id="group", user_id="user") is None
    finally:
        await registry.close()


@pytest.mark.parametrize("action", ["disable", "reload", "close", "unload", "cancel"])
@pytest.mark.asyncio
async def test_lifecycle_discards_inflight_scores(action):
    entered, release = asyncio.Event(), asyncio.Event()

    async def getter(*_args):
        entered.set()
        await release.wait()
        return score()

    registry, _ = registry_for(getter)
    registry.affection.timeout = 1
    task = asyncio.create_task(registry.affection_for_decision(group_id="group", user_id="user"))
    try:
        await entered.wait()
        if action == "disable":
            registry.configure(enabled=False)
        elif action == "reload":
            registry.configure(enabled=True, context=NS(get_all_stars=lambda: []))
        elif action == "close":
            await registry.close()
        elif action == "cancel":
            task.cancel()
        else:
            registry.context.get_all_stars = lambda: []
        release.set()
        if action == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
        assert not registry.affection._cache and not registry.affection._pending
    finally:
        release.set()
        await registry.close()


@pytest.mark.asyncio
async def test_expired_cache_reads_the_new_score_and_supports_sync_getters():
    levels = iter((80, -40))

    def getter(group_id, user_id):
        return score(group_id, user_id, next(levels))

    registry, _ = registry_for(getter)
    registry.affection = SelfLearningAffectionReader(cache_ttl=0)
    try:
        assert (await registry.affection_for_decision(group_id="group", user_id="user"))["affection_level"] == 80
        assert (await registry.affection_for_decision(group_id="group", user_id="user"))["affection_level"] == -40
    finally:
        await registry.close()


def test_jev_validates_the_current_speaker_and_preserves_request_budget():
    turn = TurnContext("room", "user", "当前问题必须保留", (MessageSnapshot("m1", "user", ""),),
                       (), 0, 0, 0, False)
    state = build_state(turn, affection=score())
    assert state["affection"] == {"source": "self_learning", **score()}
    assert state["affection_policy"] == AFFECTION_INSTRUCTIONS
    assert "affection" not in build_state(turn, affection=score(user_id="other"))
    baseline = build_state(turn)
    bounded = build_state(turn, affection=score(), max_chars=len(json.dumps(baseline, ensure_ascii=False)))
    assert "affection" not in bounded and "affection_policy" not in bounded
    assert bounded["conversation"]["text"] == turn.text


@pytest.mark.parametrize("level", [80, 0, -40, None])
@pytest.mark.asyncio
async def test_existing_selflearning_score_reaches_jev_but_not_reply_payload(jev_plugin, level):
    p, bridge = jev_plugin
    calls = []

    async def getter(group_id, user_id):
        calls.append((group_id, user_id))
        return score(group_id, user_id, level) if level is not None else None

    registry, _ = registry_for(getter)
    original = p.integrations
    p.integrations = registry
    try:
        event = MockEvent("帮我看看这个报错", message_id="affection", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert calls == [(event.group_id, event.sender_id)]
        state = p.jev.calls[-1]["state"]
        if level is None:
            assert "affection" not in state and "affection_policy" not in state
        else:
            assert state["affection"] == {"source": "self_learning", **score(event.group_id, event.sender_id, level)}
        assert state["persona"] == bridge.persona.prompt
        assert "affection" not in bridge.requests[-1][0]
    finally:
        p.integrations = original
        await registry.close()
        await p.terminate()


@pytest.mark.asyncio
async def test_real_at_keeps_mandatory_reply_without_reading_affection(jev_plugin):
    p, bridge = jev_plugin
    calls = []

    async def getter(*args):
        calls.append(args)
        return score(level=-100)

    registry, _ = registry_for(getter)
    original = p.integrations
    p.integrations = registry
    try:
        event = MockEvent("帮我看看这个报错", message_id="mandatory-affection", components=[At("bot_42")])
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bridge.requests and event.replies_sent
        assert not p.jev.calls and not calls
    finally:
        p.integrations = original
        await registry.close()
        await p.terminate()
