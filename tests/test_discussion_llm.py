import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.discussion_llm import LLMDiscussionJudge
from astrbot_plugin_chat_dynamics.core.discussion_reranker import DiscussionReranker
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter
from astrbot_plugin_chat_dynamics.core.topic_reranker import RerankCandidate, TopicReranker
from .test_rule_topic_router import setup, add, route
from .test_rule_routing_integration import jev_plugin as jev_plugin, legacy_jev_plugin as legacy_jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent


def answer(**changes):
    return {"topic": "a", "topic_confidence": .94, "recipient_status": "MATCH",
            "recipients": ["A"], "recipient_confidence": .92, **changes}


def judge(value=None, *, invoke=None, timeout=1):
    provider = SimpleNamespace(provider_config={"model": "judge"}, text_chat=AsyncMock())

    async def generate(**kwargs):
        if invoke:
            return await invoke(kwargs)
        return SimpleNamespace(role="assistant", completion_text=json.dumps(value or answer()))

    ctx = SimpleNamespace(llm_generate=AsyncMock(side_effect=generate), get_provider_by_id=lambda _: provider)
    return LLMDiscussionJudge(LLMAdapter(ctx, configured_provider_id="judge"), timeout=timeout), ctx, provider


async def classify(j):
    return await j.judge_scoped("风扇换哪个", [RerankCandidate("a", "电脑散热")],
                                history=[{"user_id": "A", "text": "换个风扇"}], author_id="B", umo="room")


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"topic": "outside"}, {"recipients": ["outside"]}, {"recipients": ["B"]},
    {"topic_confidence": float("nan")}, {"topic_confidence": True},
    {"recipient_confidence": -1}, {"recipient_status": "UNKNOWN"},
    {"recipients": ["A", "A"]}, {"topic": []},
])
async def test_untrusted_choices_are_rejected(changes):
    j, _, _ = judge(answer(**changes))
    assert (await classify(j)).reason == "invalid_output"


@pytest.mark.asyncio
async def test_one_chat_call_for_topic_and_recipient_without_rerank_or_title():
    j, ctx, _ = judge()
    result = await classify(j)
    assert result.scores == ((.94, "a"),) and result.recipient_ids == ("A",)
    ctx.llm_generate.assert_awaited_once()
    payload = json.loads(ctx.llm_generate.call_args.kwargs["prompt"])
    assert payload["eligible_recipients"] == ["A"] and "titles" not in payload


@pytest.mark.asyncio
async def test_unknown_and_none_are_distinct():
    j, _, _ = judge(answer(topic="UNKNOWN", recipient_status="UNKNOWN", recipients=[]))
    result = await classify(j)
    assert not result.scores and result.recipient_ids is None
    j, _, _ = judge(answer(topic="NONE", recipient_status="NONE", recipients=[]))
    result = await classify(j)
    assert result.scores == ((0.0, "a"),) and result.recipient_ids == ()


@pytest.mark.asyncio
async def test_provider_replacement_discards_answer():
    j, ctx, _ = judge()

    # The captured getter consults a mutable binding, as the real context does.
    bindings = [SimpleNamespace(provider_config={"model": "old"})]
    ctx.get_provider_by_id = lambda _: bindings[0]

    async def rebind(**kwargs):
        bindings[0] = SimpleNamespace(provider_config={"model": "other"})
        return SimpleNamespace(role="assistant", completion_text=json.dumps(answer()))

    ctx.llm_generate.side_effect = rebind
    assert (await classify(j)).reason == "provider_changed"


@pytest.mark.asyncio
async def test_stop_or_history_change_during_call_cannot_commit():
    for mutation in ("stop", "text", "config"):
        rt, router = setup()
        add(rt, router, "a", "A", "电脑散热需要换风扇")
        node = add(rt, router, "b", "B", "机箱风扇用哪个", t=1)
        current = [True]

        async def invoke(kwargs):
            assert not rt.state_lock.locked()
            if mutation == "stop":
                rt.topic_stop_revisions["B"] = 1
            elif mutation == "text":
                rt.dag.nodes["a"].text = "周末聚餐"
            else:
                current[0] = False
            return SimpleNamespace(role="assistant", completion_text=json.dumps(answer()))

        j, _, _ = judge(invoke=invoke)
        await router.rerank_pending(rt, node, j, is_current=lambda: current[0])
        assert route(node)["topic_status"] != "committed"
        assert "recipient_llm_match" not in route(node)["evidence"]


@pytest.mark.asyncio
async def test_llm_assigns_person_and_topic_without_sentence_edge_or_duplicate_call():
    rt, router = setup()
    add(rt, router, "a", "A", "电脑散热需要换风扇")
    node = add(rt, router, "b", "B", "机箱风扇用哪个", t=1)
    j, ctx, _ = judge()
    await router.rerank_pending(rt, node, j)
    await router.rerank_pending(rt, node, j)
    assert route(node)["addressee_ids"] == ["A"] and route(node)["topic_id"] == "a"
    assert route(node)["parent_message_id"] == "" and not node.parent_ids
    assert "topic_llm_judgment" in route(node)["evidence"]
    ctx.llm_generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_mentions_survive_conflicting_llm_answer():
    rt, router = setup()
    add(rt, router, "a", "A", "电脑散热需要换风扇")
    node = add(rt, router, "b", "B", "机箱风扇用哪个", t=1, mentions=["C"])
    j, _, _ = judge()
    await router.rerank_pending(rt, node, j)
    assert route(node)["addressee_ids"] == ["C"]
    assert route(node)["topic_id"] == "a"


@pytest.mark.asyncio
async def test_recipient_judgment_does_not_require_a_topic_candidate():
    rt, router = setup()
    add(rt, router, "a", "A", "嗯")
    node = add(rt, router, "b", "B", "好", t=1)
    j, _, _ = judge(answer(topic="UNKNOWN"))
    await router.rerank_pending(rt, node, j)
    assert route(node)["addressee_ids"] == ["A"] and not rt.routing_state.topics


@pytest.mark.asyncio
async def test_timeout_retains_rule_result_and_cancellation_propagates():
    async def slow(kwargs):
        await asyncio.sleep(10)

    rt, router = setup()
    add(rt, router, "a", "A", "电脑风扇更换", mentions=["B"])
    node = add(rt, router, "b", "B", "机箱风扇换哪个", t=1)
    j, _, _ = judge(invoke=slow, timeout=.01)
    await router.rerank_pending(rt, node, j)
    assert route(node)["addressee_ids"] == ["A"]
    assert route(node)["topic_status"] != "committed"
    task = asyncio.create_task(classify(j))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_existing_configuration_keeps_original_mode():
    cfg, _ = parse_runtime_config({"discussion_reranker_provider": "existing"})
    assert cfg.discussion_judgment_mode == "rerank" and cfg.discussion_reranker_provider == "existing"
    cfg, _ = parse_runtime_config({"discussion_judgment_mode": "invalid", "discussion_llm_timeout": 100})
    assert cfg.discussion_judgment_mode == "rerank" and cfg.discussion_llm_timeout == 3


@pytest.mark.asyncio
async def test_mode_switch_preserves_model_choices_and_independent_names(jev_plugin):
    p, _ = jev_plugin
    try:
        p.config.update(discussion_judgment_mode="llm", discussion_llm_provider="judge",
                        discussion_reranker_provider="native-rank", topic_reranker_provider="title-model")
        p._sync_runtime_from_config()
        assert p.thread_router.judgment_mode == "llm"
        assert isinstance(p._topic_reranker(), LLMDiscussionJudge)
        assert p._topic_reranker().adapter.configured_provider_id == "judge"
        assert isinstance(p._topic_reranker(for_display=True), TopicReranker)
        assert p._topic_reranker(for_display=True).adapter.configured_provider_id == "title-model"
        p.config["discussion_judgment_mode"] = "rerank"
        p._sync_runtime_from_config()
        assert isinstance(p._topic_reranker(), DiscussionReranker)
        assert p._runtime_config.discussion_llm_provider == "judge"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_panel_rejects_rerank_as_llm_and_exposes_saved_mode(jev_plugin):
    from astrbot_plugin_chat_dynamics.core.config_panel import ConfigPanel
    p, _ = jev_plugin
    try:
        panel = ConfigPanel(p)
        p.context.get_provider_by_id = lambda _: SimpleNamespace(rerank=AsyncMock())
        with pytest.raises(ValueError, match="普通聊天模型"):
            await panel.save_config_values({"discussion_llm_provider": "native-rank"})
        assert p._runtime_config.discussion_llm_provider == ""
        p.context.get_provider_by_id = lambda _: SimpleNamespace(text_chat=AsyncMock())
        await panel.save_config_values({"discussion_judgment_mode": "llm", "discussion_llm_provider": "judge"})
        values = panel.get_config_panel()["effective"]
        assert values["discussion_judgment_mode"] == "llm" and values["discussion_llm_provider"] == "judge"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_same_person_in_two_discussions_does_not_merge_them():
    rt, router = setup()
    router.judgment_mode = "llm"
    add(rt, router, "a", "A", "电脑显卡散热方案")
    add(rt, router, "ab", "B", "机箱散热风扇", t=1, quote="a")
    add(rt, router, "ac", "C", "显卡温度太高", t=2, quote="a")
    dinner = add(rt, router, "d", "C", "周末聚餐去哪家", t=3)
    j, _, _ = judge(answer(topic="NONE", recipient_status="UNKNOWN", recipients=[]))
    await router.rerank_pending(rt, dinner, j)
    add(rt, router, "dd", "D", "周末吃火锅", t=4, quote="d")
    add(rt, router, "de", "E", "一起吃饭", t=5, quote="d")
    back = add(rt, router, "back", "C", "显卡风扇换哪个", t=6)
    j, _, _ = judge()
    await router.rerank_pending(rt, back, j)
    assert route(back)["topic_id"] == "a"
    assert set(rt.routing_state.topics) == {"a", "d"}
    assert rt.routing_state.topics["a"].participants == {"A", "B", "C"}
    assert rt.routing_state.topics["d"].participants == {"C", "D", "E"}


@pytest.mark.asyncio
async def test_llm_recipient_is_ready_before_participation_decision(jev_plugin):
    p, _ = jev_plugin
    try:
        p.config.update(discussion_judgment_mode="llm", discussion_llm_provider="judge")
        p._sync_runtime_from_config()
        first = MockEvent("电脑机箱散热该怎么调整", message_id="a")
        await p.on_group_message(first)
        await flush(p, first)
        await drain(p)
        runtime = p._sessions[first.unified_msg_origin]
        author = runtime.dag.nodes["a"].user_id
        original = p._topic_reranker
        j, ctx, _ = judge(answer(recipients=[author]))
        p._topic_reranker = lambda **kwargs: original(**kwargs) if kwargs.get("for_display") else j
        second = MockEvent("机箱风扇应该往哪边吹", message_id="b", sender_id="another")
        await p.on_group_message(second)
        await flush(p, second)
        await drain(p)
        assert route(runtime.dag.nodes["b"])["addressee_ids"] == [author]
        assert runtime.jev_decision["recipient_source"] == "llm"
        assert runtime.jev_addressees["b"]["user_id"] == author
        ctx.llm_generate.assert_awaited_once()
    finally:
        await p.terminate()
