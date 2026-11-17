"""Cross-plugin ownership, persistence and immutable state regressions."""

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.narrative import NarrativeCoordinator, TurnContextBundle, MANAGED_KEY
from astrbot_plugin_chat_dynamics.core.narrative_store import NarrativeJournal
from astrbot_plugin_chat_dynamics.core.jev_decision import build_questions, build_state, decision_from_answers
from astrbot_plugin_chat_dynamics.core.pipeline_telemetry import PipelineTelemetry
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, PersonaSnapshot, TurnContext
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
from astrbot_plugin_chat_dynamics.core import narrative, persona_engine
from astrbot_plugin_chat_dynamics.core.reply_failure import unavailable_reply

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


class CharacterProvider:
    async def get_character_context(self, scope, event):
        return {"schema_version": 1, "scope": scope, "fingerprint": "character", "core_prompt": "private-prompt"}


class LifeProvider:
    async def get_snapshot(self, scope):
        return {"schema_version": 1, "scope": scope, "managed": True, "revision": 1}


async def prepare(manager, *, lightweight=False, context=None):
    item = ModelTurn(context or turn(), (SimpleNamespace(_extras={}),), {}, False)
    bundle = await manager.prepare(SimpleNamespace(umo=SCOPE["umo"]), item,
                                   PersonaSnapshot("private-host", "", "anan", "persona"), lightweight=lightweight)
    return bundle, item


@pytest.fixture
def prepare_logs(monkeypatch, caplog):
    logger = logging.getLogger(__name__)
    monkeypatch.setattr(narrative, "logger", logger)
    monkeypatch.setattr(persona_engine, "logger", logger)
    caplog.set_level(logging.INFO, logger.name)
    return caplog


@pytest.mark.asyncio
@pytest.mark.parametrize("character,snapshot,stage,reason", [
    ({"schema_version": 1, "available": False, "reason": "no_character"}, None, "character", "missing"),
    ([], None, "character", "invalid"),
    ({"schema_version": 1, "scope": dict(SCOPE, umo="qq:GroupMessage:2")}, None, "character", "scope_changed"),
    ({"schema_version": 1, "scope": SCOPE}, {"schema_version": 1, "managed": False}, "snapshot", "unmanaged"),
    ({"schema_version": 1, "scope": SCOPE}, {"schema_version": 2, "managed": True}, "snapshot", "invalid"),
    ({"schema_version": 1, "scope": SCOPE}, [], "snapshot", "invalid"),
])
async def test_prepare_early_exit_reports_actual_reason(tmp_path, prepare_logs, character, snapshot, stage, reason):
    manager = coordinator(tmp_path, {
        "worldbook": SimpleNamespace(get_character_context=lambda *args: character),
        "dynamic_life_state": SimpleNamespace(get_snapshot=lambda scope: snapshot),
    })
    bundle, item = await prepare(manager, lightweight=True)
    assert bundle is None
    assert MANAGED_KEY not in item.events[-1]._extras and not manager.journal.pending()
    turn_id = PipelineTelemetry.turn_id(item.context)
    session_id = hashlib.sha256(item.context.session_key.encode()).hexdigest()[:32]
    assert (f"Narrative prepare unavailable turn_id={turn_id} session_id={session_id} "
            f"phase=decision stage={stage} reason={reason} elapsed_ms=") in prepare_logs.text
    assert SCOPE["umo"] not in prepare_logs.text
    assert "hello" not in prepare_logs.text


@pytest.mark.asyncio
async def test_disabled_lightweight_prepare_is_quiet(tmp_path, prepare_logs):
    manager = coordinator(tmp_path)
    manager.plugin._runtime_config.narrative_enabled = False
    bundle, _ = await prepare(manager, lightweight=True)
    assert bundle is None and not prepare_logs.records
    bundle, _ = await prepare(manager)
    assert bundle is None
    assert "phase=reply stage=enabled reason=unmanaged" in prepare_logs.text


@pytest.mark.asyncio
@pytest.mark.parametrize("character,snapshot,stage,error", [
    (["private-character"], None, "character", AttributeError),
    ({"schema_version": 1}, None, "character", KeyError),
    ({"schema_version": 1, "scope": "private-scope"}, None, "character", AttributeError),
    ({"schema_version": 1, "scope": SCOPE}, ["private-snapshot"], "snapshot", AttributeError),
])
async def test_malformed_lightweight_prepare_preserves_failure(tmp_path, prepare_logs, character, snapshot, stage, error):
    manager = coordinator(tmp_path, {
        "worldbook": SimpleNamespace(get_character_context=lambda *args: character),
        "dynamic_life_state": SimpleNamespace(get_snapshot=lambda scope: snapshot),
    })
    with pytest.raises(error):
        await prepare(manager, lightweight=True)
    assert not manager.journal.pending()
    assert f"phase=decision stage={stage} reason=invalid" in prepare_logs.text
    assert "private-" not in prepare_logs.text


@pytest.mark.asyncio
async def test_prepare_life_timeout_reports_budget_and_discards_reply(tmp_path, prepare_logs):
    cancelled = asyncio.Event()

    class SlowLife:
        async def get_snapshot(self, scope):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    manager = coordinator(tmp_path, {"worldbook": CharacterProvider(), "dynamic_life_state": SlowLife()})
    assert manager._rpc_timeout(None) == 5.0
    manager.plugin._runtime_config.narrative_rpc_timeout_seconds = .02
    bundle, item = await prepare(manager)
    assert bundle is None and cancelled.is_set()
    assert MANAGED_KEY not in item.events[-1]._extras and not manager.journal.pending()
    turn_id = PipelineTelemetry.turn_id(item.context)
    assert f"Narrative prepare RPC unavailable turn_id={turn_id} " in prepare_logs.text
    assert "phase=reply provider=dynamic_life_state method=get_snapshot reason=timeout" in prepare_logs.text
    assert "budget_s=0.02" in prepare_logs.text
    assert "phase=reply stage=snapshot reason=timeout elapsed_ms=" in prepare_logs.text
    assert "private-prompt" not in prepare_logs.text


@pytest.mark.asyncio
async def test_shared_memory_rpc_failure_reports_both_waiters_and_preserves_prepares(tmp_path, prepare_logs):
    entered, release = asyncio.Event(), asyncio.Event()
    invocations = 0

    class Memory:
        async def peek_evidence_revision(self, scope):
            return {"available": True, "correction_revision": 1}

        async def recall_context(self, *args):
            nonlocal invocations
            invocations += 1
            entered.set()
            await release.wait()
            raise RuntimeError("private-recall-error")

    manager = coordinator(tmp_path, {"worldbook": CharacterProvider(), "dynamic_life_state": LifeProvider(),
                                     "livingmemory": Memory()})
    creator = asyncio.create_task(prepare(manager))
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        other = replace(turn(), messages=(replace(turn().messages[0], message_id="second-message"),))
        waiter = asyncio.create_task(prepare(manager, context=other))

        async def wait_for_join():
            while not manager.call_diagnostics()["shared_read_joins"]:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_join(), 2)
        release.set()
        prepares = await asyncio.gather(creator, waiter)
    finally:
        release.set()
        await asyncio.gather(*(task for task in (creator, waiter) if task is not None), return_exceptions=True)
    assert invocations == 1
    rpc_logs = [row.message for row in prepare_logs.records if "method=recall_context reason=unavailable" in row.message]
    assert len(rpc_logs) == 2
    for bundle, item in prepares:
        assert bundle is not None and bundle.wire()["availability"]["livingmemory"] is False
        turn_id = PipelineTelemetry.turn_id(item.context)
        assert any(f"turn_id={turn_id} " in row and "phase=reply " in row for row in rpc_logs)
    assert "private-recall-error" not in prepare_logs.text and SCOPE["umo"] not in prepare_logs.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["boundary_state", "boundary_policy", "boundary_material"])
async def test_prepare_boundary_unavailability_stays_fail_closed(tmp_path, prepare_logs, stage):

    class Boundary:
        def resolve_context(self, **kwargs):
            return {"ready": True, "owner": {"self_id": "bot"}}

        def peek_policy(self, context, uid):
            if stage == "boundary_policy":
                raise RuntimeError("private-boundary-error")
            return {}

    class Memory:
        async def recall_context(self, *args):
            providers.pop("autonomous_blocking")
            return {"items": []}

    providers = {"worldbook": CharacterProvider(), "dynamic_life_state": LifeProvider(), "livingmemory": Memory()}
    if stage != "boundary_state":
        providers["autonomous_blocking"] = Boundary()
    manager = coordinator(tmp_path, providers)
    manager.plugin._runtime_config.autonomous_blocking_required = True
    bundle, item = await prepare(manager)
    assert bundle is None
    assert MANAGED_KEY not in item.events[-1]._extras and not manager.journal.pending()
    assert f"phase=reply stage={stage} reason=boundary_unavailable elapsed_ms=" in prepare_logs.text
    assert "private-boundary-error" not in prepare_logs.text


@pytest.mark.asyncio
async def test_pipeline_only_marks_none_unavailable_when_requested():
    telemetry = PipelineTelemetry()

    async def nothing():
        return None

    async def false():
        return False

    assert await telemetry.run("turn_total", turn(), nothing()) is None
    assert await telemetry.run("prepare_reply", turn(), nothing(), none_is_unavailable=True) is None
    assert await telemetry.run("prepare_reply", turn(), false(), none_is_unavailable=True) is False
    snapshot = telemetry.snapshot()
    assert [row["outcome"] for row in snapshot["recent"]] == ["success", "unavailable", "success"]
    assert snapshot["stages"]["prepare_reply"]["unavailable"] == 1
    assert snapshot["stages"]["prepare_reply"]["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed,mandatory", [(False, True), (True, True), (True, False)])
async def test_pipeline_prepare_failure_preserves_fallback_contract(tmp_path, monkeypatch, prepare_logs,
                                                                    malformed, mandatory):
    from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
    from .test_jev_decision_layer import JevDouble, answers
    from .test_persona_model import BridgeDouble, drain, flush
    from .test_plugin_lifecycle import At, MockEvent, _plugin

    class Life(LifeProvider):
        calls = 0

        async def get_snapshot(self, scope):
            self.calls += 1
            if self.calls == 1:
                return await super().get_snapshot(scope)
            await asyncio.Future()

    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    event = MockEvent("安安，帮我看看", message_id="prepare-failure", components=[At("bot_42")] if mandatory else [])
    plugin = _plugin({"narrative_enabled": True, "narrative_sessions": [event.unified_msg_origin],
                      "daily_rhythm_enabled": False, "decision_mode": "persona_model", "base_thinking_delay": 0,
                      "debounce_base_cooldown": 10, "narrative_rpc_timeout_seconds": 1})
    plugin.narrative = NarrativeCoordinator(plugin, tmp_path / "prepare-timeout.db")
    plugin.narrative.start = lambda: None
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge
    plugin.jev = JevDouble(answers())
    life = Life()
    world = SimpleNamespace(get_character_context=lambda *args: ["private-character"]) if malformed else CharacterProvider()
    providers = {"worldbook": world, "dynamic_life_state": life}
    plugin.integrations.narrative_provider = providers.get
    try:
        await plugin.on_group_message(event)
        await flush(plugin, event)
        await drain(plugin)
        assert not bridge.requests
        telemetry = plugin.persona_engine.telemetry.snapshot()["recent"]
        decision = next(row for row in telemetry if row["stage"] == "prepare_decision")
        if malformed:
            assert life.calls == 0 and not plugin.jev.calls
            assert decision["outcome"] == "failed"
            assert all(row["stage"] != "prepare_reply" for row in telemetry)
            reason, detail = "agent_failed", "phase=decision stage=character reason=invalid"
        else:
            assert life.calls == 2 and plugin.jev.calls
            reply = next(row for row in telemetry if row["stage"] == "prepare_reply")
            assert decision["outcome"] == "success" and reply["outcome"] == "unavailable"
            assert reply["turn_id"] == decision["turn_id"]
            reason, detail = "narrative_prepare_unavailable", "phase=reply stage=snapshot reason=timeout"
        assert event.replies_sent == ([unavailable_reply(reason)] if mandatory else [])
        trace = next(row.message for row in prepare_logs.records if detail in row.message)
        warning = next(row.message for row in prepare_logs.records if f"reason={reason} " in row.message
                       and "reply generation failed" in row.message)
        assert f"turn_id={decision['turn_id']} " in trace
        assert f"pipeline_turn_id={decision['turn_id']}" in warning
    finally:
        await plugin.terminate()


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
            # The asynchronous receipt writer may already have no remaining
            # recipients. Check durable delivery, not the pending dispatch queue.
            await plugin.narrative._io.drain()
            from contextlib import closing
            with closing(plugin.narrative.journal._connect()) as db:
                receipts=[json.loads(row[0]) for row in db.execute("SELECT payload FROM events")]
            assert any(row["type"] == "delivery.success" and row["text"] == event.replies_sent[0]
                       for row in receipts)
        else:
            assert event.replies_sent == []
    finally:
        await plugin.terminate()
