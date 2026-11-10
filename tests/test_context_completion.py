"""Bounded contextual lookup, immutable evidence and final reply decisions."""

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.context_completion import complete_context
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import build_questions
from astrbot_plugin_chat_dynamics.core.narrative import TurnContextBundle, _narrative_scope_key
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent
from .test_narrative_coordination import coordinator, SCOPE


def option(value):
    return {"type": "choice", "choice": value, "confidence": 0.95}


def local_turn():
    dag = ConversationDAG("qq:GroupMessage:1")
    dag.add_message("old", "alice", "我想周末读完小说", timestamp=500)
    dag.add_message("peer", "bob", "与当前发言者无关", timestamp=700)
    dag.add_message("expired", "alice", "过期", timestamp=1)
    current = dag.add_message("current", "alice", "我刚才说想做什么", timestamp=2000)
    visible = frozenset(dag.nodes)
    dag.add_message("late", "alice", "后到内容", timestamp=1999)
    turn = TurnContext(
        "qq:GroupMessage:1",
        "alice",
        current.text,
        (MessageSnapshot("current", "alice", current.text, timestamp=2000),),
        (),
        0,
        0,
        2000,
        False,
        bot_id="bot",
        snapshot_at=2000,
        visible_node_ids=visible,
    )
    runtime = SimpleNamespace(dag=dag, sent_id_set=set())
    plugin = SimpleNamespace(persona_engine=SimpleNamespace(valid_async=AsyncMock(return_value=True)), narrative=None)
    return plugin, runtime, ModelTurn(turn, (SimpleNamespace(_extras={}),), {}, False)


def test_setting_defaults_off_and_second_judgement_has_no_lookup_choices():
    assert not parse_runtime_config({})[0].context_completion_enabled
    assert parse_runtime_config({"context_completion_enabled": True})[0].context_completion_enabled
    _, _, item = local_turn()
    assert "context_need" not in build_questions(item.context)
    assert set(build_questions(item.context, context_completion=True)["context_need"]["criteria"]) == {
        "ready",
        "clarify",
        "recent",
        "topic",
        "memory",
    }
    assert set(
        build_questions(item.context, context_completion=True, context_completed=True)["context_need"]["criteria"]
    ) == {"ready", "clarify"}


@pytest.mark.asyncio
async def test_scoped_local_lookup_is_immutable_and_once_only():
    plugin, runtime, item = local_turn()
    result = await complete_context(plugin, runtime, item, "recent")
    assert [m.message_id for m in result.context.background] == ["old"]
    assert not item.context.background and not item.observations
    assert result.observations["context_completion"]["status"] == "added"
    assert await complete_context(plugin, runtime, result, "memory") is result


@pytest.mark.asyncio
async def test_timeout_is_bounded_without_retry(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import context_completion as module

    plugin, runtime, item = local_turn()

    async def slow(*args):
        await asyncio.sleep(1)

    plugin.narrative = SimpleNamespace(supplement_context=AsyncMock(side_effect=slow))
    item = replace(item, narrative_bundle=object())
    monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.02)
    result = await complete_context(plugin, runtime, item, "memory")
    assert result.observations["context_completion"]["status"] == "timeout"
    assert plugin.narrative.supplement_context.await_count == 1


@pytest.mark.asyncio
async def test_local_sources_pass_existing_boundary_filter(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import member_boundary

    plugin, runtime, item = local_turn()
    monkeypatch.setattr(
        member_boundary, "filter_frames", lambda *a, **k: ([], SimpleNamespace(available=True, disabled=False))
    )
    result = await complete_context(plugin, runtime, item, "recent")
    assert not result.context.background


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["valid", "wrong_scope", "new_revision", "blocked", "empty", "wrong_atom_scope"])
async def test_public_memory_validation_preserves_original_events_and_history(tmp_path, monkeypatch, state):
    atom = {
        "atom_id": "a",
        "kind": "user_claim",
        "status": "active",
        "claim": "来源者说过喜欢小说",
        "scope": {"scope_key": _narrative_scope_key(SCOPE)},
        "source_refs": [{"material_id": "m1", "source_event_id": "e1", "author_kind": "user"}],
    }
    view = {
        "schema_version": 1,
        "available": True,
        "scope_key": _narrative_scope_key(SCOPE),
        "purpose": "reply",
        "card_version": "card",
        "correction_revision": 1,
        "revocations": [],
        "history_projection": {},
        "atoms": [atom],
        "slots": {"historical_claims": [atom]},
    }
    old = copy.deepcopy(view)
    old.update(atoms=[], slots={}, history_projection={"original": "unchanged"})
    payload = {
        "scope": SCOPE,
        "purpose": "reply",
        "effective_core_version": "card",
        "captured_at": 1000,
        "evidence_view": old,
        "context_items": [],
        "events": [{"event_id": "original"}],
        "host_history_snapshot": {"rows": [{"role": "user", "content": "original"}]},
    }
    bundle = TurnContextBundle("turn", json.dumps(payload))
    if state == "wrong_scope":
        view["scope_key"] = "another-session"
    if state == "new_revision":
        view["correction_revision"] = 2
    if state == "empty":
        view.update(atoms=[], slots={})
    if state == "wrong_atom_scope":
        view["atoms"][0]["scope"]["scope_key"] = "another-session"

    class Memory:
        async def recall_context(self, *args):
            return {"evidence_view": view}

        def peek_evidence_revision(self, scope):
            return {
                "schema_version": 1,
                "available": True,
                "scope_key": _narrative_scope_key(SCOPE),
                "correction_revision": 1,
            }

    manager = coordinator(tmp_path, {"livingmemory": Memory()})
    if state == "blocked":
        from astrbot_plugin_chat_dynamics.core import member_boundary

        monkeypatch.setattr(
            member_boundary,
            "filter_material",
            lambda *a, **k: ({"context_items": []}, SimpleNamespace(available=True, disabled=False)),
        )
    item = SimpleNamespace(narrative_bundle=bundle, events=(SimpleNamespace(_extras={}),))
    result, status = await manager.supplement_context(item, "有来源的查询")
    assert (
        status
        == {
            "valid": "added",
            "wrong_scope": "invalid",
            "new_revision": "stale",
            "blocked": "empty",
            "empty": "empty",
            "wrong_atom_scope": "empty",
        }[state]
    )
    assert bundle.wire() == payload
    assert result.wire()["events"] == payload["events"]
    assert result.wire()["host_history_snapshot"] == payload["host_history_snapshot"]
    assert manager.journal.pending() == []
    if state == "valid":
        assert result.wire()["evidence_view"]["atoms"][0]["atom_id"] == "a"


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["ready", "clarify", "recent"])
async def test_model_fetches_before_reply_and_never_loops(jev_plugin, second):
    p, bridge = jev_plugin
    await p.save_config_values({"context_completion_enabled": True})
    event = MockEvent("我刚才说想做什么来着", message_id="current", components=[At("bot_42")])
    runtime = p._get_or_create_runtime(
        event.unified_msg_origin, group_id=event.get_group_id(), umo=event.unified_msg_origin
    )
    runtime.bot_id = "bot_42"
    runtime.dag.add_message(
        "missing", event.get_sender_id(), "我打算周末读完小说", timestamp=p.time_service.time() - 600
    )
    calls = []

    async def evaluate(**kwargs):
        calls.append(kwargs)
        return answers(action="reply", context_need=option("recent" if len(calls) == 1 else second))

    p.jev.evaluate = evaluate
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(calls) == 2 and len(bridge.requests) == 1 and event.replies_sent
        assert "missing" not in {m["message_id"] for m in calls[0]["state"]["conversation"]["background"]}
        assert "missing" in {m["message_id"] for m in calls[1]["state"]["conversation"]["background"]}
        assert calls[1]["state"]["observations"]["context_completion"]["attempted"]
        assert set(calls[1]["questions"]["context_need"]["criteria"]) == {"ready", "clarify"}
        assert bridge.requests[-1][0]["response_plan"]["action"] == ("reply" if second == "ready" else "clarify")
        assert p._metrics["context_completion_attempted"] == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,need,reason,expected_calls",
    [
        (False, "recent", "addressed_question", 0),
        (True, "ready", "addressed_question", 1),
        (True, "memory", "other_recipient", 1),
        (True, "memory", "boundary_or_sensitive", 1),
    ],
)
async def test_ready_disabled_and_boundary_cases_do_not_trigger_lookup(
    jev_plugin, enabled, need, reason, expected_calls
):
    p, bridge = jev_plugin
    await p.save_config_values({"context_completion_enabled": enabled})
    p.jev.payload = answers(context_need=option(need), reason=reason)
    event = MockEvent("你好", message_id="current", components=[At("bot_42")])
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert len(p.jev.calls) == expected_calls
        assert p._metrics.get("context_completion_attempted", 0) == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_invalidated_turn_cannot_use_supplement():
    from astrbot_plugin_chat_dynamics.core import member_boundary

    plugin, runtime, item = local_turn()
    plugin.persona_engine.valid_async = AsyncMock(side_effect=[True, False])
    with pytest.raises(member_boundary.BoundaryCancelled):
        await complete_context(plugin, runtime, item, "recent")
