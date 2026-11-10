"""Pokes retain attributed sender history at the real prompt boundaries."""

import json
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.context_retrieval import ContextEvidence, select_background
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.persona_engine import ModelTurn, refresh_topic_context
from astrbot_plugin_chat_dynamics.core.poke import PokeReplyPolicy
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, PersonaSnapshot
from .test_persona_explicit_context import _runtime, _snapshot
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent, _plugin
from .test_poke import Poke
from .test_narrative_coordination import coordinator, SCOPE, turn


def seed(dag, now, bot="bot-42", owner="alice"):
    source = dag.add_message("source", owner, "我刚说今天想读完那本书", timestamp=now - 600)
    reply = dag.add_message(
        "answer",
        bot,
        "那本书还有哪一章没看？",
        timestamp=now - 599,
        reply_to_id=source.msg_id,
        metadata={"trigger_user_id": owner, "dialogue_delivered": True},
    )
    dag.add_message("other", "bob", "另一个人的无关话题", timestamp=now - 2)
    dag.add_message(
        "other-reply",
        bot,
        "给另一个人的回复",
        timestamp=now - 1,
        metadata={"trigger_user_id": "bob", "dialogue_delivered": True},
    )
    dag.add_message("draft", bot, "未发送的草稿", timestamp=now - 1, metadata={"trigger_user_id": owner})
    return source, reply


def test_poke_without_topic_includes_its_sender_and_delivered_reply_only():
    dag = ConversationDAG("mock:GroupMessage:room")
    source, reply = seed(dag, 2000)
    current = dag.add_message("poke", "alice", "[戳一戳] 有人戳了你", timestamp=2000, metadata={"poke_at_bot": True})
    # Even an inferred parent from another member must not replace the sender.
    dag.link_related(current.msg_id, "other", kind="inferred_reply")
    current.metadata["edge_metadata"] = {"other": {"confidence": 0.99}}
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    assert [m.message_id for m in item.context.background] == [source.msg_id, reply.msg_id]
    assert {p.relation for m in item.context.background for p in m.context_evidence} == {"poke_sender", "poke_reply"}
    assert [m.message_id for m in refresh_topic_context(runtime, item).context.background] == [
        source.msg_id,
        reply.msg_id,
    ]
    other_dag = ConversationDAG("mock:GroupMessage:other")
    other_dag.add_message("private", "alice", "其他会话资料", timestamp=1999)
    assert "private" not in item.context.allowed_ids


def test_poke_respects_age_count_watermark_and_prompt_budget():
    dag = ConversationDAG("room")
    dag.add_message("expired", "alice", "过期记录", timestamp=1)
    for index in range(12):
        source = dag.add_message(f"u{index}", "alice", "发言内容" * 1000, timestamp=1800 + index * 2)
        dag.add_message(
            f"b{index}",
            "bot-42",
            "已发送回复" * 1000,
            timestamp=1801 + index * 2,
            reply_to_id=source.msg_id,
            metadata={"trigger_user_id": "alice", "dialogue_delivered": True},
        )
    current = dag.add_message("poke", "alice", "[戳一戳] 有人戳了你", timestamp=2000, metadata={"poke_at_bot": True})
    visible = frozenset(dag.nodes)
    for stamp in (1999, 2000, 2001):
        dag.add_message(f"late-{stamp}", "alice", "后到消息", timestamp=stamp)
    rows = select_background(dag, [current], bot_id="bot-42", eligible_ids=visible)
    assert len(rows) == 12
    assert {r.node.msg_id for r in rows} == {f"{prefix}{i}" for prefix in ("u", "b") for i in range(6, 12)}
    background = _snapshot(_runtime(dag), current).context.background
    assert len(background) == 12 and sum(len(m.text) for m in background) <= 4800
    assert all(len(m.text) <= 400 for m in background)


def test_plain_text_cannot_impersonate_platform_poke():
    dag = ConversationDAG("room")
    seed(dag, 2000)
    node = dag.add_message("typed", "alice", "[戳一戳] 有人戳了你", timestamp=2000)
    assert not any(
        p.relation.startswith("poke_")
        for r in select_background(dag, [node], bot_id="bot-42")
        for p in r.context_evidence
    )


@pytest.mark.asyncio
async def test_poke_sender_context_reaches_both_jev_and_reply(jev_plugin):
    p, bridge = jev_plugin
    event = MockEvent("", sender_id="alice", message_id="poke", components=[Poke("bot_42")])
    runtime = p._get_or_create_runtime(
        event.unified_msg_origin, group_id=event.get_group_id(), umo=event.unified_msg_origin
    )
    runtime.bot_id = "bot_42"
    seed(runtime.dag, p.time_service.time(), bot="bot_42")
    p.jev.payload = answers(state="casual", reason="social_signal")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert event.replies_sent
        for conversation in (p.jev.calls[-1]["state"]["conversation"], bridge.requests[-1][0]["conversation"]):
            assert {m["message_id"] for m in conversation["background"]} == {"source", "answer"}
            assert "今天想读完那本书" in json.dumps(conversation, ensure_ascii=False)
            assert "另一个人的无关话题" not in json.dumps(conversation, ensure_ascii=False)
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True])
async def test_legacy_poke_uses_same_sender_selection_and_boundary_filter(monkeypatch, filtered):
    p = _plugin({"pipeline_mode": "filter", "presence_knob": "sensible"})
    p.poke_policy = PokeReplyPolicy(rng=random.Random(0))
    event = MockEvent("", sender_id="alice", message_id="poke", components=[Poke("bot_42")])
    runtime = p._get_or_create_runtime(
        event.unified_msg_origin, group_id=event.get_group_id(), umo=event.unified_msg_origin
    )
    runtime.bot_id = "bot_42"
    seed(runtime.dag, p.time_service.time(), bot="bot_42")
    prompts = []

    async def generate(**kwargs):
        prompts.append(kwargs["prompt"])
        return SimpleNamespace(completion_text="还在读那本书呀？")

    p.context.llm_generate = generate
    from astrbot_plugin_chat_dynamics.core import member_boundary

    original_filter = member_boundary.filter_frames

    def filter_frames(host, incoming, frames, **kwargs):
        kept, state = original_filter(host, incoming, frames, **kwargs)
        if filtered:
            kept = [f for f in kept if f.get("frame_id") not in {"source", "answer"}]
        return kept, state

    monkeypatch.setattr(member_boundary, "filter_frames", filter_frames)
    try:
        await p.on_group_message(event)
        assert prompts and ("今天想读完那本书" in prompts[0]) is not filtered
        assert "另一个人的无关话题" not in prompts[0]
    finally:
        await p.terminate()


def test_snapshot_poke_history_uses_existing_member_boundary(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import member_boundary

    dag = ConversationDAG("room")
    seed(dag, 2000)
    current = dag.add_message("poke", "alice", "[戳一戳] 有人戳了你", timestamp=2000, metadata={"poke_at_bot": True})
    original = member_boundary.filter_frames

    def filter_frames(host, event, frames, **kwargs):
        kept, state = original(host, event, frames, **kwargs)
        return [f for f in kept if f.get("frame_id") not in {"source", "answer"}], state

    monkeypatch.setattr(member_boundary, "filter_frames", filter_frames)
    assert not _snapshot(_runtime(dag), current).context.background


@pytest.mark.asyncio
async def test_memory_recall_query_identifies_poke_sender(tmp_path):
    class World:
        async def get_character_context(self, scope, event):
            return {
                "schema_version": 1,
                "available": True,
                "scope": SCOPE,
                "persona_id": "anan",
                "core_prompt": "core",
                "group_rules": "",
                "fingerprint": "a",
            }

    class Life:
        async def get_snapshot(self, scope):
            return {"schema_version": 1, "managed": True, "scope": SCOPE, "revision": 1}

    queries = []

    class Memory:
        async def recall_context(self, scope, query, *args):
            queries.append((scope, query))
            return {"items": []}

    manager = coordinator(tmp_path, {"worldbook": World(), "dynamic_life_state": Life(), "livingmemory": Memory()})
    context = replace(
        turn(),
        wake_kind="poke",
        text="[戳一戳] 有人戳了你",
        background=(
            MessageSnapshot("old", "u1", "刚才讨论的书", context_evidence=(ContextEvidence("poke_sender", "poke", 1),)),
        ),
    )
    item = ModelTurn(context, (SimpleNamespace(_extras={}),), {}, False)
    await manager.prepare(SimpleNamespace(umo=SCOPE["umo"]), item, PersonaSnapshot("host", "cid", "anan", "persona"))
    assert queries[0][0]["umo"] == SCOPE["umo"]
    query = json.loads(queries[0][1])
    assert query["initiator_id"] == "u1"
    assert query["recent_context"][0]["text"] == "刚才讨论的书"
