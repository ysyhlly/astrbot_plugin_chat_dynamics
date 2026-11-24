"""Focused unit tests for R1 (P0 stability), R2 (P0 storage), and R3 (P1 cost/cache/latency)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import astrbot_plugin_chat_dynamics.series_panels as series_panels
from astrbot_plugin_chat_dynamics.core import member_boundary
from astrbot_plugin_chat_dynamics.core.agent_bridge import (
    AstrBotAgentBridge,
    _safe_generation_failure_fields,
    _sanitize_reply_narration,
)
from astrbot_plugin_chat_dynamics.core.discussion_reranker import DiscussionReranker
from astrbot_plugin_chat_dynamics.core.jev_decision import STATIC_STATE_KEYS, build_state
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter
from astrbot_plugin_chat_dynamics.core.narrative_store import NarrativeJournal
from astrbot_plugin_chat_dynamics.core.natural_rhythm import AfterthoughtSlot
from astrbot_plugin_chat_dynamics.core.provider_budget import (
    ProviderBudget,
    ProviderBudgetBusyError,
)
from astrbot_plugin_chat_dynamics.core.reply_failure import ModelReplyFailure
from astrbot_plugin_chat_dynamics.core.session_runtime import TopicState
from astrbot_plugin_chat_dynamics.core.systemone.provider import SystemOneProvider
from astrbot_plugin_chat_dynamics.core.topic_reranker import RerankCandidate
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from astrbot_plugin_chat_dynamics.main import _is_trivial_followup, _looks_like_reasoning_provider
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, _plugin


# ---- 1. ProviderBudget & series_panels contract ----


@pytest.mark.asyncio
async def test_provider_budget_half_open_single_probe_retry_after_and_circuit_helper():
    noisy_calls = []
    ctx = SimpleNamespace(get_provider_by_id=lambda pid: (noisy_calls.append(pid), None)[1])
    budget = ProviderBudget(
        capacity=4,
        reply_reserve=1,
        circuit_breaker_threshold=2,
        circuit_breaker_base_cooldown=1.0,
        circuit_breaker_max_cooldown=10.0,
    )
    ctx._chat_dynamics_provider_budget = budget

    # Closed circuit -> 0.0 and never calls get_provider_by_id
    assert series_panels.provider_circuit_remaining(ctx, "p-hb") == 0.0
    assert series_panels.provider_circuit_remaining(None, "p-hb") == 0.0
    assert noisy_calls == []

    # Trip breaker via rate limit (weight 2 >= threshold 2)
    async def fail_429():
        raise RuntimeError("HTTP 429 rate limit")

    with pytest.raises(RuntimeError):
        await budget.run("p-hb", "background", fail_429)

    rem = series_panels.provider_circuit_remaining(ctx, "p-hb")
    assert 0.0 < rem <= 1.0
    assert noisy_calls == []

    # Open circuit raises structured retry_after and forwards it via series_llm_call
    with pytest.raises(series_panels.ProviderBudgetBusyError) as exc_info:
        await series_panels.series_llm_call(
            ctx, "p-hb", "astrbot_plugin_dynamics_learning", "review", lambda: "never"
        )
    assert exc_info.value.reason == "circuit_open"
    assert exc_info.value.retry_after > 0.0

    # Expire cooldown -> half-open allows ONLY 1 probe concurrently
    fails, _until, reason = budget._circuit["p-hb"]
    budget._circuit["p-hb"] = (fails, time.monotonic() - 0.1, reason)
    assert series_panels.provider_circuit_remaining(ctx, "p-hb") == 0.0

    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()

    async def probe_call():
        probe_entered.set()
        await probe_release.wait()
        return "probe-ok"

    probe_task = asyncio.create_task(budget.run("p-hb", "background", probe_call))
    await asyncio.wait_for(probe_entered.wait(), 2.0)

    # While probe is in flight, pre-flight reports >0 and second caller is rejected
    assert series_panels.provider_circuit_remaining(ctx, "p-hb") > 0.0
    with pytest.raises(ProviderBudgetBusyError) as second_exc:
        await budget.run("p-hb", "background", lambda: "stampede")
    assert second_exc.value.reason == "circuit_open"
    assert second_exc.value.retry_after > 0.0

    # Probe succeeds -> circuit closes
    probe_release.set()
    assert await probe_task == "probe-ok"
    assert series_panels.provider_circuit_remaining(ctx, "p-hb") == 0.0
    assert "p-hb" not in budget._circuit


@pytest.mark.asyncio
async def test_provider_budget_excludes_local_and_boundary_errors_from_circuit():
    budget = ProviderBudget(
        capacity=3,
        reply_reserve=1,
        circuit_breaker_threshold=2,
        circuit_breaker_base_cooldown=30.0,
    )

    class PersonaChanged(RuntimeError):
        pass

    for exc in (
        member_boundary.BoundaryCancelled("current_actor_unknown"),
        PersonaChanged("switched"),
        ModelReplyFailure("invalid_reply_narration"),
        ModelReplyFailure("provider_content_blocked"),
        ValueError("local validation"),
    ):
        with pytest.raises(type(exc)):
            await budget.run("p-local", "background", lambda e=exc: (_ for _ in ()).throw(e))

    # Host error response wrapping a local BoundaryCancelled also must not trip breaker
    err_resp = SimpleNamespace(role="err", completion_text="All chat models failed: BoundaryCancelled: current_actor_unknown")
    await budget.run("p-local", "background", lambda: err_resp)

    assert "p-local" not in budget._circuit
    assert budget.circuit_remaining("p-local") == 0.0


# ---- 2. Afterthought / BoundaryCancelled / Narration ----


def test_narration_sanitization_and_trace_fallback():
    # Multiline + outer parens + >118 chars is sanitized rather than failing
    long_multiline = "（第一行旁白\n第二行旁白，" + "啊" * 130 + "）"
    sanitized = _sanitize_reply_narration(long_multiline, max_chars=118, wrap=True)
    assert sanitized.startswith("（") and sanitized.endswith("）")
    assert "\n" not in sanitized
    inner = sanitized[1:-1]
    assert len(inner) <= 118
    assert inner.endswith("…")

    # Completely blank narration still produces ""
    assert _sanitize_reply_narration("   \n  （）  ", max_chars=118, wrap=True) == ""

    # Synthetic trace fallback populates turn_id / bundle_id instead of unknown
    ev = SimpleNamespace(_extras={"_chat_dynamics_trace_v1": {"turn_id": "afterthought:m1", "bundle_id": "afterthought:m1"}})
    fields = _safe_generation_failure_fields(None, RuntimeError("boom"), event=ev)
    assert fields["turn_id"] == "afterthought:m1"
    assert fields["bundle_id"] == "afterthought:m1"


@pytest.mark.asyncio
async def test_afterthought_sets_boundary_uid_trace_and_purpose(jev_plugin, monkeypatch):
    p, bridge = jev_plugin
    runtime = p._get_or_create_runtime("mock:GroupMessage:100", group_id="100", umo="mock:GroupMessage:100", bot_id="bot_42")
    purposes = []
    orig_run = p.llm.provider_budget.run

    async def spy_run(provider, purpose, invoke, **kwargs):
        purposes.append(purpose)
        return await orig_run(provider, purpose, invoke, **kwargs)

    monkeypatch.setattr(p.llm.provider_budget, "run", spy_run)

    # 1) Empty target_user_id with required boundary -> skips cleanly without calling LLM
    monkeypatch.setattr(
        member_boundary,
        "resolve_state",
        lambda host, ev: SimpleNamespace(available=True, disabled=False, required=True, service=object()),
    )
    slot_empty = AfterthoughtSlot(
        session_key=runtime.session_key,
        anchor_bot_msg_id="bot-1",
        trigger_msg_id="m-empty",
        target_user_id="",
        anchor_topic_id="",
        delivered_text="hello",
        persona_fingerprint=bridge.persona.fingerprint,
        epoch=runtime.epoch,
        private_direct=False,
        finished_at_mono=0.0,
        finished_at_wall=0.0,
        fire_at_mono=0.0,
    )
    try:
        res_skip = await p.persona_engine.execute_afterthought(runtime, slot_empty)
        assert res_skip["status"] == "skipped"
        assert "boundary_uid_missing" in res_skip["reason"]
        assert purposes == []

        # 2) Valid target_user_id -> sets boundary uid/token/trace and runs with purpose="afterthought"
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_lease(*_a, **_kw):
            yield

        monkeypatch.setattr(member_boundary, "send_lease", fake_lease)
        monkeypatch.setattr(member_boundary, "dispatch_targets_current", lambda *_a, **_kw: True)
        monkeypatch.setattr(
            member_boundary,
            "peek",
            lambda host, ev, uid: ({}, "tok-1", SimpleNamespace(available=True, disabled=False, required=True)),
        )
        captured_events = []
        orig_gen = bridge.generate

        async def capture_gen(event, *args, **kwargs):
            captured_events.append(event)
            return await orig_gen(event, *args, **kwargs)

        bridge.generate = capture_gen
        p.context.send_message = AsyncMock(return_value=SimpleNamespace(message_id="msg-after"))
        slot_ok = AfterthoughtSlot(
            session_key=runtime.session_key,
            anchor_bot_msg_id="bot-1",
            trigger_msg_id="m-ok",
            target_user_id="user_99",
            anchor_topic_id="",
            delivered_text="先发一条",
            persona_fingerprint=bridge.persona.fingerprint,
            epoch=runtime.epoch,
            private_direct=False,
            finished_at_mono=0.0,
            finished_at_wall=0.0,
            fire_at_mono=0.0,
        )
        res_ok = await p.persona_engine.execute_afterthought(runtime, slot_ok)
        assert res_ok["status"] == "sent"
        assert purposes == ["afterthought"]
        assert captured_events
        ev = captured_events[0]
        assert getattr(ev, "_chat_dynamics_boundary_uid", "") == "user_99"
        assert getattr(ev, "_chat_dynamics_boundary_token", "") == "tok-1"
        assert ev._extras["_chat_dynamics_trace_v1"]["turn_id"] == "afterthought:m-ok"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_catchup_synthetic_event_skips_when_boundary_uid_missing(jev_plugin, monkeypatch):
    p, _bridge = jev_plugin
    runtime = p._get_or_create_runtime("mock:GroupMessage:101", group_id="101", umo="mock:GroupMessage:101", bot_id="bot_42")
    runtime.dag.add_message("c1", "", "hello", timestamp=p.time_service.time())
    monkeypatch.setattr(
        member_boundary,
        "resolve_state",
        lambda host, ev: SimpleNamespace(available=True, disabled=False, required=True, service=object()),
    )
    plan_item = SimpleNamespace(
        message_ids=("c1",),
        primary_msg_id="c1",
        author_id="",
        private_direct=False,
        directed=False,
        should_quote=False,
    )
    try:
        res = await p.persona_engine.execute_catchup_item(runtime, plan_item, live_event=None)
        assert res["status"] == "skipped"
        assert "boundary_uid_missing" in res["reason"]
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_agent_bridge_reraises_boundary_cancelled_when_failed_closed(monkeypatch):
    """When _chat_dynamics_boundary_failed_closed is set, error responses must raise BoundaryCancelled."""
    ctx = SimpleNamespace(get_provider_by_id=lambda pid: SimpleNamespace(id=pid))
    bridge = AstrBotAgentBridge(ctx)
    bridge.build_config = lambda *_a, **_kw: {}
    persona = SimpleNamespace(conversation_id="c1", fingerprint="fp", persona_id="p1", prompt="p")
    bridge.current = AsyncMock(return_value=True)
    bridge.snapshot = AsyncMock(return_value=persona)

    event = MockEvent("hello", message_id="m-bc")
    event._chat_dynamics_boundary_failed_closed = True
    event._chat_dynamics_boundary_failed_closed_reason = "current_actor_unknown"
    event._extras = {}
    event.get_extra = lambda k, default=None: event._extras.get(k, default)
    event.set_extra = lambda k, v: event._extras.__setitem__(k, v)

    async def fake_reset():
        return None

    class FakeRunner:
        provider = SimpleNamespace()
        fallback_providers = []
        tool_executor = SimpleNamespace()

        async def _iter_llm_responses_with_fallback(self):
            yield SimpleNamespace(role="err", completion_text="All chat models failed: BoundaryCancelled")

        async def step(self):
            async for resp in self._iter_llm_responses_with_fallback():
                yield resp

    req = SimpleNamespace(
        prompt="hello",
        system_prompt="sys",
        contexts=[],
        conversation=SimpleNamespace(cid="c1", history="[]"),
        func_tool=None,
    )

    async def fake_build_main_agent(**_kw):
        return SimpleNamespace(provider_request=req, agent_runner=FakeRunner(), reset_coro=fake_reset())

    monkeypatch.setattr("astrbot.core.astr_main_agent.build_main_agent", fake_build_main_agent)
    with pytest.raises(member_boundary.BoundaryCancelled, match="current_actor_unknown"):
        await bridge.generate(event, (event,), "hello", persona, "prov", reply_timeout=5.0)


# ---- 3. Storage governance for narrative-delivery.sqlite3 ----


def test_narrative_journal_auto_vacuum_dedup_ttl_and_dead_letter_expiry(tmp_path):
    db_path = tmp_path / "narrative-delivery.sqlite3"
    j = NarrativeJournal(db_path)
    j.ensure_indexes()
    try:
        # 1) PRAGMA auto_vacuum == 2 (INCREMENTAL)
        with j._tx() as db:
            mode = db.execute("PRAGMA auto_vacuum").fetchone()[0]
        assert mode == 2

        # 2) Character blob deduplication across multiple bundles
        big_char = {
            "fingerprint": "char-fp-1",
            "core_prompt": "角色设定文本" * 200,
            "group_rules": "群规文本" * 100,
        }
        for i in range(5):
            payload = json.dumps(
                {
                    "scope": {"umo": "s", "persona_id": "p", "instance_id": "i", "platform": "qq"},
                    "character": big_char,
                    "context_items": [],
                    "seq": i,
                },
                ensure_ascii=False,
            )
            j.save_bundle(f"b-{i}", f"t-{i}", "s", payload, captured=100.0 + i)

        with j._tx() as db:
            blob_count = db.execute("SELECT count(*) FROM character_blobs").fetchone()[0]
            raw_arch = gzip.decompress(
                db.execute("SELECT payload FROM context_archive WHERE bundle_id='b-0'").fetchone()[0]
            )
        assert blob_count == 1
        assert b"character_blob_ref" in raw_arch
        assert b"core_prompt" not in raw_arch

        # full_bundle reconstructs full character seamlessly
        restored = j.full_bundle("b-3")
        assert restored["seq"] == 3
        assert restored["character"] == big_char

        # 3) Legacy inline character row remains 100% backwards compatible
        legacy_payload = json.dumps({"character": {"fingerprint": "legacy-fp", "core_prompt": "legacy"}, "legacy": True}).encode("utf-8")
        with j._tx(write=True) as db:
            db.execute(
                "INSERT INTO context_archive VALUES (?,?,?)",
                ("b-legacy", hashlib.sha256(legacy_payload).hexdigest(), gzip.compress(legacy_payload)),
            )
        assert j.full_bundle("b-legacy")["character"]["core_prompt"] == "legacy"

        # 4) Dead-letter unacked recipient expires after TTL so events pruning never stalls
        j.enqueue({"event_id": "ev-stale", "type": "message.received", "data": {}}, ("portrayal",), now=100.0)
        j.enqueue({"event_id": "ev-fresh", "type": "message.received", "data": {}}, ("portrayal",), now=40 * 86400)

        # First maintain at day 10 -> marks b-0..b-4 as archive_only and expires ev-stale (>7d)
        m1 = j.maintain(10 * 86400, retention_days=7, archive_retention_days=30, dead_letter_ttl_days=7)
        assert m1["archived"] == 5
        assert m1["expired_recipients"] >= 1
        assert m1["pruned_events"] >= 1
        assert m1["vacuumed"] is True
        assert m1["checkpointed"] is True

        # At day 41 -> archive_only bundles older than 30 days are pruned from bundles + context_archive
        m2 = j.maintain(41 * 86400, retention_days=7, archive_retention_days=30, dead_letter_ttl_days=7)
        assert m2["pruned_archives"] == 5
        assert m2["pruned_bundles"] == 5
        with j._tx() as db:
            assert db.execute("SELECT count(*) FROM bundles").fetchone()[0] == 0
            # Fresh event still pending
            ev_ids = [r[0] for r in db.execute("SELECT event_id FROM events").fetchall()]
            assert "ev-stale" not in ev_ids
            assert "ev-fresh" in ev_ids
    finally:
        j.close()


# ---- 4. P1 Cost, Cache & Latency ----


@pytest.mark.asyncio
async def test_systemone_provider_parses_full_usage_including_cached_tokens(monkeypatch):
    prov = SystemOneProvider(
        {"api_base": "https://example.invalid/v1", "key": ["k"], "model": "jev-latest", "timeout": 10},
        {},
    )
    envelope = {
        "answers": {"join": {"type": "noul", "noul": 0.8}},
        "usage": {
            "input_tokens": 2000,
            "output_tokens": 50,
            "total_tokens": 2050,
            "input_tokens_details": {"cached_tokens": 1500, "cache_write_tokens": 100},
        },
    }
    monkeypatch.setattr(
        "astrbot_plugin_chat_dynamics.core.systemone.provider.request_json",
        AsyncMock(return_value=envelope),
    )
    try:
        await prov.systemone_evaluate(
            state="hello",
            questions={"join": {"type": "noul", "instructions": "reply?"}},
        )
        assert prov.last_decision_usage["reported"] is True
        assert prov.last_decision_usage["input_tokens"] == 2000
        assert prov.last_decision_usage["output_tokens"] == 50
        assert prov.last_decision_usage["cached_tokens"] == 1500
        assert prov.last_decision_usage["cache_write_tokens"] == 100
    finally:
        await prov.terminate()


def test_jev_state_places_learning_context_after_static_keys():
    turn = TurnContext(
        "room:1",
        "u1",
        "今天吃什么",
        (MessageSnapshot("m1", "u1", "今天吃什么"),),
        (),
        0,
        0,
        100.0,
        False,
        bot_id="bot",
    )
    s1 = build_state(
        turn,
        persona_prompt="安安人设",
        decision_prompt="固定决策指令",
        character_rules="固定群规",
        learning_context="动态学习上下文 A",
    )
    s2 = build_state(
        turn,
        persona_prompt="安安人设",
        decision_prompt="固定决策指令",
        character_rules="固定群规",
        learning_context="动态学习上下文 B",
    )
    keys1 = list(s1.keys())
    assert "learning_context" in s1
    assert "learning_context" not in STATIC_STATE_KEYS
    # Static prefix keys must precede learning_context and stay byte-identical
    last_static_idx = max(keys1.index(k) for k in STATIC_STATE_KEYS if k in s1)
    assert keys1.index("learning_context") > last_static_idx
    assert s1["decision_prompt"] == s2["decision_prompt"] == "固定决策指令"
    assert s1["persona"] == s2["persona"]
    assert s1["character_rules"] == s2["character_rules"]


@pytest.mark.asyncio
async def test_title_routes_to_vibe_provider_and_raises_timeout_for_reasoning():
    adapter = LLMAdapter(SimpleNamespace(), reply_provider_id="slow-reply", vibe_provider_id="fast-vibe")
    assert adapter.configured_provider("title") == "fast-vibe"
    assert adapter.configured_provider("reply") == "slow-reply"

    p = _plugin(
        {
            "topic_reranker_enabled": True,
            "topic_reranker_provider": "",
            "vibe_provider_id": "fast-vibe",
            "reply_provider": "deepseek-reasoner",
            "topic_title_timeout": 10.0,
        }
    )
    try:
        title_rr = p._topic_reranker(for_display=True)
        assert await title_rr.adapter.resolve_provider_id("room", purpose="title") == "fast-vibe"
        assert title_rr.timeout_seconds == 10.0

        # Without vibe_provider, falls back to reasoning reply_provider -> timeout raised
        p.config["vibe_provider_id"] = ""
        p.config["reply_provider"] = "custom-p1"
        p._sync_runtime_from_config()
        p.context.provider_manager = SimpleNamespace(
            inst_map={"custom-p1": SimpleNamespace(id="custom-p1", get_model=lambda: "deepseek-reasoner")}
        )
        assert _looks_like_reasoning_provider(p.context, "custom-p1") is True
        title_rr2 = p._topic_reranker(for_display=True)
        assert await title_rr2.adapter.resolve_provider_id("room", purpose="title") == "custom-p1"
        assert title_rr2.timeout_seconds >= 25.0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_topic_batch_rule_mode_requires_four_nodes_and_caps_retries():
    p = _plugin({"daily_rhythm_enabled": False, "rule_topic_routing_enabled": True})
    calls = []

    async def fail_title(**kwargs):
        calls.append(json.loads(kwargs["prompt"]))
        raise RuntimeError("title failed")

    p.context.llm_generate = fail_title
    rt = p._get_or_create_runtime("mock:GroupMessage:100", group_id="100", umo="mock:GroupMessage:100", bot_id="bot_42")
    try:
        # 3 nodes -> below threshold (>=4), must NOT trigger title call
        now = p.time_service.time()
        nodes = [rt.dag.add_message(f"n{i}", "u1", f"显卡散热测试消息 {i}", timestamp=now - 10 + i) for i in range(3)]
        topic = TopicState("t1", message_ids=[n.msg_id for n in nodes], label_requested=True, updated_at=now)
        rt.routing_state.topics["t1"] = topic
        await p.topic_batches.process(rt, [(n, 0) for n in nodes], is_current=lambda: True)
        assert len(calls) == 0

        # Add 4th node -> eligible, capped at 2 failures (1 initial + 1 retry)
        n3 = rt.dag.add_message("n3", "u2", "显卡散热方案四", timestamp=now)
        topic.message_ids.append("n3")
        nodes.append(n3)
        await p.topic_batches.process(rt, [(n, 0) for n in nodes], is_current=lambda: True)
        assert len(calls) == 1 and topic.title_failures == 1

        topic.title_retry_at = 0.0
        await p.topic_batches.process(rt, [(n, 0) for n in nodes], is_current=lambda: True)
        assert len(calls) == 2 and topic.title_failures == 2

        # 3rd attempt blocked by retry cap
        topic.title_retry_at = 0.0
        await p.topic_batches.process(rt, [(n, 0) for n in nodes], is_current=lambda: True)
        assert len(calls) == 2
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_discussion_reranker_timeout_skips_and_half_open_probe():
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()
    call_count = 0

    class FakeRerankProvider:
        provider_config = {"id": "rr1"}
        provider_settings = {}
        model_name = "sfr"

        async def rerank(self, *, query, documents, top_n):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            probe_entered.set()
            await probe_release.wait()
            return [
                SimpleNamespace(index=0, relevance_score=0.9),
                SimpleNamespace(index=1, relevance_score=0.1),
            ]

    prov = FakeRerankProvider()
    ctx = SimpleNamespace(get_provider_by_id=lambda pid: prov if pid == "rr1" else None)
    rr = DiscussionReranker(ctx, "rr1")
    assert rr.timeout == 6.0

    c1 = RerankCandidate("t1", "topic 1", ("msg1",))
    c2 = RerankCandidate("t2", "topic 2", ("msg2",))

    # Skip single candidate
    res_single = await rr.rank("hello", [c1])
    assert res_single.reason == "single_candidate"
    assert call_count == 0

    # Skip high rule confidence
    res_high = await rr.rank("hello", [c1, c2], rule_confidence=0.90)
    assert res_high.reason == "high_rule_confidence"
    assert call_count == 0

    # Failure enters backoff; subsequent call skips
    res_to = await rr.rank("hello", [c1, c2])
    assert res_to.reason == "timeout"
    assert call_count == 1
    assert rr.calls.failures["rr1"][0] == 1
    assert (await rr.rank("hello", [c1, c2])).reason == "backoff"

    # Expire backoff -> single-flight half-open probe blocks concurrent stampede
    rr.calls.failures["rr1"] = (1, time.monotonic() - 1.0)
    t_probe = asyncio.create_task(rr.rank("hello", [c1, c2]))
    await asyncio.wait_for(probe_entered.wait(), 2.0)
    res_stampede = await rr.rank("hello", [c1, c2])
    assert res_stampede.reason == "backoff"
    probe_release.set()
    res_ok = await t_probe
    assert res_ok.reason == "ranked"
    assert "rr1" not in rr.calls.failures


@pytest.mark.asyncio
async def test_trivial_followup_during_inflight_reply_does_not_discard_reply_and_invalid_logs_stage(jev_plugin, caplog):
    assert _is_trivial_followup("？") is True
    assert _is_trivial_followup("哈哈哈") is True
    assert _is_trivial_followup("嗯嗯") is True
    assert _is_trivial_followup("补充：不要超过一百字") is False
    assert _is_trivial_followup("不对，改成明天") is False

    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_first():
        if len(bridge.requests) == 1:
            entered.set()
            await release.wait()

    bridge.before_reply = slow_first
    first = MockEvent("帮我解释下这个原理", message_id="m-first", components=[At("bot_42")])
    trivial = MockEvent("？", message_id="m-trivial")
    try:
        await p.on_group_message(first)
        await flush(p, first)
        await asyncio.wait_for(entered.wait(), 2.0)

        runtime = p._sessions[first.unified_msg_origin]
        rev_before = runtime.user_revisions.get(first.sender_id, 0)
        assert p.persona_engine.has_in_flight_reply_generation(runtime, first.sender_id) is True

        # Trivial follow-up must NOT bump user_revisions during active generate_ready()
        await p.on_group_message(trivial)
        assert runtime.user_revisions.get(first.sender_id, 0) == rev_before

        release.set()
        await flush(p, trivial)
        await drain(p)
        # First reply was delivered cleanly instead of being discarded!
        assert first.replies_sent
        assert p.persona_engine.has_in_flight_reply_generation(runtime, first.sender_id) is False

        # Verify _invalid() logs drop stage
        caplog.clear()
        with caplog.at_level("WARNING"):
            turn_ctx = TurnContext(runtime.session_key, "u1", "hi", (MessageSnapshot("m-drop", "u1", "hi"),), (), runtime.epoch, 999, 0.0, False)
            from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
            fake_item = ModelTurn(turn_ctx, (MockEvent("hi", message_id="m-drop"),), {}, False)
            assert p.persona_engine._invalid(runtime, fake_item, "member_revision_changed", stage="post_generation") is False
        assert "stage=post_generation" in caplog.text

        # Verify cancel_owned_turns(pre_reply_only=True) cancels pre-reply tasks but not reply_generation_tasks
        pre_reply_task = asyncio.create_task(asyncio.sleep(10))
        reply_gen_task = asyncio.create_task(asyncio.sleep(10))
        runtime.owned_turn_tasks[pre_reply_task] = "u-cancel"
        runtime.owned_turn_tasks[reply_gen_task] = "u-cancel"
        runtime.reply_generation_tasks.add(reply_gen_task)
        try:
            runtime.cancel_owned_turns("u-cancel", pre_reply_only=True)
            await asyncio.sleep(0)
            assert pre_reply_task.cancelled()
            assert not reply_gen_task.cancelled()
            # Full cancel (e.g. /dynamics_stop) cancels both
            runtime.cancel_owned_turns("u-cancel", pre_reply_only=False)
            await asyncio.sleep(0)
            assert reply_gen_task.cancelled()
        finally:
            runtime.owned_turn_tasks.pop(pre_reply_task, None)
            runtime.owned_turn_tasks.pop(reply_gen_task, None)
            runtime.reply_generation_tasks.discard(reply_gen_task)
            for t in (pre_reply_task, reply_gen_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(pre_reply_task, reply_gen_task, return_exceptions=True)
    finally:
        release.set()
        await p.terminate()
