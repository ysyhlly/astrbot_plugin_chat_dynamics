"""Cross-plugin ownership, persistence and immutable state regressions."""

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.narrative import NarrativeCoordinator, TurnContextBundle, MANAGED_KEY
from astrbot_plugin_chat_dynamics.core.narrative_store import NarrativeJournal
from astrbot_plugin_chat_dynamics.core.jev_decision import build_questions, build_state, decision_from_answers
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, PersonaSnapshot, TurnContext
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn

SCOPE = {"platform": "qq", "umo": "qq:GroupMessage:1", "persona_id": "anan", "instance_id": "anan"}


def envelope(event_id="in:1", text="hello"):
    return {"schema_version": 1, "event_id": event_id, "scope": SCOPE, "text": text,
            "type": "message.received", "source_ids": ["1"], "fact_kind": "observation", "data": {}}


def turn(wake="ambient"):
    return TurnContext(SCOPE["umo"], "u1", "hello", (MessageSnapshot("m1", "u1", "hello"),), (),
                       1, 0, 1.0, wake == "at", wake_kind=wake)


def coordinator(tmp_path, providers=None):
    providers = providers or {}
    cfg = SimpleNamespace(narrative_enabled=True, narrative_sessions=frozenset([SCOPE["umo"]]),
                          narrative_outreach_enabled=False)
    plugin = SimpleNamespace(_runtime_config=cfg, _shutting_down=False, shadow_mode=False,
                             integrations=SimpleNamespace(narrative_provider=lambda name: providers.get(name)),
                             time_service=SimpleNamespace(wall_time=lambda: 10000.0))
    result = NarrativeCoordinator(plugin, tmp_path / "journal.sqlite3")
    result.start = lambda: None
    return result


def test_notification_ack_is_durable_and_duplicate_does_not_reopen(tmp_path):
    journal = NarrativeJournal(tmp_path / "j.db")
    journal.enqueue(envelope(), ("life", "memory"), 1)
    journal.ack("in:1", "life")
    journal.enqueue(envelope(text="replacement"), ("life", "memory"), 2)
    assert journal.pending()[0][0]["text"] == "hello"
    assert journal.pending()[0][1] == ["memory"]
    NarrativeJournal(tmp_path / "j.db").ack("in:1", "memory")
    assert journal.pending() == []


def test_crashed_send_is_uncertain_not_resendable(tmp_path):
    journal = NarrativeJournal(tmp_path / "j.db")
    intent = {"intent_id": "i1", "scope": SCOPE}
    journal.add_intent(intent)
    journal.begin(SCOPE, "t1", 0, 9000, "i1")
    restored = NarrativeJournal(tmp_path / "j.db")
    assert restored.intents()[0][1] == "uncertain"
    assert restored.snapshot(SCOPE["umo"])["receipts"][0]["status"] == "uncertain"
    assert restored.quota(SCOPE["umo"], 0) == (1, 9000)


def test_bundle_cannot_be_mutated_and_drops_optional_background_first():
    payload = {"scope": SCOPE, "snapshot": {"revision": 3, "activity": "writing", "availability": "busy",
               "commitments": [], "summary": "plain"}, "context_items": [{"text": "x" * 1000} for _ in range(10)]}
    bundle = TurnContextBundle("t", json.dumps(payload))
    mutable = bundle.wire()
    mutable["snapshot"]["activity"] = "sleep"
    assert bundle.snapshot["activity"] == "writing"
    context = bundle.decision_context()
    assert len(json.dumps(context, ensure_ascii=False)) <= 1500
    assert context["availability"] == "busy"


@pytest.mark.asyncio
async def test_prepare_ignores_cross_group_profiles_and_marks_empty_sources(tmp_path):
    class World:
        async def get_character_context(self, scope, event):
            return {"schema_version": 1, "available": True, "scope": SCOPE, "persona_id": "anan", "fingerprint": "a",
                    "core_prompt": "core", "group_rules": "group"}

    class Life:
        async def get_snapshot(self, scope):
            return {"schema_version": 1, "managed": True, "revision": 1, "scope": scope}

    class Portrait:
        async def get_profiles(self, scope, participants):
            return {"items": [{"source_id": "p1", "scope": dict(SCOPE, umo="qq:GroupMessage:2"), "text": "private"}]}

    manager = coordinator(tmp_path, {"worldbook": World(), "dynamic_life_state": Life(), "portrayal": Portrait()})
    event = SimpleNamespace(_extras={})
    item = ModelTurn(turn(), (event,), {}, False)
    bundle = await manager.prepare(SimpleNamespace(umo=SCOPE["umo"]), item, PersonaSnapshot("host", "cid", "anan", "persona"))
    assert bundle.wire()["context_items"] == []
    assert event._extras[MANAGED_KEY]["completed"]["livingmemory"] is True
    assert manager.journal.pending()[0][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_disabled_character_cannot_fall_back_to_host_persona(tmp_path):
    class World:
        async def get_character_context(self, *args):
            return {"schema_version": 1, "disabled": True}
    manager = coordinator(tmp_path, {"worldbook": World()})
    bundle = await manager.prepare(SimpleNamespace(umo=SCOPE["umo"]), ModelTurn(turn(), (SimpleNamespace(_extras={}),), {}, False),
                                   PersonaSnapshot("host", "cid", "anan", "persona"))
    assert bundle is None
    assert manager.journal.pending() == []


@pytest.mark.asyncio
async def test_unloaded_provider_response_is_discarded(tmp_path):
    class Source:
        async def read(self):
            providers.clear()
            return {"schema_version": 1}
    providers = {"source": Source()}
    manager = coordinator(tmp_path, providers)
    assert await manager.call("source", "read") is None


@pytest.mark.parametrize("status,text", [("success", "sent fragment"), ("failure", ""), ("uncertain", "")])
def test_receipts_never_archive_unsent_text(tmp_path, status, text):
    manager = coordinator(tmp_path)
    event = SimpleNamespace(_extras={MANAGED_KEY: {"scope": SCOPE, "turn_id": "t1"}})
    attempt = manager.begin_send(event, "full draft")
    result = None if status == "uncertain" else SimpleNamespace(success=status == "success", delivery_uncertain=False, message_id="m")
    manager.finish_send(event, attempt, "sent fragment", result)
    receipt = manager.journal.pending()[0][0]
    assert receipt["type"] == "delivery." + status
    assert receipt["text"] == text


@pytest.mark.asyncio
async def test_notification_optional_provider_retries_without_duplicate_recipient(tmp_path):
    class Life:
        async def ingest_events(self, events):
            return {"schema_version": 1, "accepted": 1}
    manager = coordinator(tmp_path, {"dynamic_life_state": Life()})
    manager.journal.enqueue(envelope(), ("dynamic_life_state", "livingmemory"), 1)
    await manager.dispatch()
    await manager.dispatch()
    assert manager.journal.pending()[0][1] == ["livingmemory"]


@pytest.mark.asyncio
async def test_motive_cannot_change_destination_or_supply_precomputed_message(tmp_path):
    manager = coordinator(tmp_path)
    manager.plugin._runtime_config.narrative_outreach_enabled = True
    manager.plugin._runtime_config.narrative_outreach_sessions = frozenset([SCOPE["umo"]])
    valid = {"intent_id": "i1", "scope": SCOPE, "target_umo": SCOPE["umo"]}
    assert (await manager.submit_intent(dict(valid, target_umo="qq:GroupMessage:2")))["accepted"] is False
    assert (await manager.submit_intent(dict(valid, text="send me")))["accepted"] is False
    assert (await manager.submit_intent(valid))["accepted"] is True
    assert (await manager.submit_intent(valid))["accepted"] is False


def test_managed_at_cannot_be_silenced_by_negative_decision():
    answers = {key: {"type": "choice", "choice": choice, "confidence": .9}
               for key, choice in [("action", "ignore"), ("state", "observing"), ("length", "brief"), ("reason", "boundary_or_sensitive")]}
    assert decision_from_answers(turn("at"), answers, narrative=True).action == "acknowledge"
    assert "impact" in build_questions(turn(), narrative=True)
    assert "impact" not in build_questions(turn())


def test_state_budgets_include_character_constraints():
    state = build_state(turn(), character_state={"activity": "writing", "availability": "busy"})
    assert state["character_state"]["availability"] == "busy"


def test_explicit_all_group_switch_never_enables_private_chats(tmp_path):
    manager = coordinator(tmp_path)
    manager.plugin._runtime_config.narrative_all_groups = True
    assert manager.enabled("new-adapter:GroupMessage:new-group")
    assert not manager.enabled("new-adapter:FriendMessage:user")
    assert not manager.enabled("malformed")
    manager.plugin._runtime_config.narrative_enabled = False
    assert not manager.enabled(SCOPE["umo"])


@pytest.mark.asyncio
async def test_important_advance_uses_turn_even_after_event_notifications_acked(tmp_path):
    class Life:
        async def advance(self, events, revision):
            assert events[0]["text"] == "hello"
            return {"snapshot": {"managed": True, "revision": 2}}
    manager = coordinator(tmp_path, {"dynamic_life_state": Life()})
    bundle = TurnContextBundle("t1", json.dumps({"scope": SCOPE, "snapshot": {"revision": 1}, "captured_at": 100,
                                                "character": {}, "completed": {}, "events": [envelope()]}))
    item = ModelTurn(turn(), (SimpleNamespace(_extras={}),), {}, False)
    revised = await manager.advance_important(item, bundle, "commitment")
    assert revised.snapshot["revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("availability,wake", [("asleep", True), ("busy", True), ("asleep", False), ("asleep", "name"), ("asleep", "subject"), ("asleep", "overlap")])
async def test_managed_native_turn_sees_same_state_and_at_gets_real_receipt(tmp_path, monkeypatch, availability, wake):
    from .test_plugin_lifecycle import MockEvent, At
    from .test_jev_decision_layer import JevDouble, answers
    from .test_persona_model import BridgeDouble, flush, drain
    from .test_plugin_lifecycle import _plugin
    from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge

    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    event = MockEvent("安安，帮我看看", message_id="n1", components=[At("bot_42")] if wake is True else [])
    if wake == "subject":
        event = MockEvent("安安昨天说过这件事", message_id="n1")
    elif wake == "overlap":
        event = MockEvent("晚安安", message_id="n1", is_at_or_wake_command=True)
    plugin = _plugin({"bot_names": ["安安"] if isinstance(wake, str) else ["小助手"], "narrative_enabled": True, "narrative_sessions": [event.unified_msg_origin],
                      "daily_rhythm_enabled": False, "decision_mode": "persona_model", "base_thinking_delay": 0,
                      "debounce_base_cooldown": 10})
    plugin.narrative = NarrativeCoordinator(plugin, tmp_path / "e2e.db")
    plugin.narrative.start = lambda: None
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge
    plugin.jev = JevDouble(answers())

    class World:
        async def get_character_context(self, scope, event):
            return {"schema_version": 1, "available": True, "scope": scope, "persona_id": scope["persona_id"],
                    "fingerprint": "stable", "core_prompt": "安安固定核心", "group_rules": "可以拒绝不愿做的事"}
    class Life:
        async def get_snapshot(self, scope):
            return {"schema_version": 1, "managed": True, "scope": scope, "revision": 1,
                    "availability": availability, "activity": "写作", "commitments": []}
    providers = {"worldbook": World(), "dynamic_life_state": Life()}
    plugin.integrations.narrative_provider = lambda name: providers.get(name)
    try:
        await plugin.on_group_message(event)
        await flush(plugin, event)
        await drain(plugin)
        assert plugin.jev.calls[0]["state"]["character_state"]["availability"] == availability
        if wake is True or wake == "name":
            assert event.replies_sent
            payload = bridge.requests[0][0]
            assert payload["character_state"]["availability"] == availability
            assert payload["response_plan"]["length"] == "brief"
            assert any(row["type"] == "delivery.success" and row["text"] == event.replies_sent[0]
                       for row, _ in plugin.narrative.journal.pending())
        else:
            assert event.replies_sent == []
    finally:
        await plugin.terminate()
