"""Reply ownership and overlapping memory preparation regressions."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn
from astrbot_plugin_chat_dynamics.core.turn_decision import PersonaSnapshot
from .test_narrative_coordination import coordinator, turn, SCOPE
from .test_jev_decision_layer import jev_plugin as jev_plugin, answers
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent, At, Reply


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_host_evidence_and_recall_overlap_and_cancel_together(tmp_path, cancel):
    manager = coordinator(tmp_path)
    recall_entered, host_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = set()

    async def conversation(*args):
        return NS(history='[{"role":"user","content":"hello"}]', created_at=1)

    manager.plugin.context = NS(conversation_manager=NS(get_conversation=conversation))

    async def call(provider, method, *args, **kwargs):
        if method == "get_character_context":
            return {"schema_version": 1, "available": True, "scope": SCOPE, "persona_id": "anan", "core_prompt": "core"}
        if method == "get_snapshot":
            return {"schema_version": 1, "managed": True, "revision": 1, "scope": SCOPE}
        if method in {"recall_context", "get_evidence_view"}:
            (recall_entered if method == "recall_context" else host_entered).set()
            try:
                await release.wait()
            finally:
                finished.add(method)
        return None

    manager.call = call
    item = ModelTurn(turn(), (NS(_extras={}),), {}, False)
    task = asyncio.create_task(
        manager.prepare(NS(umo=SCOPE["umo"]), item, PersonaSnapshot("host", "cid", "anan", "persona"))
    )
    try:
        await asyncio.wait_for(asyncio.gather(recall_entered.wait(), host_entered.wait()), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            assert await task is not None
        assert finished == {"recall_context", "get_evidence_view"}
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_kind", ["at", "quote", "other_sender"])
async def test_peer_chatter_does_not_cancel_ambient_owned_reply(jev_plugin, peer_kind):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending():
        if len(bridge.requests) == 1:
            entered.set()
            await release.wait()

    bridge.before_reply = pending
    first = MockEvent("这个报错怎么处理", message_id="first")
    try:
        await p.on_group_message(first)
        await flush(p, first)
        await asyncio.wait_for(entered.wait(), 2)
        runtime = p._sessions[first.unified_msg_origin]
        revision = runtime.user_revisions[first.sender_id]
        quote = Reply("peer-message")
        quote.sender_id = "peer"
        later = MockEvent(
            "你今晚来吗",
            message_id="peer-talk",
            sender_id="another" if peer_kind == "other_sender" else first.sender_id,
            components=[At("peer")] if peer_kind == "at" else [quote],
        )
        p.jev.payload = answers(action="ignore", reason="other_recipient")
        await p.on_group_message(later)
        await flush(p, later)
        assert runtime.user_revisions[first.sender_id] == revision
        release.set()
        await drain(p)
        assert first.replies_sent
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("judging", [False, True])
async def test_peer_target_does_not_merge_or_supersede_buffer(judging):
    clock = VirtualClock(initial_time=100)
    buffer = DebounceBuffer(time_service=clock, semantic=True, base_cooldown=0.15, max_cap=1.5)
    results = []

    async def received(result):
        results.append(result)

    try:
        await buffer.ingest("room", "alice", "original request", object(), received)
        if judging:
            await clock.advance(0.2)
        await buffer.ingest("room", "alice", "message to peer", object(), received, supersede_pending=False)
        await clock.advance(0.2)
        for _ in range(5):
            await asyncio.sleep(0)
        assert [r.consolidated_text for r in results] == ["original request", "message to peer"]
        assert all(buffer.is_result_current(r) for r in results)
        assert [r.metadata["peer_directed"] for r in results] == [False, True]
        await buffer.ingest("room", "alice", "back to bot", object(), received)
        await clock.advance(.2)
        assert results[-1].consolidated_text == "back to bot"
    finally:
        await buffer.close(flush=False)


@pytest.mark.asyncio
async def test_only_owners_fragments_extend_window_and_cap_is_short():
    clock = VirtualClock(initial_time=100)
    buffer = DebounceBuffer(time_service=clock, semantic=True, base_cooldown=0.15, max_cap=1.5)
    results = []

    async def received(result):
        results.append(result)

    try:
        await buffer.ingest("room", "alice", "question", object(), received)
        await clock.advance(0.1)
        await buffer.ingest("room", "bob", "unrelated", object(), received)
        await clock.advance(0.06)
        assert [r.user_id for r in results] == ["alice"]
        first = results[0]
        await buffer.ingest("room", "alice", "supplement", object(), received)
        assert not buffer.is_result_current(first)
        await clock.advance(0.16)
        assert results[-1].consolidated_text == "question\nsupplement"
        # A continuous owner burst cannot keep resetting the cap indefinitely.
        for index in range(16):
            await buffer.ingest("room", "carol", str(index), object(), received)
            await clock.advance(0.1)
        assert any(r.user_id == "carol" for r in results)
    finally:
        await buffer.close(flush=False)
