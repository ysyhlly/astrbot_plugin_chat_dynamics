"""Topic titles survive ongoing chat and replay shows topics before titles finish."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks, scene_replay_snapshot
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState, TopicState
from .test_jev_decision_layer import JevDouble, answers
from .test_persona_model import BridgeDouble, drain, flush
from .test_plugin_lifecycle import MockEvent, Reply, _plugin
from .test_router_enrichment_concurrency import setup as router_setup


@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.asyncio
async def test_title_survives_another_member_message_without_sending(monkeypatch, shadow):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    p = _plugin({"daily_rhythm_enabled": False, "base_thinking_delay": 0, "shadow_mode": shadow})
    p.persona_engine.bridge = BridgeDouble()
    p.jev = JevDouble(answers(action="ignore", reason="low_value_chatter"))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def model(**kwargs):
        data = json.loads(kwargs["prompt"])
        if "messages" in data:
            calls.append(data)
            entered.set()
            await release.wait()
            return SimpleNamespace(completion_text='{"title":"显卡风扇散热"}')
        return SimpleNamespace(completion_text="UNKNOWN")

    p.context.llm_generate = model
    texts = ["显卡风扇温度太高怎么办", "显卡风扇曲线可以先调高", "显卡风扇调高之后还是很热",
             "显卡风扇还需要检查机箱风道", "显卡风扇风道已经清理好了"]
    try:
        events = []
        for i, text in enumerate(texts):
            if i == 4:
                await asyncio.wait_for(entered.wait(), 1)
                before = scene_replay_snapshot(p)["topic_blocks"]
                assert len(before) == 1 and before[0]["title_status"] == "generating"
            event = MockEvent(text, sender_id="B" if i >= 3 else "AB"[i % 2], message_id=f"m{i}",
                              components=[Reply(f"m{i-1}")] if i else [])
            events.append(event)
            await p.on_group_message(event)
            await flush(p, event)
            await drain(p)
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(p._background_tasks)), 2)
        data = scene_replay_snapshot(p)
        assert len(data["topic_blocks"]) == 1 and not data["empty"]
        block = data["topic_blocks"][0]
        assert block["topic_title"] == "显卡风扇散热" and block["title_status"] == "ready"
        assert block["message_count"] == 5
        assert all(message["text"] == "消息内容已隐藏" for message in block["messages"])
        assert data["topic_content_redacted"] and not data["topic_titles_redacted"]
        assert len(calls) == 1
        assert not any(event.replies_sent for event in events)
    finally:
        release.set()
        await p.terminate()


@pytest.mark.parametrize("topic_provider,reply_provider,expected", [
    ("topic-chat", "reply-chat", "topic-chat"), ("", "reply-chat", "reply-chat"), ("", "", "host-chat"),
])
@pytest.mark.asyncio
async def test_topic_model_selection_and_separate_title_timeout(topic_provider, reply_provider, expected):
    p = _plugin({"topic_reranker_provider": topic_provider, "reply_provider": reply_provider,
                 "topic_reranker_timeout": 1.5, "topic_title_timeout": 12})
    async def host_provider(**kwargs):
        return "host-chat"
    p.context.get_current_chat_provider_id = host_provider
    try:
        reranker, title = p._topic_reranker(), p._topic_reranker(for_display=True)
        assert await title.adapter.resolve_provider_id("room", purpose="title") == expected
        assert reranker.timeout_seconds == 1.5 and title.timeout_seconds == 12
        await p.save_config_values({"shadow_mode": True})
        assert p._topic_reranker() is None
        assert p._topic_reranker(for_display=True) is not None
        await p.save_config_values({"topic_reranker_enabled": False})
        assert p._topic_reranker(for_display=True) is None
    finally:
        await p.terminate()


@pytest.mark.parametrize("show_titles", [False, True])
@pytest.mark.parametrize("show_body", [False, True])
def test_generated_title_visibility_is_independent_of_message_body(show_titles, show_body):
    dag = ConversationDAG()
    # The title belongs to topic state even if the first retained node is blank.
    first = dag.add_message("one", "a", "", timestamp=1, metadata={"routing": {"topic_id": "topic"}})
    dag.add_message("two", "b", "private body", timestamp=2, metadata={"routing": {"topic_id": "topic"}})
    state = RoutingState(topics={"topic": TopicState("topic", generated_title="显卡散热", message_ids=["one", "two"])})
    p = SimpleNamespace(dags={"room": dag}, _sessions={"room": SimpleNamespace(routing_state=state)},
                        console_show_message_content=show_body, replay_show_topic_titles=show_titles)
    block = replay_topic_blocks(p, [], "room")[0]
    assert block["topic_title"] == ("显卡散热" if show_titles else "话题 1")
    assert block["title_status"] == ("ready" if show_titles else "hidden")
    assert block["messages"][1]["text"] == ("private body" if show_body else "消息内容已隐藏")
    assert "topic_title" not in first.metadata, "presentation must not mutate source evidence"


@pytest.mark.parametrize("status,changes,enabled", [
    ("ready", {"generated_title": "简短标题"}, True),
    ("generating", {"title_in_flight": True}, True),
    ("failed", {"title_failures": 1}, True),
    ("disabled", {}, False), ("pending", {}, True),
])
def test_formed_topic_remains_visible_with_a_title_status(status, changes, enabled):
    dag = ConversationDAG()
    dag.add_message("m", "a", "private", metadata={"routing": {"topic_id": "t", "topic_status": "committed"}})
    topic = TopicState("t", message_ids=["m"], **changes)
    p = SimpleNamespace(dags={"room": dag}, _sessions={"room": SimpleNamespace(routing_state=RoutingState(topics={"t": topic}))},
                        console_show_message_content=False, _runtime_config=SimpleNamespace(topic_reranker_enabled=enabled))
    block = replay_topic_blocks(p, [], "room")[0]
    assert block["topic_status"] == "committed" and block["title_status"] == status
    assert block["message_count"] == 1
    assert "private" not in str(block)


@pytest.mark.asyncio
async def test_replay_with_topics_and_no_decisions_is_not_empty_and_title_setting_applies():
    p = _plugin()
    try:
        runtime = p._get_or_create_runtime("room", group_id="room", umo="room")
        runtime.dag.add_message("m", "a", "private body", timestamp=p.time_service.time(),
                                metadata={"routing": {"topic_id": "t", "topic_status": "committed"},
                                          "topic_title": "显卡散热"})
        data = scene_replay_snapshot(p)
        assert not data["events"] and not data["empty"]
        assert data["topic_blocks"][0]["topic_title"] == "显卡散热"
        await p.save_config_values({"replay_show_topic_titles": False})
        data = scene_replay_snapshot(p)
        assert not data["empty"] and data["topic_titles_redacted"]
        assert data["topic_blocks"][0]["topic_title"] == "话题 1"
        assert "显卡散热" not in str(data) and "private body" not in str(data)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_topic_calibration_clears_the_previous_topic_title_in_replay():
    runtime, router, first, node = router_setup()
    async def old_title(**kwargs):
        return "番茄种植"
    await router.title_topic(runtime, node, SimpleNamespace(title=old_title))
    assert node.metadata["topic_title"] == "番茄种植"

    async def recalibrate(**kwargs):
        return SimpleNamespace(choice="A", topic_id=first.msg_id)
    await router.rerank_pending(runtime, node, SimpleNamespace(rerank=recalibrate))
    assert node.metadata["routing"]["topic_id"] == first.msg_id
    assert "topic_title" not in node.metadata
    p = SimpleNamespace(dags={"room": runtime.dag}, _sessions={"room": runtime},
                        console_show_message_content=False)
    block = replay_topic_blocks(p, [], "room")[0]
    assert block["message_count"] == 2 and block["title_status"] == "pending"
    assert "番茄种植" not in str(block)

    async def new_title(**kwargs):
        return "显卡驱动安装"
    await router.title_topic(runtime, first, SimpleNamespace(title=new_title))
    block = replay_topic_blocks(p, [], "room")[0]
    assert block["topic_title"] == "显卡驱动安装" and block["title_status"] == "ready"
