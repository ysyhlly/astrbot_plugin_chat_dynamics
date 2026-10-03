"""Membership shares the live Jev request; labels and retirement have separate lifetimes."""
import asyncio
from dataclasses import replace

import pytest

from astrbot_plugin_chat_dynamics.core.dashboard import scene_replay_snapshot
from astrbot_plugin_chat_dynamics.core.session_runtime import TopicState
from astrbot_plugin_chat_dynamics.core.topic_identity import node_topic_id
from astrbot_plugin_chat_dynamics.core.topic_jev import align_topic_task, build_topic_task
from astrbot_plugin_chat_dynamics.core.topic_resolution import TopicResolver
from .test_jev_decision_layer import JevDouble, answers
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent, Reply
from .test_topic_batch import batch_plugin as batch_plugin, tick


def choose(p, choice, **extra):
    p.jev = JevDouble(answers(action="ignore", reason="low_value_chatter",
        topic={"type": "choice", "choice": choice, "confidence": .9}, **extra))


async def send(p, text, mid, *, user="A", group="group_100"):
    event = MockEvent(text, sender_id=user, group_id=group, message_id=mid)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    return p._sessions[event.unified_msg_origin]


@pytest.mark.asyncio
async def test_new_label_enters_the_next_jev_request_and_existing_topic_never_calls_label_model(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        rt = await send(p, "显卡温度很高，如何调整散热方案", "first")
        assert rt.dag.nodes["first"].metadata["routing"]["evidence"][-1] == "topic_jev_new"
        ledger = rt.dag.nodes["first"].metadata["routing"]["ledger"]["entries"]
        assert any(row["code"] == "topic_jev_new" and row["source"] == "jev" for row in ledger)
        assert not p.batch_calls and len(p.jev.calls) == 1
        assert {"completion", "join", "topic"} <= p.jev.calls[0]["questions"].keys()
        await tick(p, 30)
        assert len(p.batch_calls) == 1
        choose(p, "topic_0")
        await send(p, "如何设置风扇曲线", "next", user="B")
        assert p.jev.calls[0]["state"]["active_topics"]["topic_0"]["label"] == "显卡散热讨论"
        assert node_topic_id(rt.dag.nodes["next"]) == "first"
        assert rt.routing_state.topics["first"].human_updated_at == p.time_service.time()
        await tick(p, 60)
        assert len(p.batch_calls) == 1 and len(p.jev.calls) == 1
        assert not rt.topic_batch_task
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_only_the_new_topic_receives_another_label_request(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        rt = await send(p, "显卡风扇温度调节方案", "gpu")
        await tick(p, 30)
        await send(p, "今晚烧烤聚餐菜单计划", "food", user="B")
        assert set(rt.routing_state.topics) == {"gpu", "food"}
        await tick(p, 30)
        assert len(p.batch_calls) == 2 and set(p.batch_calls[-1]["topics"]) == {"food"}
        assert len(p.jev.calls) == 2
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_more_than_eight_new_labels_finish_in_later_windows_then_go_idle(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        for i, text in enumerate(("显卡温度调节方案", "番茄种植浇水方案", "烧烤聚餐菜单计划",
            "火车旅行购票安排", "明天会议报告准备", "吉他练习技巧介绍", "小猫饮食健康问题",
            "厨房清洁整理办法", "跑步训练比赛安排")):
            rt = await send(p, text, f"n{i}")
        assert len(rt.routing_state.topics) == 9
        await tick(p, 30)
        assert len(p.batch_calls) == 1 and len(p.batch_calls[0]["topics"]) == 8
        await tick(p, 30)
        assert len(p.batch_calls) == 2 and len(p.batch_calls[1]["topics"]) == 1
        assert all(t.generated_title for t in rt.routing_state.topics.values())
        await tick(p, 120)
        assert len(p.batch_calls) == 2 and not rt.topic_batch_task
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_topic_label_survives_short_window_and_dag_eviction_and_match_renews_hour(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        rt = await send(p, "显卡风扇温度调节方案", "old")
        await tick(p, 30)
        rt.dag.nodes.clear()
        await tick(p, 3400)
        p._prune_idle_sessions(p.time_service.time())
        assert "old" in rt.routing_state.topics
        choose(p, "topic_0")
        await send(p, "之前说的散热方案还有个问题", "followup", user="B")
        assert node_topic_id(rt.dag.nodes["followup"]) == "old"
        assert p.jev.calls[0]["state"]["active_topics"]["topic_0"]["label"] == "显卡散热讨论"
        await tick(p, 200)
        p._prune_idle_sessions(p.time_service.time())
        assert "old" in rt.routing_state.topics and not rt.routing_state.archive.entries
        assert len(p.batch_calls) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_idle_hour_archives_without_messages_and_never_reopens_as_a_candidate(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        rt = await send(p, "显卡风扇温度调节方案", "old")
        await tick(p, 30)
        await tick(p, 3570)
        p._prune_idle_sessions(p.time_service.time())
        assert "old" in rt.routing_state.topics, "exactly one hour is still valid"
        await tick(p, 1)
        p._prune_idle_sessions(p.time_service.time())
        assert not rt.routing_state.topics
        assert rt.routing_state.archive.entries["old"].title == "显卡散热讨论"
        assert p._sessions[rt.session_key] is rt, "a quiet session must retain its bounded archive"
        replay = scene_replay_snapshot(p)
        assert replay["archived_topics"][0]["topic_title"] == "显卡散热讨论"
        assert replay["topic_blocks"][0]["topic_status"] == "archived"
        assert len(p.batch_calls) == 1
        await send(p, "显卡风扇温度调节方案", "fresh", user="B")
        assert p.jev.calls[-1]["state"]["active_topics"] == {}
        assert node_topic_id(rt.dag.nodes["fresh"]) == "fresh"
        assert "old" in rt.routing_state.archive.entries
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_each_topic_expires_independently_and_bot_replies_do_not_refresh_human_activity(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        rt = await send(p, "显卡风扇温度调节方案", "gpu")
        await tick(p, 100)
        await send(p, "今晚烧烤聚餐菜单计划", "food", user="B")
        await tick(p, 3400)
        bot = rt.dag.add_message("bot-reply", rt.bot_id, "显卡散热建议", timestamp=p.time_service.time())
        TopicResolver.remember(rt.routing_state, bot, "gpu", dag=rt.dag)
        await tick(p, 101)
        p._prune_idle_sessions(p.time_service.time())
        assert "gpu" not in rt.routing_state.topics and "food" in rt.routing_state.topics
        assert "gpu" in rt.routing_state.archive.entries
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_incomplete_utterance_does_not_create_or_name_a_topic(batch_plugin):
    p = batch_plugin
    choose(p, "NEW", completion={"type": "choice", "choice": "wait", "confidence": .9})
    try:
        rt = await send(p, "关于显卡散热的问题，等我说完", "unfinished")
        assert not rt.routing_state.topics and not p.batch_calls
        assert p.debounce.is_active(rt.session_key, "A")
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    {"type": "choice", "choice": "NEW", "confidence": .79},
    {"type": "choice", "choice": "NEW", "confidence": True},
    {"type": "choice", "choice": "NEW", "confidence": float("nan")},
    {"type": "choice", "choice": "NEW", "confidence": 1.01},
    {"type": "choice", "choice": "alien", "confidence": .9},
    {"type": "choice", "choice": "KEEP", "confidence": .9},
])
async def test_uncertain_or_unoffered_jev_opinion_does_not_start_label_requests(batch_plugin, answer):
    p = batch_plugin
    choose(p, "NEW")
    p.jev.payload["topic"] = answer
    try:
        rt = await send(p, "显卡风扇温度调节方案", "m")
        await tick(p, 30)
        assert not rt.routing_state.topics and not p.batch_calls
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_jev_topics_are_session_local_and_fragment_assignment_is_atomic(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        first = MockEvent("显卡温度有点高", message_id="one")
        second = MockEvent("如何调整风扇曲线", message_id="two")
        await p.on_group_message(first)
        await p.on_group_message(second)
        await flush(p, second)
        await drain(p)
        rt = p._sessions[first.unified_msg_origin]
        assert node_topic_id(rt.dag.nodes["one"]) == node_topic_id(rt.dag.nodes["two"]) == "one"
        assert len(p.jev.calls) == 1
        other = await send(p, "今晚烧烤聚餐菜单计划", "foreign", group="other")
        assert p.jev.calls[-1]["state"]["active_topics"] == {}
        assert set(other.routing_state.topics) == {"foreign"}
    finally:
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stop", "reset", "config", "routing"])
async def test_late_jev_topic_opinion_cannot_write_after_invalidation(batch_plugin, change):
    p = batch_plugin
    choose(p, "NEW")
    started, release = asyncio.Event(), asyncio.Event()
    original = p.jev.evaluate
    async def blocked(**kwargs):
        started.set()
        await release.wait()
        return await original(**kwargs)
    p.jev.evaluate = blocked
    event = MockEvent("显卡风扇温度调节方案", sender_id="A", message_id="m")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await started.wait()
        rt = p._sessions[event.unified_msg_origin]
        if change == "stop":
            await p.cmd_dynamics_stop(MockEvent("/dynamics_stop", sender_id="A"))
        elif change == "reset":
            p._reset_session_state(rt.session_key)
        elif change == "config":
            await p.save_config_values({"topic_reranker_provider": "new-provider"})
        else:
            rt.dag.nodes["m"].metadata["routing"]["evidence"].append("topic_boundary")
        release.set()
        await drain(p)
        await tick(p, 30)
        assert not rt.routing_state.topics and not p.batch_calls
    finally:
        release.set()
        await p.terminate()


def test_relevant_topic_shortlist_keeps_excerpts_and_excludes_future_topics():
    from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
    from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
    from astrbot_plugin_chat_dynamics.core.turn_decision import TurnContext, MessageSnapshot
    rt = SessionRuntime("r", "g", "r", dag=ConversationDAG())
    node = rt.dag.add_message("current", "u", "显卡散热问题", timestamp=1000)
    old = TopicState("old", generated_title="显卡散热", updated_at=100, created_at=100, summary_excerpts=["显卡风扇调节方案"])
    rt.routing_state.topics["old"] = old
    for i in range(12):
        rt.routing_state.topics[str(i)] = TopicState(str(i), generated_title="烧烤聚餐", updated_at=900+i,
                                                   created_at=900+i, summary_excerpts=["今晚烧烤聚餐菜单计划"])
    rt.routing_state.topics["future"] = replace(old, topic_id="future", created_at=1001, updated_at=1001)
    turn = TurnContext("r", "u", node.text, (MessageSnapshot("current", "u", ""),), (), 0, 0, 1000, False)
    descriptions, questions, mapping = build_topic_task(rt, turn, 1000)
    assert len(descriptions) == 8 and "topic" in questions
    assert sum(bool(d["excerpt"]) for d in descriptions.values()) == 8
    assert old in mapping["topics"].values()
    assert rt.routing_state.topics["future"] not in mapping["topics"].values()


def test_full_active_label_state_fits_jev_budget_without_erasing_current_utterance():
    import json
    from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
    from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
    from astrbot_plugin_chat_dynamics.core.turn_decision import TurnContext, MessageSnapshot
    from astrbot_plugin_chat_dynamics.core.jev_decision import build_state
    rt = SessionRuntime("r", "g", "r", dag=ConversationDAG())
    node = rt.dag.add_message("current", "u", "如何解决之前的散热问题" * 600, timestamp=1000)
    for i in range(80):
        rt.routing_state.topics[str(i)] = TopicState(str(i), generated_title="主题标签" * 6,
            updated_at=900+i, created_at=900+i, summary_excerpts=["讨论细节" * 50])
    turn = TurnContext("r", "u", node.text, (MessageSnapshot("current", "u", ""),), (), 0, 0, 1000, False)
    descriptions, questions, mapping = build_topic_task(rt, turn, 1000)
    state = build_state(turn, active_topics=descriptions, persona_prompt="人设" * 600)
    assert len(descriptions) == 8
    assert 0 < len(state["active_topics"]) < 80
    assert list(state["active_topics"]) == list(descriptions)[:len(state["active_topics"])]
    assert len(questions["topic"]["criteria"]) == 10
    questions, mapping = align_topic_task(state, questions, mapping)
    assert set(questions["topic"]["criteria"]) == set(state["active_topics"]) | {"NEW", "KEEP"}
    assert set(mapping["topics"]) == set(state["active_topics"])
    assert len(json.dumps(state, ensure_ascii=False)) <= 12000
    assert len(state["conversation"]["text"]) > 500


@pytest.mark.asyncio
async def test_new_topic_opinion_preserves_platform_quote_edges(batch_plugin):
    p = batch_plugin
    choose(p, "NEW")
    try:
        rt = await send(p, "显卡风扇温度调节方案", "gpu")
        await tick(p, 100)
        event = MockEvent("说到晚上的烧烤，我准备下菜单", sender_id="B", message_id="food", components=[Reply("gpu")])
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        node = rt.dag.nodes["food"]
        assert node_topic_id(node) == "food"
        assert node.reply_to_id == "gpu" and node.edge_kinds["gpu"] == "reply"
        assert rt.routing_state.topics["gpu"].human_updated_at == 100
        assert rt.routing_state.topics["food"].human_updated_at == 200
    finally:
        await p.terminate()
