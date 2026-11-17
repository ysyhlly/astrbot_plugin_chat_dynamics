import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from .test_narrative_coordination import coordinator, SCOPE
from astrbot_plugin_chat_dynamics.core.narrative import TurnContextBundle
from astrbot_plugin_chat_dynamics.core.agent_bridge import PersonaSnapshot

from astrbot_plugin_chat_dynamics.core.turn_decision import TurnDecision


@pytest.fixture(params=["care_wait", "social_chat"])
def harness(tmp_path, monkeypatch, request):
    manager = coordinator(tmp_path)
    p = manager.plugin
    p._runtime_config.narrative_outreach_enabled = True
    p._runtime_config.narrative_outreach_sessions = [SCOPE["umo"]]
    p.time_service.time = lambda: 50
    p.time_service.wall_time = lambda: 10000
    p._metric = lambda *a: None
    p.is_group_takeover_enabled = lambda group: True
    p._resolve_session_key = lambda umo: umo
    p._last_bot_nodes = {}
    p._next_outgoing_id = lambda: "sent-1"
    p._observe_routed_bot = lambda *a: None
    p.arbiter = NS(is_in_deep_cooling=lambda *a, **k: False, record_bot_spoke=lambda *a, **k: None)
    p.pacer = None
    node = NS(msg_id="sent-1")
    runtime = NS(
        bot_id="bot",
        epoch=0,
        user_revisions={},
        interaction_state="observing",
        send_lock=asyncio.Lock(),
        dag=NS(get_recent_nodes=lambda **k: [], add_message=lambda **k: node),
        remember_sent=lambda mid: None,
    )
    p._sessions = {SCOPE["umo"]: runtime}
    p._get_or_create_runtime = lambda *a, **k: runtime
    event = NS(_extras={}, unified_msg_origin=SCOPE["umo"])
    event.send = AsyncMock(return_value={"message_id": "sent-1"})
    manager.internal_event = lambda scope: event

    @asynccontextmanager
    async def lock(*args):
        yield

    bridge = NS(
        snapshot=AsyncMock(return_value=PersonaSnapshot("p", "cid", "anan", "core")),
        session_lock=lock,
        commit=AsyncMock(return_value=True),
    )
    p.persona_engine = NS(
        bridge=bridge,
        decide=AsyncMock(
            return_value=TurnDecision("reply", "focused", ("intent:care:1",), "关心", "brief", "care_check")
        ),
        generate=AsyncMock(return_value=NS(chains=[], text="到家了吗？方便时回我就好。")),
    )
    p.llm = NS(resolve_provider_id=AsyncMock(return_value="offline"))
    payload = dict(
        scope=SCOPE, snapshot={"revision": 1, "scope_revision": 1}, character={"core_prompt": "core", "group_rules": ""}
    )
    bundle = TurnContextBundle("turn", json.dumps(payload))
    manager.prepare = AsyncMock(return_value=bundle)
    manager.current = AsyncMock(return_value=True)
    item = dict(
        intent_id="care:1",
        scope=SCOPE,
        kind=request.param,
        recipient_user_id="123",
        source_event_id="source",
        source_occurred_at=1,
        reason="约好到家报平安",
        evidence_quote="到家告诉你",
        waiting_for="到家消息",
        status="pending",
        managed=True,
        paused=False,
        outreach_paused=False,
        not_before=9000,
        expires_at=12000,
    )

    async def rpc(provider, method, *args, **kwargs):
        if method == "get_intent":
            return {"item": copy.deepcopy(item)}
        if method == "list_due_intents":
            return [copy.deepcopy(item)] if item["status"] == "pending" else []
        if method == "get_snapshot":
            return payload["snapshot"]
        if method == "cancel_intent":
            item["status"] = "cancelled"
            return {"changed": True}
        if method == "update_intent":
            item["status"] = args[1]
            return {"changed": True}

    manager.call = AsyncMock(side_effect=rpc)

    async def send_owned(runtime, incoming, fragment, **kwargs):
        assert incoming._chat_dynamics_boundary_uid == "123"
        assert incoming._extras["_narrative_recipient_v1"] == "123"
        return await kwargs["send_effect"](incoming)

    p._send_owned = AsyncMock(side_effect=send_owned)
    p.context = NS()
    from astrbot_plugin_chat_dynamics.core import member_boundary as boundary

    state = NS(available=True, disabled=False, required=True, service=object())
    monkeypatch.setattr(boundary, "resolve_state", lambda *a, **k: state)
    monkeypatch.setattr(boundary, "peek", lambda *a, **k: ({"active": False}, {"target_revision": 1}, state))
    monkeypatch.setattr(boundary, "capture_policy_snapshot", lambda *a: ({"targets": {}}, state))
    return manager, p, event, item, boundary, state


@pytest.mark.asyncio
async def test_targeted_care_send_and_history(harness):
    manager, p, event, item, _, _ = harness
    await manager.send_intent(copy.deepcopy(item))
    assert item["status"] == "sent"
    event.send.assert_awaited_once()
    chain = event.send.call_args.args[0]
    assert str(chain.chain[0].qq) == "123"
    assert p.persona_engine.generate.await_count == 1
    p.persona_engine.bridge.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_can_decline_without_repeated_generation(harness):
    manager, p, event, item, _, _ = harness
    p.persona_engine.decide.return_value = replace(p.persona_engine.decide.return_value, action="ignore")
    await manager.send_intent(copy.deepcopy(item))
    await manager.send_intent(copy.deepcopy(item))
    assert item["status"] == "cancelled"
    assert p.persona_engine.decide.await_count == 1
    p.persona_engine.generate.assert_not_awaited()
    event.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_boundary_blocks_target_without_borrowing_bot_identity(harness, monkeypatch):
    manager, p, event, item, boundary, state = harness
    monkeypatch.setattr(boundary, "peek", lambda *a, **k: ({"active": True, "mode": "silence"}, {}, state))
    await manager.send_intent(item)
    p.persona_engine.decide.assert_not_awaited()
    event.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_return_during_generation_cancels(harness):
    manager, p, event, item, _, _ = harness

    async def generate(*a, **k):
        item["status"] = "cancelled"
        return NS(chains=[], text="问候")

    p.persona_engine.generate.side_effect = generate
    await manager.send_intent(item)
    event.send.assert_not_awaited()
    p.persona_engine.bridge.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_after_claim_is_checked_inside_send(harness):
    manager, p, event, item, _, _ = harness
    old = p._send_owned.side_effect

    async def send(*a, **k):
        item["status"] = "cancelled"
        return await old(*a, **k)

    p._send_owned.side_effect = send
    await manager.send_intent(item)
    event.send.assert_not_awaited()
    p.persona_engine.bridge.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_group_intent_still_rejected_with_member_boundary(harness):
    manager, p, event, item, _, _ = harness
    item.pop("kind")
    await manager.send_intent(item)
    p.persona_engine.decide.assert_not_awaited()
    event.send.assert_not_awaited()
