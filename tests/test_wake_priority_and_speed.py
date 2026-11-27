"""Tests for pure-quote immediate flush, priority queue / gentle preemption, and parallel prepare_reply."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import persona_engine as pe_mod
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn, _is_priority_wake
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import answers, jev_plugin as _jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, Reply

jev_plugin = _jev_plugin


def _quote_bot_event(text="你怎么看", *, message_id="q1", sender_id="user_a", extra_components=()):
    quote = Reply("old-bot-msg")
    quote.sender_id = "bot_42"
    return MockEvent(
        text,
        sender_id=sender_id,
        message_id=message_id,
        components=[quote, *extra_components],
    )


def _make_turn(
    runtime,
    *,
    sender_id="user_a",
    message_id="m1",
    text="你好",
    wake_kind="none",
    explicit=False,
    private_direct=False,
    debounce_result=None,
):
    now = 100.0
    node = runtime.dag.add_message(
        message_id,
        sender_id,
        text,
        timestamp=now,
        metadata={"platform_message_id": True},
    )
    event = MockEvent(text, sender_id=sender_id, message_id=message_id)
    turn = TurnContext(
        runtime.session_key,
        sender_id,
        text,
        (MessageSnapshot(message_id, sender_id, text),),
        (),
        runtime.epoch,
        runtime.user_revisions.get(sender_id, 0),
        now,
        explicit=explicit,
        wake_kind=wake_kind,
        bot_id=runtime.bot_id,
    )
    return ModelTurn(
        turn,
        (event,),
        {},
        False,
        platform_message_ids=frozenset({message_id}),
        outcome_nodes=(node,),
        debounce_result=debounce_result,
        private_direct=private_direct,
    )


# ---------------------------------------------------------------------------
# 1. main.py wake_immediate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected_immediate",
    [
        ("at", True),
        ("pure_quote", True),
        ("quote_and_at_other", False),
        ("name", False),
        ("quote_peer", False),
    ],
)
async def test_wake_immediate_flags(jev_plugin, case, expected_immediate):
    p, _bridge = jev_plugin
    await p.save_config_values({"bot_names": ["小助手"]})
    captured = []
    orig_ingest = p.debounce.ingest

    async def spy_ingest(*args, **kwargs):
        captured.append(dict(kwargs))
        return await orig_ingest(*args, **kwargs)

    p.debounce.ingest = spy_ingest

    if case == "at":
        event = MockEvent("帮我看看", message_id="ev-at", components=[At("bot_42")])
    elif case == "pure_quote":
        event = _quote_bot_event("你怎么看", message_id="ev-pure-quote")
    elif case == "quote_and_at_other":
        event = _quote_bot_event(
            "你看看它刚才说的",
            message_id="ev-quote-other",
            extra_components=[At("peer_99")],
        )
    elif case == "name":
        event = MockEvent("小助手，你怎么看", message_id="ev-name")
    else:
        quote = Reply("peer-msg")
        quote.sender_id = "peer_99"
        event = MockEvent("同意", message_id="ev-quote-peer", components=[quote])

    try:
        await p.on_group_message(event)
        assert len(captured) == 1
        assert captured[0]["immediate"] is expected_immediate
    finally:
        await p.terminate()


# ---------------------------------------------------------------------------
# 2. _is_priority_wake, priority queue ordering, and ambient eviction
# ---------------------------------------------------------------------------


def test_is_priority_wake_predicate(jev_plugin):
    p, _bridge = jev_plugin
    runtime = p._get_or_create_runtime("g:1", group_id="1", umo="g:1", bot_id="bot_42")
    assert _is_priority_wake(_make_turn(runtime, message_id="p-at", wake_kind="at", explicit=True)) is True
    assert _is_priority_wake(_make_turn(runtime, message_id="p-quote", wake_kind="quote", explicit=True)) is True
    assert _is_priority_wake(_make_turn(runtime, message_id="p-priv", wake_kind="none", private_direct=True)) is True
    assert _is_priority_wake(_make_turn(runtime, message_id="a-none", wake_kind="none")) is False
    assert _is_priority_wake(_make_turn(runtime, message_id="a-name", wake_kind="name", explicit=True)) is False
    assert _is_priority_wake(_make_turn(runtime, message_id="a-poke", wake_kind="poke")) is False
    assert _is_priority_wake(None) is False


@pytest.mark.asyncio
async def test_priority_turns_jump_ahead_of_ambient_in_fifo_order(jev_plugin):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def slow_first():
        if len(bridge.requests) == 1:
            entered.set()
            await release.wait()

    bridge.before_reply = slow_first
    p.jev.payload = answers(action="reply", state="focused", reason="open_group_topic", join={"type": "noul", "noul": 0.9})

    # Active head is a priority @ so it won't be preempted while we inspect queue ordering.
    head = MockEvent("先处理我", sender_id="u_head", message_id="head", components=[At("bot_42")])
    a1 = MockEvent("闲聊1", sender_id="u_a1", message_id="a1")
    a2 = MockEvent("闲聊2", sender_id="u_a2", message_id="a2")
    p1 = MockEvent("紧急@1", sender_id="u_p1", message_id="p1", components=[At("bot_42")])
    p2 = _quote_bot_event("紧急引用2", sender_id="u_p2", message_id="p2")
    a3 = MockEvent("闲聊3", sender_id="u_a3", message_id="a3")

    try:
        await p.on_group_message(head)
        await flush(p, head)
        await asyncio.wait_for(entered.wait(), 2.0)

        runtime = p._sessions[head.unified_msg_origin]
        for ev in (a1, a2, p1, p2, a3):
            await p.on_group_message(ev)
            await flush(p, ev)

        queued_ids = [item.context.messages[-1].message_id for item in runtime.model_queue]
        assert queued_ids == ["p1", "p2", "a1", "a2", "a3"]

        release.set()
        await drain(p)

        order = [req[0]["conversation"]["messages"][-1]["message_id"] for req in bridge.requests]
        assert order[:3] == ["head", "p1", "p2"]
        assert p1.replies_sent and p2.replies_sent
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_priority_evicts_oldest_ambient_when_semaphore_full(jev_plugin):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def block_head():
        if len(bridge.requests) == 1:
            entered.set()
            await release.wait()

    bridge.before_reply = block_head
    p.jev.payload = answers(action="reply", state="focused", reason="open_group_topic", join={"type": "noul", "noul": 0.9})

    head = MockEvent("先处理我", sender_id="u_head", message_id="head", components=[At("bot_42")])
    ambients = [
        MockEvent(f"普通闲聊{i}", sender_id=f"u_amb_{i}", message_id=f"amb_{i}")
        for i in range(8)
    ]
    urgent = MockEvent("紧急求助", sender_id="u_urgent", message_id="urgent_at", components=[At("bot_42")])

    finished_results = []
    orig_finish = p.debounce.finish_result

    def spy_finish(res):
        if res is not None:
            finished_results.append(res)
        return orig_finish(res)

    p.debounce.finish_result = spy_finish

    try:
        await p.on_group_message(head)
        await flush(p, head)
        await asyncio.wait_for(entered.wait(), 2.0)

        runtime = p._sessions[head.unified_msg_origin]
        for ev in ambients:
            await p.on_group_message(ev)
            await flush(p, ev)

        assert runtime.model_admission.locked()
        assert len(runtime.model_queue) == 8

        # Submitting a priority turn must NOT stall on full semaphore; it evicts amb_0.
        await p.on_group_message(urgent)
        await asyncio.wait_for(flush(p, urgent), 1.5)

        queued_ids = [item.context.messages[-1].message_id for item in runtime.model_queue]
        assert queued_ids[0] == "urgent_at"
        assert "amb_0" not in queued_ids
        assert len(queued_ids) == 8

        evicted_node = runtime.dag.get_node("amb_0")
        evicted_outcome = evicted_node.metadata.get("outcome", {})
        assert evicted_outcome.get("final_outcome") == "suppressed"
        assert evicted_outcome.get("suppression_reason") == "preempted_by_wake"
        assert evicted_outcome.get("stage") == "queue"

        release.set()
        await drain(p)
        assert urgent.replies_sent
        assert runtime.model_admission._value == 9
    finally:
        release.set()
        await p.terminate()


# ---------------------------------------------------------------------------
# 3. Gentle preemption tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gentle_preemption_cancels_pre_reply_ambient_immediately(jev_plugin):
    p, bridge = jev_plugin
    ambient_in_decide = asyncio.Event()
    ambient_cancelled = asyncio.Event()
    block_decide = asyncio.Event()

    orig_eval = p.jev.evaluate

    async def slow_eval(**kwargs):
        msgs = kwargs.get("state", {}).get("conversation", {}).get("messages", [])
        if msgs and msgs[-1].get("message_id") == "amb_pre":
            ambient_in_decide.set()
            try:
                await block_decide.wait()
            except asyncio.CancelledError:
                ambient_cancelled.set()
                raise
        return await orig_eval(**kwargs)

    p.jev.evaluate = slow_eval
    p.jev.payload = answers(action="reply", state="focused", reason="open_group_topic", join={"type": "noul", "noul": 0.9})

    amb = MockEvent("随便聊聊", sender_id="u_amb", message_id="amb_pre")
    urgent = MockEvent("急事找你", sender_id="u_at", message_id="urgent_pre", components=[At("bot_42")])

    try:
        await p.on_group_message(amb)
        await flush(p, amb)
        await asyncio.wait_for(ambient_in_decide.wait(), 2.0)

        await p.on_group_message(urgent)
        await flush(p, urgent)

        # Ambient must be cancelled without ever setting block_decide!
        await asyncio.wait_for(ambient_cancelled.wait(), 2.0)
        await drain(p)

        assert urgent.replies_sent
        assert not amb.replies_sent
        runtime = p._sessions[amb.unified_msg_origin]
        amb_outcome = runtime.dag.get_node("amb_pre").metadata.get("outcome", {})
        assert amb_outcome.get("final_outcome") == "suppressed"
        assert amb_outcome.get("suppression_reason") == "preempted_by_wake"
        assert runtime.model_admission._value == 9
    finally:
        block_decide.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_gentle_preemption_cancels_invalidated_in_reply_before_first_sentence(jev_plugin):
    p, bridge = jev_plugin
    in_reply = asyncio.Event()
    gen_cancelled = asyncio.Event()
    release_gen = asyncio.Event()

    async def slow_reply():
        if len(bridge.requests) == 1:
            in_reply.set()
            try:
                await release_gen.wait()
            except asyncio.CancelledError:
                gen_cancelled.set()
                raise

    bridge.before_reply = slow_reply
    p.jev.payload = answers(action="reply", state="focused", reason="open_group_topic", join={"type": "noul", "noul": 0.9})

    amb = MockEvent("聊个天", sender_id="u_amb", message_id="amb_inv")
    urgent = MockEvent("快帮我", sender_id="u_at", message_id="urgent_inv", components=[At("bot_42")])

    try:
        await p.on_group_message(amb)
        await flush(p, amb)
        await asyncio.wait_for(in_reply.wait(), 2.0)

        runtime = p._sessions[amb.unified_msg_origin]
        # Invalidate the ambient turn (e.g., author revision moved / recalled) while first sentence not sent.
        runtime.user_revisions[amb.sender_id] = runtime.user_revisions.get(amb.sender_id, 0) + 1
        assert runtime.reply_first_sentence_sent.get(amb.sender_id, 0) == 0

        await p.on_group_message(urgent)
        await flush(p, urgent)

        await asyncio.wait_for(gen_cancelled.wait(), 2.0)
        await drain(p)

        assert urgent.replies_sent
        assert not amb.replies_sent
        amb_outcome = runtime.dag.get_node("amb_inv").metadata.get("outcome", {})
        assert amb_outcome.get("final_outcome") == "suppressed"
        assert amb_outcome.get("suppression_reason") == "preempted_by_wake"
        assert runtime.model_admission._value == 9
    finally:
        release_gen.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_gentle_preemption_does_not_interrupt_valid_active_reply(jev_plugin):
    p, bridge = jev_plugin
    in_reply = asyncio.Event()
    release_gen = asyncio.Event()
    gen_cancelled = False

    async def slow_reply():
        nonlocal gen_cancelled
        if len(bridge.requests) == 1:
            in_reply.set()
            try:
                await release_gen.wait()
            except asyncio.CancelledError:
                gen_cancelled = True
                raise

    bridge.before_reply = slow_reply
    p.jev.payload = answers(action="reply", state="focused", reason="open_group_topic", join={"type": "noul", "noul": 0.9})

    amb = MockEvent("聊个正事", sender_id="u_amb", message_id="amb_valid")
    urgent = MockEvent("也帮我看看", sender_id="u_at", message_id="urgent_wait", components=[At("bot_42")])

    try:
        await p.on_group_message(amb)
        await flush(p, amb)
        await asyncio.wait_for(in_reply.wait(), 2.0)

        await p.on_group_message(urgent)
        await flush(p, urgent)
        await asyncio.sleep(0.05)

        assert not gen_cancelled
        release_gen.set()
        await drain(p)

        assert amb.replies_sent
        assert urgent.replies_sent
        assert [req[0]["conversation"]["messages"][-1]["message_id"] for req in bridge.requests] == [
            "amb_valid",
            "urgent_wait",
        ]
    finally:
        release_gen.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_gentle_preemption_does_not_interrupt_after_first_sentence_sent(jev_plugin):
    p, bridge = jev_plugin
    in_reply = asyncio.Event()
    release_gen = asyncio.Event()
    gen_cancelled = False

    async def slow_reply():
        nonlocal gen_cancelled
        if len(bridge.requests) == 1:
            in_reply.set()
            try:
                await release_gen.wait()
            except asyncio.CancelledError:
                gen_cancelled = True
                raise

    bridge.before_reply = slow_reply
    p.jev.payload = answers(action="reply", state="focused", reason="open_group_topic", join={"type": "noul", "noul": 0.9})

    amb = MockEvent("长回复测试", sender_id="u_amb", message_id="amb_fs")
    urgent = MockEvent("插队测试", sender_id="u_at", message_id="urgent_fs", components=[At("bot_42")])

    try:
        await p.on_group_message(amb)
        await flush(p, amb)
        await asyncio.wait_for(in_reply.wait(), 2.0)

        runtime = p._sessions[amb.unified_msg_origin]
        # First sentence already sent: even if invalidated, must not cancel in-flight reply.
        runtime.reply_first_sentence_sent[amb.sender_id] = 1
        runtime.user_revisions[amb.sender_id] = runtime.user_revisions.get(amb.sender_id, 0) + 1

        await p.on_group_message(urgent)
        await flush(p, urgent)
        await asyncio.sleep(0.05)
        assert not gen_cancelled

        runtime.reply_first_sentence_sent.pop(amb.sender_id, None)
        release_gen.set()
        await drain(p)
        assert urgent.replies_sent
    finally:
        release_gen.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_gentle_preemption_never_interrupts_another_priority_turn(jev_plugin):
    p, bridge = jev_plugin
    first_in_decide = asyncio.Event()
    release_first = asyncio.Event()
    first_cancelled = False

    orig_eval = p.jev.evaluate

    async def slow_quote_eval(**kwargs):
        nonlocal first_cancelled
        msgs = kwargs.get("state", {}).get("conversation", {}).get("messages", [])
        if msgs and msgs[-1].get("message_id") == "q_first":
            first_in_decide.set()
            try:
                await release_first.wait()
            except asyncio.CancelledError:
                first_cancelled = True
                raise
        return await orig_eval(**kwargs)

    p.jev.evaluate = slow_quote_eval
    p.jev.payload = answers(action="reply", state="focused", reason="addressed_question")

    q_first = _quote_bot_event("第一个引用问题", sender_id="u_one", message_id="q_first")
    at_second = MockEvent("第二个@", sender_id="u_two", message_id="at_second", components=[At("bot_42")])

    try:
        await p.on_group_message(q_first)
        await flush(p, q_first)
        await asyncio.wait_for(first_in_decide.wait(), 2.0)

        await p.on_group_message(at_second)
        await flush(p, at_second)
        await asyncio.sleep(0.05)

        assert not first_cancelled
        release_first.set()
        await drain(p)

        assert q_first.replies_sent
        assert at_second.replies_sent
        assert [req[0]["conversation"]["messages"][-1]["message_id"] for req in bridge.requests] == [
            "q_first",
            "at_second",
        ]
    finally:
        release_first.set()
        await p.terminate()


# ---------------------------------------------------------------------------
# 4. Parallel prepare_reply with JEV decide() tests
# ---------------------------------------------------------------------------


class _FakeBundle:
    def __init__(self, turn_id: str, payload: dict):
        self.turn_id = turn_id
        self.bundle_id = f"b-{turn_id}-{payload.get('tag', 'base')}"
        self.payload = json.dumps(payload, ensure_ascii=False)
        self._data = payload

    @property
    def snapshot(self):
        return self._data["snapshot"]

    def wire(self):
        return json.loads(self.payload)

    def decision_context(self):
        return {"availability": self._data["snapshot"].get("availability", "available")}


class _FakeNarrative:
    def __init__(
        self,
        *,
        full_delay: float = 0.0,
        revise_on_advance: bool = False,
        on_full_start=None,
        on_full_cancelled=None,
    ):
        self.full_delay = full_delay
        self.revise_on_advance = revise_on_advance
        self.on_full_start = on_full_start
        self.on_full_cancelled = on_full_cancelled
        self.light_calls = 0
        self.full_calls = 0
        self.full_cancelled_count = 0
        self.full_start_times = []
        self.full_end_times = []
        self.bound_bundles = []

    async def note_member_activity(self, event, parsed):
        return None

    async def prepare(self, runtime, item, persona, *, lightweight=False):
        turn_id = item.context.messages[-1].message_id if item.context.messages else "t0"
        if lightweight:
            self.light_calls += 1
            return _FakeBundle(
                turn_id,
                {
                    "tag": f"light-{self.light_calls}",
                    "preparation_phase": "decision",
                    "character": {
                        "fingerprint": persona.fingerprint,
                        "core_prompt": persona.prompt,
                        "group_rules": "",
                    },
                    "snapshot": {"availability": "available", "revision": 1, "scope_revision": 1, "stale": False},
                    "availability": {},
                },
            )
        self.full_calls += 1
        call_no = self.full_calls
        self.full_start_times.append(time.monotonic())
        if callable(self.on_full_start):
            self.on_full_start(call_no, item)
        try:
            if self.full_delay > 0:
                await asyncio.sleep(self.full_delay)
        except asyncio.CancelledError:
            self.full_cancelled_count += 1
            if callable(self.on_full_cancelled):
                self.on_full_cancelled(call_no)
            raise
        self.full_end_times.append(time.monotonic())
        prior_tag = ""
        if item.narrative_bundle is not None:
            prior_tag = item.narrative_bundle.wire().get("tag", "")
        return _FakeBundle(
            turn_id,
            {
                "tag": f"full-{call_no}-from-{prior_tag}",
                "preparation_phase": "reply",
                "character": {
                    "fingerprint": persona.fingerprint,
                    "core_prompt": persona.prompt,
                    "group_rules": "",
                },
                "snapshot": {"availability": "available", "revision": 1, "scope_revision": 1, "stale": False},
                "availability": {},
            },
        )

    async def route_life_events_async(self, bundle, impact, **kwargs):
        return None

    async def advance_important(self, item, bundle, impact):
        if not self.revise_on_advance:
            return bundle
        data = bundle.wire()
        data["tag"] = "revised-by-advance"
        return _FakeBundle(bundle.turn_id, data)

    async def bind_async(self, events, bundle):
        self.bound_bundles.append(bundle)

    async def attach_reply_evidence_async(self, item, decision, jev_decision):
        return None

    async def current(self, bundle, event):
        return True

    async def begin_send_async(self, event, text):
        return None

    async def finish_send_async(self, *args, **kwargs):
        return None

    def evidence_current(self, wire):
        return True

    async def evidence_current_async(self, wire):
        return True

    def outreach_allowed(self, umo):
        return False

    async def observe_turn(self, *args, **kwargs):
        return None

    async def record_delivery(self, *args, **kwargs):
        return None

    async def close_io(self):
        return None


@pytest.mark.asyncio
async def test_parallel_prepare_reply_overlaps_with_decide_and_saves_latency(jev_plugin):
    p, bridge = jev_plugin
    decide_finished_at = None
    full_started_before_decide_done = False

    def on_full_start(call_no, _item):
        nonlocal full_started_before_decide_done
        if call_no == 1 and decide_finished_at is None:
            full_started_before_decide_done = True

    fake_narrative = _FakeNarrative(full_delay=0.15, on_full_start=on_full_start)
    p.narrative = fake_narrative

    orig_eval = p.jev.evaluate

    async def timed_eval(**kwargs):
        nonlocal decide_finished_at
        await asyncio.sleep(0.15)
        res = await orig_eval(**kwargs)
        decide_finished_at = time.monotonic()
        return res

    p.jev.evaluate = timed_eval
    p.jev.payload = answers(action="reply", state="focused", reason="addressed_question")

    event = _quote_bot_event("帮我查一下这个", message_id="par-quote")
    try:
        started = time.monotonic()
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        elapsed = time.monotonic() - started

        assert event.replies_sent and len(bridge.requests) == 1
        assert fake_narrative.light_calls == 1
        assert fake_narrative.full_calls == 1
        assert full_started_before_decide_done is True
        # Serial would be >= 0.30s; parallel should complete close to 0.15s (+ small overhead).
        assert elapsed < 0.27, f"expected parallel overlap < 0.27s, got {elapsed:.3f}s"

        samples = [s for s in p.persona_engine.telemetry.samples if s["stage"] == "prepare_reply"]
        assert len(samples) == 1
        assert samples[0]["outcome"] == "success"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_parallel_prepare_reply_cancelled_cleanly_on_ignore(jev_plugin):
    p, bridge = jev_plugin
    full_started = asyncio.Event()
    full_cancelled = asyncio.Event()

    fake_narrative = _FakeNarrative(
        full_delay=2.0,
        on_full_start=lambda _n, _it: full_started.set(),
        on_full_cancelled=lambda _n: full_cancelled.set(),
    )
    p.narrative = fake_narrative
    p.jev.delay = 0.02
    p.jev.payload = answers(action="ignore", state="observing", reason="other_recipient")

    event = _quote_bot_event("跟别人说的", message_id="par-ignore")
    try:
        started = time.monotonic()
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        elapsed = time.monotonic() - started

        assert not event.replies_sent and not bridge.requests
        assert full_started.is_set()
        assert full_cancelled.is_set()
        assert fake_narrative.full_cancelled_count == 1
        assert elapsed < 0.5
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_parallel_prepare_reply_not_launched_when_rhythm_defers(jev_plugin, monkeypatch):
    p, bridge = jev_plugin
    await p.save_config_values({"natural_reply_rhythm_enabled": True})
    fake_narrative = _FakeNarrative(full_delay=0.1)
    p.narrative = fake_narrative

    monkeypatch.setattr(
        p.natural_rhythm,
        "evaluate_ingress_pacing",
        lambda runtime, **kwargs: SimpleNamespace(
            defer=True,
            urgent_breakthrough=False,
            reason_code="rhythm_deferred_micro_distract",
            reason_zh="暂时忙",
            wake_at_wall=time.time() + 30.0,
        ),
    )

    event = _quote_bot_event("稍等下", message_id="par-defer")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)

        assert not event.replies_sent and not bridge.requests
        assert fake_narrative.light_calls == 1
        assert fake_narrative.full_calls == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_parallel_prepare_reply_discarded_on_advance_important_mutation(jev_plugin):
    p, bridge = jev_plugin
    fake_narrative = _FakeNarrative(full_delay=0.2, revise_on_advance=True)
    p.narrative = fake_narrative
    p.jev.delay = 0.02
    p.jev.payload = answers(action="reply", state="focused", reason="addressed_question")

    event = _quote_bot_event("重要事件推进", message_id="par-adv-mut")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)

        assert event.replies_sent and len(bridge.requests) == 1
        # Speculative call #1 was cancelled; fresh call #2 ran after advance_important revised bundle.
        assert fake_narrative.full_calls == 2
        assert fake_narrative.full_cancelled_count == 1
        runtime = p._sessions[event.unified_msg_origin]
        assert "from-revised-by-advance" in runtime.model_diagnostic["narrative"]["bundle_id"]
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_parallel_prepare_reply_discarded_on_context_completion_mutation(jev_plugin, monkeypatch):
    p, bridge = jev_plugin
    p._runtime_config = replace(p._runtime_config, context_completion_enabled=True)
    fake_narrative = _FakeNarrative(full_delay=0.2)
    p.narrative = fake_narrative
    p.jev.delay = 0.02

    call_idx = 0
    orig_eval = p.jev.evaluate

    async def need_recent_then_ready(**kwargs):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            p.jev.payload = answers(
                action="reply",
                state="focused",
                reason="addressed_question",
                context_need={"type": "choice", "choice": "recent", "confidence": 0.95},
            )
        else:
            p.jev.payload = answers(
                action="reply",
                state="focused",
                reason="addressed_question",
                context_need={"type": "choice", "choice": "ready", "confidence": 0.95},
            )
        return await orig_eval(**kwargs)

    p.jev.evaluate = need_recent_then_ready

    async def fake_complete_context(plugin, runtime, item, mode):
        data = item.narrative_bundle.wire()
        data["tag"] = "completed-by-ctx"
        updated_bundle = _FakeBundle(item.narrative_bundle.turn_id, data)
        return replace(
            item,
            narrative_bundle=updated_bundle,
            observations={
                **item.observations,
                "context_completion": {"attempted": True, "status": "ok", "added_messages": 1, "elapsed_ms": 5},
            },
        )

    monkeypatch.setattr(pe_mod, "complete_context", fake_complete_context)

    event = _quote_bot_event("补充上下文看看", message_id="par-ctx-mut")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)

        assert event.replies_sent and len(bridge.requests) == 1
        assert fake_narrative.full_calls == 2
        assert fake_narrative.full_cancelled_count == 1
        runtime = p._sessions[event.unified_msg_origin]
        assert "from-completed-by-ctx" in runtime.model_diagnostic["narrative"]["bundle_id"]
    finally:
        await p.terminate()
