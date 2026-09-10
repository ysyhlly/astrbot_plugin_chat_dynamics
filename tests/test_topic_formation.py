from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter
from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks


def setup():
    return SessionRuntime("room", "g", "room", bot_id="bot", dag=ConversationDAG()), ThreadRouter()


def add(rt, router, mid, user, text, timestamp, reply=None):
    node = rt.dag.add_message(mid, user, text, timestamp=timestamp, reply_to_id=reply)
    return node, router.route(rt, node)


@pytest.mark.parametrize("text", ["[图片]", "[图片][图片]", "[表情包]", "[image]", "😂😂😂😂😂", "哈哈哈哈哈哈哈"])
def test_media_and_reactions_never_form_topic_even_in_burst(text):
    rt, router = setup()
    for i in range(8):
        _, result = add(rt, router, str(i), str(i % 2), text, i)
        assert result.topic_id == ""
        assert result.topic_status == "unformed"
    assert not rt.routing_state.topics
    assert replay_topic_blocks(SimpleNamespace(dags={"room": rt.dag}), [], "room") == []


def test_four_connected_human_turns_form_one_topic_and_backfill():
    rt, router = setup()
    texts = ["显卡风扇温度太高怎么办", "显卡风扇曲线可以先调高", "显卡风扇调高之后还是很热", "显卡风扇还需要检查机箱风道"]
    for i, text in enumerate(texts):
        node, result = add(rt, router, str(i), str(i % 2), text, 10+i, str(i-1) if i else None)
        if i < 3:
            assert not result.topic_id
    topic_id = result.topic_id
    assert topic_id
    assert len(rt.routing_state.topics) == 1
    assert {n.metadata["routing"]["topic_id"] for n in rt.dag.nodes.values()} == {topic_id}
    # Delayed embedding refresh must not undo confirmed history.
    assert router.route(rt, rt.dag.nodes["0"]).topic_id == topic_id
    _, quiet = add(rt, router, "later", "0", "显卡风扇温度今天又变高了", 100)
    assert not quiet.topic_id


def test_single_speaker_or_unrelated_room_activity_does_not_form_topic():
    for texts, users in [
        (["显卡风扇温度" + str(i) for i in range(5)], ["A"] * 5),
        (["今晚一起去吃烧烤", "路由器无法连接网络", "明天会议需要做报告", "小猫最近不爱吃东西"], ["A", "B", "A", "B"]),
    ]:
        rt, router = setup()
        for i, (text, user) in enumerate(zip(texts, users)):
            _, result = add(rt, router, str(i), user, text, i)
            assert not result.topic_id
        assert not rt.routing_state.topics


@pytest.mark.asyncio
async def test_unformed_never_calls_llm_or_title_and_bot_does_not_inflate():
    rt, router = setup()
    class Uncalled:
        async def rerank(self, **kwargs):
            raise AssertionError("unformed chat must not classify")
        async def title(self, **kwargs):
            raise AssertionError("unformed chat must not title")
    for i, user in enumerate(["A", "bot", "bot", "B"]):
        node, result = add(rt, router, str(i), user, "显卡风扇温度配置" + str(i), i, str(i-1) if i else None)
        assert not result.topic_id
        await router.rerank_pending(rt, node, Uncalled())
        await router.title_topic(rt, node, Uncalled())
    assert not rt.routing_state.topics


def test_debounced_fragments_do_not_count_as_independent_turns():
    rt, router = setup()
    for i in range(4):
        node = rt.dag.add_message(str(i), str(i % 2), "显卡风扇温度配置" + str(i), timestamp=i,
                                  reply_to_id=str(i-1) if i else None,
                                  metadata={"turn_id": "batch" + str(i % 2)})
        assert not router.route(rt, node).topic_id
    assert not rt.routing_state.topics


def test_interleaved_connected_bursts_do_not_merge_unrelated_topics():
    rt, router = setup()
    for i in range(4):
        add(rt, router, "gpu"+str(i), "AB"[i % 2], "显卡风扇温度调节方案" + str(i), i*2,
            "gpu"+str(i-1) if i else None)
        add(rt, router, "food"+str(i), "CD"[i % 2], "今晚烧烤聚餐菜单计划" + str(i), i*2+1,
            "food"+str(i-1) if i else None)
    assert len(rt.routing_state.topics) == 2
    assert rt.dag.nodes["gpu3"].metadata["topic_id"] != rt.dag.nodes["food3"].metadata["topic_id"]
