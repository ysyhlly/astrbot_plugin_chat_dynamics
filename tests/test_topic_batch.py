"""Topic windows reduce calls while preserving identities and live replay."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.dashboard import scene_replay_snapshot
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.topic_reranker import TopicReranker
from .test_jev_decision_layer import JevDouble, answers
from .test_persona_model import BridgeDouble, drain, flush
from .test_plugin_lifecycle import MockEvent, Reply, _plugin


@pytest.fixture
def batch_plugin(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    p = _plugin({"daily_rhythm_enabled": False}, clock=VirtualClock(100))
    p.persona_engine.bridge = BridgeDouble()
    p.jev = JevDouble(answers(action="ignore", reason="low_value_chatter",
                             topic={"type": "choice", "choice": "NEW", "confidence": .9}))
    evaluate = p.jev.evaluate
    async def matching_jev(**kwargs):
        result = dict(await evaluate(**kwargs))
        word = "显卡" if "显卡" in kwargs["state"]["conversation"]["text"] else "烧烤"
        topic = next((key for key, value in kwargs["state"].get("active_topics", {}).items()
                      if word in value["excerpt"]), "NEW")
        result["topic"] = {"type": "choice", "choice": topic, "confidence": .9}
        return result
    p.jev.evaluate = matching_jev
    p.batch_calls = []

    async def model(**kwargs):
        payload = json.loads(kwargs["prompt"])
        p.batch_calls.append(payload)
        assert all(not rt.state_lock.locked() for rt in p._sessions.values())
        return SimpleNamespace(completion_text=json.dumps({
            "titles": {tid: "显卡散热讨论" if any("显卡" in m["text"] for m in topic["messages"]) else "烧烤菜单讨论"
                       for tid, topic in payload["topics"].items() if topic["needs_title"]},
        }))
    p.context.llm_generate = model
    return p


async def burst(p, *, group="group_100", prefix="gpu", count=4, offset=0):
    previous = f"{prefix}-{offset-1}" if offset else None
    events = []
    for i in range(offset, offset+count):
        text = ("显卡风扇温度调节散热方案" if prefix == "gpu" else "今晚烧烤聚餐菜单计划") + str(i)
        raw = MockEvent(text, sender_id="AB"[i % 2], group_id=group, message_id=f"{prefix}-{i}",
                        components=[Reply(previous)] if previous else [])
        await p.on_group_message(raw)
        await flush(p, raw)
        await drain(p)
        events.append(raw)
        previous = raw.message_id
    return events


async def settle():
    for _ in range(40):
        await asyncio.sleep(0)


async def tick(p, seconds):
    await p.time_service.advance(seconds)
    await settle()


def test_batch_interval_defaults_and_validation():
    assert parse_runtime_config({})[0].topic_batch_interval == 30
    assert parse_runtime_config({"topic_batch_interval": 60})[0].topic_batch_interval == 60
    for value in (0, 4, 301, float("inf")):
        cfg, warnings = parse_runtime_config({"topic_batch_interval": value})
        assert cfg.topic_batch_interval == 30 and warnings


@pytest.mark.asyncio
async def test_many_turns_wait_for_one_window_and_formed_topic_stays_visible(batch_plugin):
    p = batch_plugin
    try:
        events = await burst(p, count=10)
        before = scene_replay_snapshot(p)
        assert len(before["topic_blocks"]) == 1 and not before["empty"]
        assert before["topic_blocks"][0]["title_status"] == "pending"
        assert before["topic_blocks"][0]["message_count"] == 10
        assert not p.batch_calls
        await tick(p, 29.9)
        assert not p.batch_calls
        await tick(p, 0.2)
        assert len(p.batch_calls) == 1
        assert "messages" not in p.batch_calls[0], "the ordinary model only labels confirmed topics"
        block = scene_replay_snapshot(p)["topic_blocks"][0]
        assert block["topic_title"] == "显卡散热讨论" and block["title_status"] == "ready"
        assert all(m["text"] == "消息内容已隐藏" for m in block["messages"])
        assert len(p.jev.calls) == 10 and not any(e.replies_sent for e in events)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_continuous_chat_never_restarts_the_first_window(batch_plugin):
    p = batch_plugin
    try:
        await burst(p)
        for i in range(4, 9):
            await tick(p, 5)
            await burst(p, count=1, offset=i)
            assert not p.batch_calls
        await tick(p, 5)
        assert len(p.batch_calls) == 1
        assert len(p.batch_calls[0]["topics"]) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_multiple_topics_share_one_call_and_idle_never_polls(batch_plugin):
    p = batch_plugin
    try:
        await burst(p)
        await burst(p, prefix="food")
        assert len(scene_replay_snapshot(p)["topic_blocks"]) == 2
        await tick(p, 30)
        assert len(p.batch_calls) == 1 and len(p.batch_calls[0]["topics"]) == 2
        await tick(p, 120)
        assert len(p.batch_calls) == 1
        assert not p._sessions["mock:GroupMessage:group_100"].topic_batch_task
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_existing_titles_are_reused_without_more_topic_calls(batch_plugin):
    p = batch_plugin
    try:
        await burst(p)
        await tick(p, 30)
        await burst(p, count=4, offset=4)
        await tick(p, 30)
        assert len(p.batch_calls) == 1
        assert scene_replay_snapshot(p)["topic_blocks"][0]["message_count"] == 8
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_sessions_have_independent_windows(batch_plugin):
    p = batch_plugin
    try:
        await burst(p, group="one")
        await tick(p, 10)
        await burst(p, group="two")
        await tick(p, 20)
        assert len(p.batch_calls) == 1
        await tick(p, 10)
        assert len(p.batch_calls) == 2
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["reset", "disable", "stop", "unload"])
async def test_pending_batches_obey_lifecycle_changes(batch_plugin, ending):
    p = batch_plugin
    try:
        events = await burst(p)
        if ending == "reset":
            await p._reset_session_state_async(events[0].unified_msg_origin)
        elif ending == "disable":
            await p.save_config_values({"topic_reranker_enabled": False})
        elif ending == "stop":
            for uid in ("A", "B"):
                await p.cmd_dynamics_stop(MockEvent("/dynamics_stop", sender_id=uid))
        else:
            await p.terminate()
        await tick(p, 60)
        assert not p.batch_calls
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_interval_setting_reschedules_queued_messages_and_is_saved(batch_plugin):
    p = batch_plugin
    try:
        await burst(p)
        await p.save_config_values({"topic_batch_interval": 5})
        assert p._runtime_config.topic_batch_interval == p.get_effective_config()["topic_batch_interval"] == 5
        await tick(p, 5)
        assert len(p.batch_calls) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True])
async def test_new_turns_do_not_invalidate_inflight_topic_title(batch_plugin, shadow):
    p = batch_plugin
    started, release = asyncio.Event(), asyncio.Event()
    original = p.context.llm_generate
    async def blocked(**kwargs):
        started.set()
        await release.wait()
        return await original(**kwargs)
    p.context.llm_generate = blocked
    try:
        await p.save_config_values({"shadow_mode": shadow})
        await burst(p)
        await tick(p, 30)
        await asyncio.wait_for(started.wait(), 1)
        await burst(p, count=1, offset=4)
        release.set()
        await settle()
        assert scene_replay_snapshot(p)["topic_blocks"][0]["title_status"] == "ready"
        assert len(p.batch_calls) == 1
        await tick(p, 30)
        assert len(p.batch_calls) == 1
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_reset_rejects_a_model_that_ignores_cancellation(batch_plugin):
    p = batch_plugin
    started, release = asyncio.Event(), asyncio.Event()
    original = p.context.llm_generate
    async def blocked(**kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return await original(**kwargs)
    p.context.llm_generate = blocked
    try:
        events = await burst(p)
        runtime = p._sessions[events[0].unified_msg_origin]
        await tick(p, 30)
        await started.wait()
        topic = next(iter(runtime.routing_state.topics.values()))
        p._reset_session_state(runtime.session_key)
        release.set()
        await settle()
        assert not topic.generated_title and not topic.title_in_flight
        assert not runtime.routing_state.topics and not runtime.topic_batch_pending
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_title_model_cannot_reassign_membership(batch_plugin):
    p = batch_plugin
    try:
        p.thread_router = ThreadRouter(require_intense_dialogue=False)
        rt = p._get_or_create_runtime("room", group_id="g", umo="room", bot_id="bot")
        nodes = []
        for i, text in enumerate(("graphics driver installation", "tomato growing in garden", "graphics rendering library"), 1):
            node = rt.dag.add_message(f"n{i}", f"u{i}", text, timestamp=90+i)
            p._route_message(rt, node)
            nodes.append(node)
        original_topics = [n.metadata["routing"]["topic_id"] for n in nodes]
        for topic in rt.routing_state.topics.values():
            topic.label_requested = True
        async def classify(**kwargs):
            data = json.loads(kwargs["prompt"])
            p.batch_calls.append(data)
            return SimpleNamespace(completion_text=json.dumps({"assignments": {"n2": "n1", "n3": "n1"}}))
        p.context.llm_generate = classify
        async with rt.state_lock:
            p.topic_batches.enqueue(rt, nodes)
        await tick(p, 30)
        assert len(p.batch_calls) == 1
        assert [n.metadata["routing"]["topic_id"] for n in nodes] == original_topics
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_failed_titles_back_off_and_require_new_messages_to_retry(batch_plugin):
    p = batch_plugin
    async def failed(**kwargs):
        p.batch_calls.append(json.loads(kwargs["prompt"]))
        raise RuntimeError("offline")
    p.context.llm_generate = failed
    try:
        await burst(p)
        await tick(p, 30)
        assert len(p.batch_calls) == 1
        await tick(p, 30)
        assert len(p.batch_calls) == 1, "failure cannot schedule idle retries"
        await burst(p, count=1, offset=4)
        await tick(p, 30)
        assert len(p.batch_calls) == 2
        await burst(p, count=1, offset=5)
        await tick(p, 30)
        assert len(p.batch_calls) == 2, "second failure owns a 60-second backoff"
        await burst(p, count=1, offset=6)
        await tick(p, 30)
        assert len(p.batch_calls) == 3
        await burst(p, count=1, offset=7)
        await tick(p, 30)
        assert len(p.batch_calls) == 3, "three attempts exhaust this topic's title work"
        assert scene_replay_snapshot(p)["topic_blocks"][0]["title_status"] == "failed"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_stop_during_generation_rejects_the_affected_topic_title(batch_plugin):
    p = batch_plugin
    started, release = asyncio.Event(), asyncio.Event()
    original = p.context.llm_generate
    async def blocked(**kwargs):
        started.set()
        await release.wait()
        return await original(**kwargs)
    p.context.llm_generate = blocked
    try:
        events = await burst(p)
        rt = p._sessions[events[0].unified_msg_origin]
        await tick(p, 30)
        await started.wait()
        topic = next(iter(rt.routing_state.topics.values()))
        await p.cmd_dynamics_stop(MockEvent("/dynamics_stop", sender_id="A"))
        release.set()
        await settle()
        assert not topic.generated_title and not topic.title_in_flight
        assert not rt.topic_batch_task and not rt.topic_batch_pending
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_busy_window_bounds_context_and_preserves_same_name_qq_accounts(batch_plugin):
    p = batch_plugin
    try:
        p.thread_router = ThreadRouter(require_intense_dialogue=False)
        rt = p._get_or_create_runtime("room", group_id="g", umo="room", bot_id="bot")
        nodes = []
        for i in range(160):
            node = rt.dag.add_message(f"m{i}", "12345" if i % 2 else "67890",
                "显卡风扇温度调整散热方案" + str(i), timestamp=100+i/100,
                metadata={"sender_name": "相同昵称", "sender_platform": "aiocqhttp"})
            p._route_message(rt, node)
            nodes.append(node)
        for topic in rt.routing_state.topics.values():
            topic.label_requested = True
        async with rt.state_lock:
            p.topic_batches.enqueue(rt, nodes)
            assert len(rt.topic_batch_pending) == 128
        await tick(p, 30)
        assert len(p.batch_calls) == 1
        topics = p.batch_calls[0]["topics"]
        assert len(topics) <= 8 and all(len(t["messages"]) <= 3 for t in topics.values())
        messages = [m for t in topics.values() for m in t["messages"]]
        assert {m["author_identity"]["qq"] for m in messages} == {"12345", "67890"}
        assert {m["author_identity"]["display_name"] for m in messages} == {"相同昵称"}
        assert len(rt.dag.nodes) == 160, "batch limits must not discard local evidence"
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["prose", "[]", '{"assignments":[],"titles":{}}',
    '{"assignments":{"alien":"t1","m1":"alien"},"titles":{"alien":"秘密","t1":"bad\\nline"}}'])
async def test_malformed_or_unoffered_batch_results_are_not_applied(output):
    adapter = AsyncMock()
    adapter.generate.return_value = output
    result = await TopicReranker(adapter, enabled=True).enrich_batch(umo="room", payload={
        "messages": [{"message_id": "m1", "candidate_topic_ids": ["t1"]}],
        "topics": {"t1": {"needs_title": True}},
    })
    assert not result.assignments and not result.titles
    assert adapter.generate.await_count == 1
