"""Batch 3 regression tests: hot-path safety, prefix cache stability, budget isolation, and non-blocking offload."""

from __future__ import annotations

import asyncio
import json
import random
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_chat_dynamics import series_panels
from astrbot_plugin_chat_dynamics.core import member_boundary
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import (
    STATIC_STATE_KEYS,
    build_state,
)
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from astrbot_plugin_chat_dynamics.core.llm_decision import split_decision_payload
from astrbot_plugin_chat_dynamics.core.media_archive import MediaArchive
from astrbot_plugin_chat_dynamics.core.natural_rhythm import NaturalRhythmController
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn, PersonaEngine
from astrbot_plugin_chat_dynamics.core.provider_budget import (
    ProviderBudget,
    ProviderBudgetBusyError,
)
from astrbot_plugin_chat_dynamics.core.reply_request_prompt import assemble_reply_prompts
from astrbot_plugin_chat_dynamics.core.series_hub import federated_series_search
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.systemone.routing import RoutedSystemOneClient
from astrbot_plugin_chat_dynamics.core.systemone.usage_context import record_decision_usage
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock


def _make_runtime(session_key: str = "aiocqhttp:GroupMessage:g1") -> SessionRuntime:
    return SessionRuntime(
        session_key=session_key,
        group_id="g1",
        umo=session_key,
        bot_id="bot1",
        scope_kind="group",
        dag=ConversationDAG(),
    )


# ---------------------------------------------------------------------------
# P0-1: BoundaryCancelled handled explicitly in PersonaEngine.run()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_engine_run_catches_boundary_cancelled_as_suppressed_without_mandatory_fallback():
    rt = _make_runtime()
    failure_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    mandatory_calls: list[Any] = []
    finished_results: list[Any] = []

    plugin = SimpleNamespace(
        _shutting_down=False,
        _in_flight=set(),
        _runtime_config=parse_runtime_config({})[0],
        _mark_panel_runtime_dirty=lambda: None,
        _create_background_task=asyncio.create_task,
        debounce=SimpleNamespace(
            finish_result=lambda res: finished_results.append(res),
            release_result_media=lambda _res: None,
        ),
        context=SimpleNamespace(),
    )
    engine = PersonaEngine(plugin)
    engine.valid = lambda runtime, item: True  # type: ignore[method-assign]
    engine.valid_async = lambda runtime, item: asyncio.sleep(0, result=True)  # type: ignore[method-assign]

    async def _raise_boundary(runtime, item, **_kwargs):
        raise member_boundary.BoundaryCancelled("evidence_revision_changed")

    async def _record_mandatory(runtime, item, *, failure=""):
        mandatory_calls.append((item, failure))

    engine.process = _raise_boundary  # type: ignore[method-assign]
    engine._ensure_mandatory_reply = _record_mandatory  # type: ignore[method-assign]
    engine._finish_generation_failure = lambda *a, **kw: failure_calls.append((a, kw))  # type: ignore[method-assign]

    turn = TurnContext(
        session_key=rt.session_key,
        author="u1",
        text="@安安 你好",
        messages=(
            MessageSnapshot(
                message_id="m1",
                author="u1",
                author_name="Alice",
                text="@安安 你好",
                timestamp=1.0,
            ),
        ),
        background=(),
        epoch=1,
        revision=0,
        started_at=1.0,
        explicit=True,
        wake_kind="at",
        bot_id="bot1",
    )
    item = ModelTurn(
        context=turn,
        events=(SimpleNamespace(unified_msg_origin=rt.session_key),),
        observations={},
        shadow=False,
        debounce_result="tok1",
    )
    await rt.model_admission.acquire()
    rt.model_queue.append(item)

    await engine.run(rt)

    assert len(failure_calls) == 1
    args, kwargs = failure_calls[0]
    assert args[2] == "evidence_revision_changed"
    assert kwargs.get("suppressed") is True
    assert kwargs.get("error_type") == "BoundaryCancelled"
    assert rt.model_diagnostic["reason_code"] == "boundary_cancelled"
    assert rt.model_diagnostic["reason"] == "evidence_revision_changed"
    assert mandatory_calls == []
    assert finished_results == ["tok1"]


# ---------------------------------------------------------------------------
# P0-2: execute_afterthought() disables early return, awaits completion_task, bounds lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_ready_disables_early_callbacks_for_afterthought():
    rt = _make_runtime()
    seen_kwargs: dict[str, Any] = {}
    cfg, _ = parse_runtime_config(
        {"reply_first_sentence_enabled": True, "max_fragments": 3}
    )
    plugin = SimpleNamespace(
        _runtime_config=cfg,
        _mark_panel_runtime_dirty=lambda: None,
        _create_background_task=asyncio.create_task,
        context=SimpleNamespace(),
    )
    engine = PersonaEngine(plugin)

    async def _fake_generate(event, events, prompt, persona, provider, **kwargs):
        seen_kwargs.update(kwargs)
        return SimpleNamespace(text="完整补话", history=[], completion_task=None)

    engine.generate = _fake_generate  # type: ignore[method-assign]
    turn = TurnContext(
        session_key=rt.session_key,
        author="u1",
        text="补一句",
        messages=(MessageSnapshot(message_id="m1", author="u1", text="补一句"),),
        background=(),
        epoch=1,
        revision=0,
        started_at=1.0,
        explicit=False,
        bot_id="bot1",
    )
    out = await engine.generate_ready(
        rt,
        turn,
        SimpleNamespace(unified_msg_origin=rt.session_key),
        (),
        "prompt",
        SimpleNamespace(conversation_id="c1"),
        "p1",
        budget_purpose="afterthought",
    )
    assert out.text == "完整补话"
    assert seen_kwargs.get("on_partial_text") is None
    assert seen_kwargs.get("on_output_ready") is None


@pytest.mark.asyncio
async def test_execute_afterthought_lock_timeout_and_completion_task_safety():
    rt = _make_runtime()
    metrics: list[str] = []
    cfg, _ = parse_runtime_config(
        {
            "natural_reply_rhythm_enabled": True,
            "rhythm_afterthought_enabled": True,
            "reply_first_sentence_enabled": True,
        }
    )
    lock = asyncio.Lock()

    class _FakeBridge:
        def session_lock(self, umo: str, *, acquire_timeout: float | None = None):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _cm():
                if acquire_timeout is None:
                    await lock.acquire()
                else:
                    await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
                try:
                    yield
                finally:
                    lock.release()

            return _cm()

        async def snapshot(self, event):
            return SimpleNamespace(fingerprint="fp1", conversation_id="c1", persona_id="p1", prompt="")

        async def current(self, event, persona):
            return True

        async def commit(self, event, output, delivered_text):
            events_order.append(f"commit:{delivered_text}")
            return None

    events_order: list[str] = []

    async def _fake_send_owned(runtime, event, text, reply_to_id=None):
        events_order.append(f"send:{text}")
        return SimpleNamespace(
            success=True,
            message_id="b2",
            delivery_uncertain=False,
            delivered_text=lambda t: t,
        )

    plugin = SimpleNamespace(
        _shutting_down=False,
        _runtime_config=cfg,
        _metric=lambda name: metrics.append(name),
        _mark_panel_runtime_dirty=lambda: None,
        llm=SimpleNamespace(resolve_provider_id=lambda *a, **kw: asyncio.sleep(0, result="p1")),
        _build_synthetic_rhythm_event=lambda *a, **kw: SimpleNamespace(unified_msg_origin=rt.session_key),
        _send_owned=_fake_send_owned,
        _last_bot_nodes={},
        _observe_routed_bot=lambda *a, **kw: None,
        _next_outgoing_id=lambda: "b2",
        time_service=SimpleNamespace(time=lambda: 100.0, wall_time=lambda: 100.0),
        context=SimpleNamespace(),
    )
    engine = PersonaEngine(plugin)
    engine.bridge = _FakeBridge()
    engine._afterthought_lock_timeout = 0.05

    slot = SimpleNamespace(
        anchor_bot_msg_id="b1",
        trigger_msg_id="m1",
        target_user_id="u1",
        anchor_topic_id="t1",
        delivered_text="刚才说了一半",
        persona_fingerprint="fp1",
        private_direct=False,
        tier="short",
        epoch=rt.epoch,
    )

    # 1) Lock held -> bounded timeout skips cleanly without blocking
    await lock.acquire()
    try:
        res = await engine.execute_afterthought(rt, slot, quote_self=False)
    finally:
        lock.release()
    assert res == {"status": "skipped", "reason": "rhythm_afterthought_lock_timeout"}

    # 2) Unfinished completion_task returned by generate_ready is awaited BEFORE send/commit
    async def _finish_bg():
        await asyncio.sleep(0.02)
        events_order.append("completion_done")
        return SimpleNamespace(text="完整补话内容", history=["h1"], completion_task=None)

    async def _fake_generate_ready(*args, **kwargs):
        assert kwargs.get("allow_early") is False
        bg = asyncio.create_task(_finish_bg())
        return SimpleNamespace(text="半句", history=[], completion_task=bg)

    engine.generate_ready = _fake_generate_ready  # type: ignore[method-assign]

    res2 = await engine.execute_afterthought(rt, slot, quote_self=False)
    assert res2["status"] == "sent"
    assert events_order == ["completion_done", "send:完整补话内容", "commit:完整补话内容"]


# ---------------------------------------------------------------------------
# P1-1: NaturalRhythmController live probe, concurrent dispatch, loop resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_natural_rhythm_probes_live_world_availability_overcoming_stale_bundle():
    clock = VirtualClock(initial_time=10_000.0)
    rt = _make_runtime("aiocqhttp:GroupMessage:g_stale")
    cfg, _ = parse_runtime_config(
        {
            "natural_reply_rhythm_enabled": True,
            "rhythm_catchup_enabled": True,
            "rhythm_distract_probability": 1.0,
        }
    )

    live_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def _live_call(cap: str, action: str, scope: dict[str, Any], timeout: float = 3.0):
        live_calls.append((cap, action, scope))
        return {"availability": "available", "stale": False, "managed": True}

    stale_bundle = SimpleNamespace(
        scope={"platform": "aiocqhttp", "umo": rt.umo, "persona_id": "p1", "instance_id": "i1"},
        snapshot={"availability": "busy", "stale": False},
    )
    plugin = SimpleNamespace(
        _runtime_config=cfg,
        time_service=clock,
        _sessions={rt.session_key: rt},
        _shutting_down=False,
        shadow_mode=False,
        presence_knob="lively",
        narrative=SimpleNamespace(
            _bundles={rt.umo: stale_bundle},
            call=_live_call,
            enabled=lambda umo: True,
        ),
        _metric=lambda _name: None,
        _mark_panel_runtime_dirty=lambda: None,
    )
    sent: list[str] = []

    async def _exec(runtime, item):
        sent.append(item.primary_msg_id)
        return {"status": "sent", "message_id": "b1"}

    ctrl = NaturalRhythmController(plugin, rng=random.Random(1), catchup_executor=_exec)
    n1 = rt.dag.add_message(
        "m1",
        "u1",
        "@安安 有空帮我看下吗？",
        clock.time(),
        mentioned_users=["bot1"],
        metadata={"is_wake": True, "wall_timestamp": clock.wall_time()},
    )
    dec = ctrl.evaluate_ingress_pacing(
        rt,
        msg_id="m1",
        user_id="u1",
        text=n1.text,
        directed=True,
        availability="busy",
        persona_fingerprint="fp1",
        node=n1,
    )
    assert dec.defer is True

    avail = await ctrl._probe_world_availability(rt)
    assert avail == "available"
    assert live_calls and live_calls[0][0] == "dynamic_life_state"

    await clock.advance(60.0)
    count = await ctrl.run_catchup(rt)
    assert count == 1
    assert sent == ["m1"]


@pytest.mark.asyncio
async def test_natural_rhythm_scheduler_dispatches_sessions_concurrently_and_survives_exception():
    clock = VirtualClock(initial_time=20_000.0)
    cfg, _ = parse_runtime_config({"natural_reply_rhythm_enabled": True})
    plugin = SimpleNamespace(
        _runtime_config=cfg,
        time_service=clock,
        _sessions={},
        _shutting_down=False,
        shadow_mode=False,
        _metric=lambda _name: None,
        _mark_panel_runtime_dirty=lambda: None,
    )
    ctrl = NaturalRhythmController(plugin, rng=random.Random(2))

    block_s2 = asyncio.Event()
    s3_done = asyncio.Event()
    s4_done = asyncio.Event()
    order: list[str] = []

    async def _fake_dispatch(session_key: str, kind: str) -> None:
        order.append(f"start:{session_key}")
        if session_key == "s1":
            raise RuntimeError("boom in s1")
        if session_key == "s2":
            await block_s2.wait()
            order.append("end:s2")
            return
        if session_key == "s3":
            order.append("end:s3")
            s3_done.set()
            return
        if session_key == "s4":
            order.append("end:s4")
            s4_done.set()

    ctrl._dispatch_due = _fake_dispatch  # type: ignore[method-assign]
    ctrl.start()
    try:
        now = clock.time()
        ctrl._schedule_wake("s1", "catchup", now)
        ctrl._schedule_wake("s2", "catchup", now)
        ctrl._schedule_wake("s3", "catchup", now)

        # s3 must complete while s2 is still blocked (no head-of-line blocking)
        await asyncio.wait_for(s3_done.wait(), timeout=1.0)
        assert "end:s3" in order
        assert "end:s2" not in order
        assert ctrl._scheduler_task is not None and not ctrl._scheduler_task.done()

        # Scheduler loop must still be alive after s1 raised and handle a new job s4
        ctrl._schedule_wake("s4", "catchup", clock.time())
        await asyncio.wait_for(s4_done.wait(), timeout=1.0)
        assert "end:s4" in order

        block_s2.set()
        await asyncio.sleep(0.02)
        assert "end:s2" in order
    finally:
        await ctrl.stop()


# ---------------------------------------------------------------------------
# P1-2: cancel_owned_turns(pre_reply_only=True) protects outer task in reply phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_owned_turns_pre_reply_only_protects_outer_task_during_reply_phase():
    rt = _make_runtime()
    plugin = SimpleNamespace(
        _runtime_config=parse_runtime_config({})[0],
        _create_background_task=asyncio.create_task,
        context=SimpleNamespace(),
    )
    engine = PersonaEngine(plugin)

    entered_reply = asyncio.Event()
    release_reply = asyncio.Event()
    pre_reply_cancelled = False
    reply_phase_cancelled = False

    async def _pre_reply_worker():
        nonlocal pre_reply_cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pre_reply_cancelled = True
            raise

    t_pre = asyncio.create_task(_pre_reply_worker())
    rt.owned_turn_tasks[t_pre] = "u1"
    await asyncio.sleep(0)
    rt.cancel_owned_turns("u1", pre_reply_only=True)
    await asyncio.gather(t_pre, return_exceptions=True)
    assert pre_reply_cancelled is True

    async def _slow_generate(*_args, **_kwargs):
        entered_reply.set()
        await release_reply.wait()
        return SimpleNamespace(text="ok", history=[], completion_task=None)

    engine.generate = _slow_generate  # type: ignore[method-assign]
    turn = TurnContext(
        session_key=rt.session_key,
        author="u1",
        text="hi",
        messages=(MessageSnapshot(message_id="m1", author="u1", text="hi"),),
        background=(),
        epoch=1,
        revision=0,
        started_at=1.0,
        explicit=False,
        bot_id="bot1",
    )

    async def _reply_worker():
        nonlocal reply_phase_cancelled
        try:
            await engine.generate_ready(
                rt,
                turn,
                SimpleNamespace(unified_msg_origin=rt.session_key),
                (),
                "prompt",
                SimpleNamespace(conversation_id="c1"),
                "p1",
            )
        except asyncio.CancelledError:
            reply_phase_cancelled = True
            raise

    t_reply = asyncio.create_task(_reply_worker())
    rt.owned_turn_tasks[t_reply] = "u1"
    await asyncio.wait_for(entered_reply.wait(), timeout=2.0)

    # pre_reply_only=True MUST NOT cancel the outer task or inner reply task once in reply phase
    rt.cancel_owned_turns("u1", pre_reply_only=True)
    await asyncio.sleep(0.02)
    assert not t_reply.done()
    assert reply_phase_cancelled is False

    # pre_reply_only=False (e.g. explicit stop/reset) still cancels it
    rt.cancel_owned_turns("u1", pre_reply_only=False)
    await asyncio.wait_for(asyncio.gather(t_reply, return_exceptions=True), timeout=2.0)
    assert reply_phase_cancelled is True


# ---------------------------------------------------------------------------
# P1-3: Prompt Prefix Cache stability across all 3 fixed sites
# ---------------------------------------------------------------------------


def test_prompt_prefix_cache_stability_across_all_three_sites():
    # 1) llm_decision.split_decision_payload: optional questions stay out of system_prompt
    turn = TurnContext(
        session_key="aiocqhttp:GroupMessage:g1",
        author="u1",
        text="晚上吃什么？",
        messages=(
            MessageSnapshot(
                message_id="m1",
                author="u1",
                author_name="Alice",
                text="晚上吃什么？",
                timestamp=1.0,
            ),
        ),
        background=(),
        epoch=1,
        revision=0,
        started_at=1.0,
        explicit=False,
        bot_id="bot1",
    )
    state_plain = build_state(turn, persona_prompt="你叫安安。")
    state_affection = build_state(
        turn,
        persona_prompt="你叫安安。",
        affection={"user_id": "u1", "group_id": "g1", "affection_level": 82, "max_affection": 100},
    )
    assert "affection_policy" not in STATIC_STATE_KEYS

    base_q = {
        "join": {"type": "noul", "instructions": "是否接话"},
        "action": {"type": "choice", "instructions": "动作", "criteria": {"reply": "回", "wait": "等"}},
    }
    extended_q = {
        **base_q,
        "recipient": {"type": "choice", "instructions": "对谁说", "criteria": {"bot": "我", "other": "别人"}},
        "continuity": {"type": "choice", "instructions": "连续性", "criteria": {"same": "同", "new": "新"}},
        "impact": {"type": "choice", "instructions": "影响", "criteria": {"low": "低", "high": "高"}},
        "topic": {"type": "choice", "instructions": "话题", "criteria": {"t1": "一", "new": "新"}},
        "expression_mode": {"type": "choice", "instructions": "语气", "criteria": {"literal": "直", "sarcasm": "反"}},
    }

    sys_base, user_base = split_decision_payload(state_plain, base_q)
    sys_ext, user_ext = split_decision_payload(state_plain, extended_q)
    assert sys_base == sys_ext
    assert "recipient" not in sys_ext
    assert "continuity" not in sys_ext
    assert "recipient" in user_ext
    assert "expression_mode" in user_ext

    # 2) jev_decision.build_state: affection_policy does not bust system_prompt
    sys_no_aff, user_no_aff = split_decision_payload(state_plain, base_q)
    sys_with_aff, user_with_aff = split_decision_payload(state_affection, base_q)
    assert sys_no_aff == sys_with_aff
    assert "affection_policy" not in sys_with_aff
    assert "affection_policy" in user_with_aff

    # 3) reply_request_prompt: plan=None keeps available_modes in turn_note, not system_prompt
    req = SimpleNamespace(system_prompt="# Persona Instructions\n\n你叫安安。")
    evt = SimpleNamespace(_extras={})
    sys_no_poke, note_no_poke = assemble_reply_prompts(
        req,
        evt,
        native_persona="你叫安安。",
        managed=None,
        internal=False,
        poke_supported=False,
        boundary_state=None,
        tools=(),
    )
    sys_poke, note_poke = assemble_reply_prompts(
        req,
        evt,
        native_persona="你叫安安。",
        managed=None,
        internal=False,
        poke_supported=True,
        boundary_state=None,
        tools=(),
    )
    assert sys_no_poke == sys_poke
    assert "本轮可选回应方式" not in sys_poke
    assert "本轮可选回应方式：text|silent。" in note_no_poke
    assert "本轮可选回应方式：text|poke|poke_text|silent。" in note_poke


# ---------------------------------------------------------------------------
# P1-4: ProviderBudget per-provider & foreground/background queue isolation + off-loop summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_budget_isolates_max_queue_across_providers_and_lanes():
    budget = ProviderBudget(capacity=2, reply_reserve=1, max_queue=2)
    release_bg_a = asyncio.Event()
    started_bg_a = asyncio.Event()

    async def _hold_bg_a():
        started_bg_a.set()
        await release_bg_a.wait()
        return "bg_a_ok"

    # Occupy the 1 background slot on Provider A
    t_active = asyncio.create_task(budget.run("prov_a", "background", _hold_bg_a))
    await started_bg_a.wait()

    # Queue 2 background waiters on Provider A (fills Provider A background max_queue=2)
    t_q1 = asyncio.create_task(budget.run("prov_a", "background", lambda: asyncio.sleep(0, result="q1")))
    t_q2 = asyncio.create_task(budget.run("prov_a", "background", lambda: asyncio.sleep(0, result="q2")))
    await asyncio.sleep(0.01)

    # 3rd background waiter on Provider A MUST be rejected as queue_full
    with pytest.raises(ProviderBudgetBusyError) as exc_info:
        await budget.run("prov_a", "background", lambda: asyncio.sleep(0, result="q3"))
    assert exc_info.value.reason == "queue_full"

    # Foreground reply on Provider A MUST still be admitted (isolated lane + reserved reply slot!)
    fg_res = await budget.run("prov_a", "reply", lambda: asyncio.sleep(0, result="fg_ok"))
    assert fg_res == "fg_ok"

    # Background call on Provider B MUST still be admitted (isolated per provider!)
    pb_res = await budget.run("prov_b", "background", lambda: asyncio.sleep(0, result="pb_ok"))
    assert pb_res == "pb_ok"

    release_bg_a.set()
    assert await asyncio.gather(t_active, t_q1, t_q2) == ["bg_a_ok", "q1", "q2"]


@pytest.mark.asyncio
async def test_provider_budget_daily_summary_offloaded_off_event_loop():
    main_tid = threading.get_ident()
    summary_tids: list[int] = []

    class _FakeTelemetry:
        def today_usage_summary(self):
            summary_tids.append(threading.get_ident())
            return {"total_tokens": 100, "estimated_cost_usd": 0.01}

        def record(self, **_kwargs):
            return None

    budget = ProviderBudget(
        capacity=2,
        telemetry=_FakeTelemetry(),
        daily_token_soft_limit=1000,
    )
    res = await budget.run("prov_a", "reply", lambda: asyncio.sleep(0, result="ok"))
    assert res == "ok"
    assert summary_tids, "expected today_usage_summary to be invoked"
    assert all(tid != main_tid for tid in summary_tids)

    # live_snapshot() must NEVER call today_usage_summary synchronously on the main thread
    before = len(summary_tids)
    snap = budget.live_snapshot()
    assert isinstance(snap, dict) and "capacity" in snap
    assert all(tid != main_tid for tid in summary_tids[before:])


# ---------------------------------------------------------------------------
# P1-5: MediaArchive thread-safety, prune throttling, and background offload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_archive_prune_throttling_and_background_offload(tmp_path):
    now_val = [1000.0]
    archive = MediaArchive(tmp_path / "media", ttl=10.0, now=lambda: now_val[0])
    d = archive.create_directory("sess1")
    (d / "a.bin").write_bytes(b"abc")
    archive.active.discard(d)
    (d / "lease.json").write_text(json.dumps({"conversation_hash": "h", "expires_at": 1005.0}))

    # create_directory() already called prune() at t=1000 -> within 30s throttle skips
    now_val[0] = 1010.0
    assert archive.prune(min_interval=30.0) is False
    assert d.exists()

    # Advance past 30s throttle -> runs and cleans expired dir
    now_val[0] = 1031.0
    assert archive.prune(min_interval=30.0) is True
    assert not d.exists()

    # Verify background offload from PersonaEngine and AstrBotAgentBridge runs off main thread
    main_tid = threading.get_ident()
    tids: list[tuple[str, int]] = []
    done_ev = asyncio.Event()
    done_ev2 = asyncio.Event()
    loop = asyncio.get_running_loop()

    class _SpyArchive:
        def touch(self, umo, cid, *, history=None):
            tids.append(("touch", threading.get_ident()))
            if history is not None:
                loop.call_soon_threadsafe(done_ev2.set)

        def prune(self, *, min_interval=None):
            tids.append(("prune", threading.get_ident()))
            loop.call_soon_threadsafe(done_ev.set)

    plugin = SimpleNamespace(
        media_archive=_SpyArchive(),
        _runtime_config=parse_runtime_config({})[0],
        context=SimpleNamespace(),
    )
    engine = PersonaEngine(plugin)
    engine._schedule_media_maintenance("u1", "c1")
    await asyncio.wait_for(done_ev.wait(), timeout=2.0)

    bridge = AstrBotAgentBridge(SimpleNamespace())
    bridge._schedule_media_touch(_SpyArchive(), "u1", "c1", history=[{"role": "user"}])
    await asyncio.wait_for(done_ev2.wait(), timeout=2.0)

    assert len(tids) == 3
    assert all(tid != main_tid for _op, tid in tids)


# ---------------------------------------------------------------------------
# P2-1: Concurrent decision usage isolation & group_id telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_usage_context_isolates_concurrent_calls_and_records_group_id(monkeypatch):
    gate_a_started = asyncio.Event()
    gate_b_done = asyncio.Event()

    class _ConcurrentProvider:
        is_systemone_provider = True
        timeout = 10.0
        last_decision_usage = None

        def get_model(self):
            return "sys1-model"

        async def systemone_evaluate(self, *, state, questions, timeout=None):
            tag = state["conversation"]["tag"]
            if tag == "A":
                record_decision_usage(
                    self,
                    {"input_tokens": 111, "output_tokens": 11, "cached_tokens": 10, "reported": True},
                )
                gate_a_started.set()
                await gate_b_done.wait()
                return {"join": {"type": "noul", "noul": 0.9}}
            await gate_a_started.wait()
            record_decision_usage(
                self,
                {"input_tokens": 999, "output_tokens": 99, "cached_tokens": 90, "reported": True},
            )
            gate_b_done.set()
            return {"join": {"type": "noul", "noul": 0.2}}

    provider = _ConcurrentProvider()
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        series_panels,
        "record_series_llm_usage",
        lambda ctx, **kw: recorded.append(kw) or True,
    )

    cfg, _ = parse_runtime_config({"decision_provider": "p_sys1", "participation_backend": "jev"})
    host = SimpleNamespace(
        _runtime_config=cfg,
        context=SimpleNamespace(get_provider_by_id=lambda pid: provider if pid == "p_sys1" else None),
    )
    router = RoutedSystemOneClient(host, fallback=SimpleNamespace())
    questions = {"join": {"type": "noul", "instructions": "join?"}}

    await asyncio.gather(
        router.evaluate(
            state={"conversation": {"tag": "A"}},
            questions=questions,
            timeout=5.0,
            group_id="aiocqhttp:GroupMessage:group_A",
        ),
        router.evaluate(
            state={"conversation": {"tag": "B"}},
            questions=questions,
            timeout=5.0,
            group_id="aiocqhttp:GroupMessage:group_B",
        ),
    )

    by_group = {entry["group_id"]: entry for entry in recorded}
    assert set(by_group) == {
        "aiocqhttp:GroupMessage:group_A",
        "aiocqhttp:GroupMessage:group_B",
    }
    assert by_group["aiocqhttp:GroupMessage:group_A"]["response"]["usage"]["input_tokens"] == 111
    assert by_group["aiocqhttp:GroupMessage:group_B"]["response"]["usage"]["input_tokens"] == 999


# ---------------------------------------------------------------------------
# P2-2: _conf_schema.json & ConfigPanel alignment
# ---------------------------------------------------------------------------


def test_conf_schema_topic_reranker_timeout_and_missing_keys():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    cfg, warnings = parse_runtime_config({})
    assert warnings == ()

    assert schema["topic_reranker_timeout"]["default"] == 6.0
    assert schema["topic_reranker_timeout"]["slider"]["max"] == 30
    assert cfg.topic_reranker_timeout == 6.0

    assert schema["vibe_provider"]["default"] == ""
    assert schema["narrative_archive_retention_days"]["default"] == 30
    assert schema["narrative_dead_letter_ttl_days"]["default"] == 7

    panel = ConfigPanel(SimpleNamespace(_runtime_config=cfg, config={}, _coerce_config=lambda d: d))
    eff = panel.get_effective_config(runtime_config=cfg)
    assert eff["vibe_provider"] == ""
    assert eff["narrative_archive_retention_days"] == 30
    assert eff["narrative_dead_letter_ttl_days"] == 7
    assert eff["topic_reranker_timeout"] == 6.0


# ---------------------------------------------------------------------------
# P2-3: federated_series_search protects thread pool against slow sync plugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_federated_series_search_isolates_slow_sync_plugin_via_inflight_and_cooldown(monkeypatch):
    slow_calls = 0
    fast_calls = 0
    release_slow = threading.Event()

    class _SlowSyncOwner:
        def series_search_items(self, query, scope, limit):
            nonlocal slow_calls
            slow_calls += 1
            release_slow.wait(timeout=1.5)
            return [{"title": "slow"}]

    class _FastSyncOwner:
        def series_search_items(self, query, scope, limit):
            nonlocal fast_calls
            fast_calls += 1
            return [{"kind": "note", "title": f"fast-{query}", "subtitle": "ok"}]

    slow_owner = _SlowSyncOwner()
    fast_owner = _FastSyncOwner()
    owners = {
        "astrbot_plugin_dynamics_world": slow_owner,
        "astrbot_plugin_dynamics_memory": fast_owner,
    }
    monkeypatch.setattr(
        series_panels,
        "find_plugin_owner",
        lambda _ctx, pid: owners.get(pid),
    )

    plugin = SimpleNamespace(context=SimpleNamespace())
    try:
        for i in range(4):
            res = await federated_series_search(plugin, query=f"q{i}", timeout_seconds=0.06)
            assert any(item["title"] == f"fast-q{i}" for item in res["items"])
            assert "astrbot_plugin_dynamics_world" in res["timed_out_plugins"]
        # Slow plugin was NOT spawned 4 times into _SEARCH_EXECUTOR
        assert slow_calls == 1
        assert fast_calls == 4
    finally:
        release_slow.set()
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Shared series_panels.py: zero-subprocess _installed_commit + async cached status()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_series_panels_no_git_subprocess_and_async_cached_status(tmp_path, monkeypatch):
    # 1) Pure-Python .git resolution (symbolic ref, detached HEAD, worktree gitdir)
    sha_main = "a" * 40
    repo_dir = tmp_path / "repo"
    (repo_dir / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo_dir / ".git" / "HEAD").write_text("ref: refs/heads/feat/panel-v2\n", encoding="utf-8")
    (repo_dir / ".git" / "refs" / "heads" / "feat").mkdir(parents=True)
    (repo_dir / ".git" / "refs" / "heads" / "feat" / "panel-v2").write_text(sha_main + "\n", encoding="utf-8")
    assert series_panels._installed_commit(repo_dir) == sha_main[:12]

    sha_detached = "b" * 40
    detached_dir = tmp_path / "detached"
    (detached_dir / ".git").mkdir(parents=True)
    (detached_dir / ".git" / "HEAD").write_text(sha_detached + "\n", encoding="utf-8")
    assert series_panels._installed_commit(detached_dir) == sha_detached[:12]

    # Worktree .git file pointing to gitdir
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    (wt_dir / ".git").write_text(f"gitdir: {detached_dir / '.git'}\n", encoding="utf-8")
    assert series_panels._installed_commit(wt_dir) == sha_detached[:12]

    # 2) SeriesPanels.status() offloads presence() to worker thread and caches within TTL
    main_tid = threading.get_ident()
    presence_calls: list[tuple[int, bool]] = []

    def _fake_presence(ctx, identity, *, force_refresh=False):
        presence_calls.append((threading.get_ident(), bool(force_refresh)))
        return {"schema_version": 1, "current": identity, "plugins": [], "seq": len(presence_calls)}

    monkeypatch.setattr(series_panels, "presence", _fake_presence)
    monkeypatch.setattr(series_panels, "json_response", lambda data, status_code=200: (status_code, data))

    req_query: dict[str, str] = {}

    async def _fake_admin(ctx, req, *, check_csrf=False):
        return "admin"

    monkeypatch.setattr(series_panels, "require_series_admin", _fake_admin)
    monkeypatch.setattr(
        series_panels,
        "request",
        SimpleNamespace(method="GET", query=req_query, headers={}),
    )

    ctx = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "admin"}})
    owner = SimpleNamespace(context=ctx)
    panel = series_panels.SeriesPanels(owner, "astrbot_plugin_chat_dynamics")
    series_panels.invalidate_presence_cache(context=ctx)

    code1, body1 = await panel.status()
    code2, body2 = await panel.status()
    assert code1 == 200 and code2 == 200
    assert body1["data"]["seq"] == 1
    assert body2["data"]["seq"] == 1
    assert len(presence_calls) == 1
    assert presence_calls[0][0] != main_tid

    # Explicit refresh=1 bypasses TTL cache and re-runs off-loop
    req_query["refresh"] = "1"
    code3, body3 = await panel.status()
    assert code3 == 200
    assert body3["data"]["seq"] == 2
    assert len(presence_calls) == 2
    assert presence_calls[1][0] != main_tid
    assert presence_calls[1][1] is True
