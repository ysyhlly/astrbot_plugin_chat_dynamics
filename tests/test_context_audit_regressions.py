"""Regression coverage for relation scope, temporal visibility and captured quotes."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import build_questions, build_state
from astrbot_plugin_chat_dynamics.core.persona_engine import refresh_topic_context, snapshot_turn
from astrbot_plugin_chat_dynamics.core.session_runtime import RoutingState
from astrbot_plugin_chat_dynamics.core.topic_jev import build_topic_task
from astrbot_plugin_chat_dynamics.core.topic_resolution import TopicResolver
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from astrbot_plugin_chat_dynamics.tests.test_persona_explicit_context import _runtime, _snapshot
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult


def route(node, topic="deployment", **extra):
    node.metadata["routing"] = {"topic_id": topic, "topic_confidence": .9,
                                "topic_status": "committed", "topic_ambiguous": False, **extra}
    return node


def test_mention_history_cannot_expand_a_confirmed_topic_to_an_unrelated_topic():
    dag = ConversationDAG("mention-topic")
    route(dag.add_message("constraint", "carol", "工具必须离线安装", timestamp=10))
    route(dag.add_message("alice-dinner", "alice", "今晚吃火锅", timestamp=12), "dinner")
    route(dag.add_message("eve-dinner", "eve", "我来订饭店", timestamp=13), "dinner")
    current = route(dag.add_message("current", "bob", "部署工具怎么安装？", timestamp=20,
                                    mentioned_users=["alice"]))
    background = _snapshot(_runtime(dag), current).context.background
    # A recent message of the mentioned account is not a quote of that dinner task.
    assert "eve-dinner" not in {m.message_id for m in background}


@pytest.mark.parametrize("stamp", [19, 20], ids=["backdated", "same-timestamp"])
def test_queued_topic_choices_use_the_original_message_watermark(stamp):
    dag = ConversationDAG("late-topic")
    current = dag.add_message("current", "bob", "解释部署方法", timestamp=20)
    runtime = _runtime(dag)
    runtime.routing_state = RoutingState()
    item = _snapshot(runtime, current)
    late = dag.add_message("late", "eve", "后到消息：另一个人的密码重置请求", timestamp=stamp)
    TopicResolver.remember(runtime.routing_state, late, late.msg_id, dag=dag)
    runtime.routing_state.topics[late.msg_id].generated_title = "后到的话题"
    assert late.msg_id not in item.context_node_ids
    descriptions, _, _ = build_topic_task(runtime, item.context, 20)
    assert not descriptions


def test_refresh_preserves_a_captured_quote_after_dag_eviction():
    dag = ConversationDAG("evicted-quote")
    quote = dag.add_message("quoted-requirements", "alice", "必须离线，不能使用GPU", timestamp=1)
    current = dag.add_message("current", "bob", "请按引用内容写方案", timestamp=20, reply_to_id=quote.msg_id)
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    assert item.context.messages[0].semantics.quoted_author_id == "alice"
    dag.prune(ttl_seconds=10, current_time=25)
    assert dag.get_node("current") is current
    route(current, semantic_topic_decision=True)
    refreshed = refresh_topic_context(runtime, item)
    assert quote.msg_id in {m.message_id for m in refreshed.context.background}
    assert refreshed.context.messages[0].semantics.quoted_author_id == "alice"


@pytest.mark.parametrize("prompt_size", [0, 4000], ids=["many-topics", "long-guidance"])
def test_budget_preserves_the_only_confirmed_topic_constraint(prompt_size):
    dag = ConversationDAG("constraint-budget")
    route(dag.add_message("constraint", "carol", "必须离线，不能使用GPU。" + "c" * 200, timestamp=1))
    nodes = [route(dag.add_message(f"current-{i}", "bob", "请解释安装" + "t" * 1000,
                                   timestamp=10 + i)) for i in range(4)]
    events = [SimpleNamespace(message_id=n.msg_id, sender_id=n.user_id, text=n.text,
                              reply_to_id="", media_component_types=[]) for n in nodes]
    result = SimpleNamespace(user_id="bob", consolidated_text="\n".join(n.text for n in nodes),
                             raw_events=events, start_time=10, metadata={}, last_event=events[-1])
    turn = snapshot_turn(_runtime(dag), result, [n.msg_id for n in nodes], events, False, {}, False).context
    assert [m.message_id for m in turn.background] == ["constraint"]
    kwargs = ({"decision_prompt": "g" * prompt_size} if prompt_size else {
        "active_topics": {f"topic_{i}": {"label": "l" * 48, "excerpt": "e" * 180 if i < 8 else ""}
                          for i in range(80)}})
    state = build_state(turn, persona_prompt="p" * 1200, **kwargs)
    assert "constraint" in {m["message_id"] for m in state["conversation"]["background"]}


def test_target_options_always_include_the_newest_fragment():
    messages = tuple(MessageSnapshot(f"m{i}", "bob", "") for i in range(12))
    turn = TurnContext("room", "bob", "若干碎片后，最新一条提出实际问题", messages, (), 0, 0, 0, False)
    question = build_questions(turn)["target"]
    assert "m11" in question["criteria"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_mention_ancestry_cannot_authorise_other_members_reply_branches(confirmed):
    dag = ConversationDAG("mention-branch")
    dinner = route(dag.add_message("dinner", "alice", "今晚吃火锅", timestamp=10), "dinner")
    route(dag.add_message("reservation", "eve", "饭店订好了", timestamp=12,
                          reply_to_id=dinner.msg_id), "dinner")
    current = dag.add_message("current", "bob", "请帮我看部署方案", timestamp=20, mentioned_users=["alice"])
    if confirmed:
        route(current)
    ids = {m.message_id for m in _snapshot(_runtime(dag), current).context.background}
    assert "reservation" not in ids
    assert ("dinner" in ids) is not confirmed


def test_accepted_inference_can_still_retrieve_other_members_confirmed_topic_conditions():
    dag = ConversationDAG("inferred-topic")
    parent = route(dag.add_message("question", "alice", "工具怎么安装？", timestamp=10))
    constraint = route(dag.add_message("constraint", "carol", "目标机器不能联网", timestamp=12))
    current = dag.add_message("current", "bob", "继续给个步骤", timestamp=20)
    assert dag.link_inferred_reply(current.msg_id, parent.msg_id, .9, "semantic_reply")
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == [parent.msg_id, constraint.msg_id]


def test_queued_topic_description_is_detached_from_later_labels_and_profile_changes():
    dag = ConversationDAG("frozen-topic")
    prior = route(dag.add_message("prior", "alice", "原来的部署任务只能离线运行", timestamp=10))
    current = dag.add_message("current", "bob", "解释部署方案", timestamp=20)
    runtime = _runtime(dag)
    runtime.routing_state = RoutingState()
    TopicResolver.remember(runtime.routing_state, prior, "deployment", dag=dag)
    topic = runtime.routing_state.topics["deployment"]
    topic.generated_title = "原部署任务"
    item = _snapshot(runtime, current)
    topic.generated_title = topic.label = "后来生成的其他标题"
    late = dag.add_message("late", "eve", "后来加入的密码重置请求", timestamp=19)
    TopicResolver.remember(runtime.routing_state, late, "deployment", dag=dag)
    descriptions, _, mapping = build_topic_task(runtime, item.context, 20)
    assert descriptions == {"topic_0": {"label": "原部署任务", "excerpt": prior.text}}
    assert mapping["topics"]["topic_0"] is topic


def test_refresh_preserves_a_pruned_quote_chain_and_adds_only_visible_topic_conditions():
    dag = ConversationDAG("quote-chain")
    root = dag.add_message("root", "carol", "原任务必须离线运行", timestamp=1)
    quote = dag.add_message("quote", "alice", "请按原任务准备方案", timestamp=2, reply_to_id=root.msg_id,
                            metadata={"sender_name": "阿青", "sender_platform": "qq"})
    constraint = route(dag.add_message("constraint", "dave", "只有CPU可用", timestamp=17))
    current = dag.add_message("current", "bob", "请解释引用中的部署方案", timestamp=20, reply_to_id=quote.msg_id)
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    before = item.context.learning_payload()
    dag.prune(ttl_seconds=10, current_time=25)
    route(current, semantic_topic_decision=True)
    refreshed = refresh_topic_context(runtime, item)
    assert [m.message_id for m in refreshed.context.background] == [root.msg_id, quote.msg_id, constraint.msg_id]
    assert refreshed.context.messages[0].semantics.quoted_author_id == "alice"
    assert refreshed.context.messages[0].quoted_author_name == "阿青"
    assert refreshed.context.messages[0].semantics.parent_message_id == quote.msg_id
    assert item.context.learning_payload() == before
    assert sum(len(m.text) for m in refreshed.context.background) <= 6000


@pytest.mark.asyncio
async def test_jev_receives_frozen_topics_when_the_graph_advances_before_decision(jev_plugin):
    p, bridge = jev_plugin
    event = MockEvent("小助手帮我解释怎么安装", message_id="current", sender_id="bob", is_at_or_wake_command=True)
    p.jev.payload = answers(topic={"type": "choice", "choice": "topic_0", "confidence": .9})
    try:
        await p.on_group_message(event)
        runtime = p._sessions[event.unified_msg_origin]
        now = p.time_service.time()
        prior = route(runtime.dag.add_message("prior", "alice", "部署任务必须离线安装", timestamp=now - 3))
        TopicResolver.remember(runtime.routing_state, prior, "deployment", dag=runtime.dag)
        topic = runtime.routing_state.topics["deployment"]
        topic.generated_title = "原部署任务"
        original_snapshot = bridge.snapshot
        advanced = False

        async def advance_graph(raw_event):
            nonlocal advanced
            persona = await original_snapshot(raw_event)
            if not advanced:
                advanced = True
                cutoff = runtime.dag.nodes["current"].timestamp
                late = route(runtime.dag.add_message("late", "eve", "后来的密码重置请求", timestamp=cutoff - 1))
                TopicResolver.remember(runtime.routing_state, late, "deployment", dag=runtime.dag)
                topic.generated_title = topic.label = "后来生成的标题"
            return persona

        bridge.snapshot = advance_graph
        await flush(p, event)
        await drain(p)
        assert len(p.jev.calls) == len(bridge.requests) == 1
        assert p.jev.calls[0]["state"]["active_topics"] == {
            "topic_0": {"label": "原部署任务", "excerpt": prior.text}}
        assert "late" not in {m["message_id"] for m in bridge.requests[0][0]["conversation"]["background"]}
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_jev_can_attach_a_many_fragment_reply_to_the_latest_platform_message(jev_plugin, monkeypatch):
    p, bridge = jev_plugin
    events = [MockEvent(f"安装补充{i}，请帮我看一下", message_id=f"fragment-{i}",
                        is_at_or_wake_command=i == 0) for i in range(12)]
    p.jev.payload = answers(target={"type": "choice", "choice": events[-1].message_id, "confidence": .9})
    sent = []

    async def record_send(runtime, event, fragment, *, reply_to_id=None):
        sent.append(reply_to_id)
        return SendResult(success=True, message_id="sent")

    monkeypatch.setattr(p, "_send_owned", record_send)
    try:
        for event in events:
            await p.on_group_message(event)
        await flush(p, events[-1])
        await drain(p)
        assert len(p.jev.calls) == len(bridge.requests) == 1
        assert list(p.jev.calls[0]["questions"]["target"]["criteria"]) == [f"fragment-{i}" for i in range(4, 12)]
        assert bridge.requests[0][0]["response_plan"]["target_message_ids"][0] == "fragment-11"
        assert sent == ["fragment-11"]
    finally:
        await p.terminate()


def test_a_quoted_fragment_carries_the_earlier_fragments_of_its_original_turn():
    dag = ConversationDAG("fragment-quote")
    root = dag.add_message("first-fragment", "alice", "部署环境必须离线，不能使用GPU", timestamp=1,
                           metadata={"turn_id": "original", "turn_index": 0})
    quote = dag.add_message("last-fragment", "alice", "请按这些限制给出安装方案", timestamp=2,
                            metadata={"turn_id": "original", "turn_index": 1})
    assert dag.link_related(quote.msg_id, root.msg_id, kind="fragment")
    current = dag.add_message("current", "bob", "请回答引用中的部署请求", timestamp=400,
                              reply_to_id=quote.msg_id)
    ids = {m.message_id for m in _snapshot(_runtime(dag), current).context.background}
    assert ids == {root.msg_id, quote.msg_id}


def test_a_quote_fragment_does_not_pull_an_unrelated_same_author_turn():
    dag = ConversationDAG("fragment-control")
    dag.add_message("unrelated", "alice", "晚上一起吃饭", timestamp=1,
                    metadata={"turn_id": "other-turn"})
    quote = dag.add_message("quoted", "alice", "部署方案", timestamp=2,
                            metadata={"turn_id": "quoted-turn"})
    current = dag.add_message("current", "bob", "请解释引用里的请求", timestamp=400,
                              reply_to_id=quote.msg_id)
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == [quote.msg_id]


def test_a_same_timestamp_future_member_cannot_supply_the_queued_topic_title():
    dag = ConversationDAG("title-watermark")
    prior = route(dag.add_message("prior", "alice", "部署只能离线运行", timestamp=10))
    current = dag.add_message("current", "bob", "解释一下部署方法", timestamp=20)
    runtime = _runtime(dag)
    runtime.routing_state = RoutingState()
    TopicResolver.remember(runtime.routing_state, prior, "deployment", dag=dag)
    late = route(dag.add_message("late", "eve", "后到的密码重置请求", timestamp=20))
    TopicResolver.remember(runtime.routing_state, late, "deployment", dag=dag)
    topic = runtime.routing_state.topics["deployment"]
    topic.generated_title = topic.label = "后到的密码重置请求"
    item = _snapshot(runtime, current)
    assert late.msg_id not in item.context.visible_node_ids
    descriptions, _, _ = build_topic_task(runtime, item.context, 20)
    assert descriptions["topic_0"]["excerpt"] == prior.text
    assert "密码重置" not in descriptions["topic_0"]["label"]


def test_refresh_preserves_an_evicted_constraint_when_jev_confirms_the_same_topic():
    dag = ConversationDAG("evicted-topic-constraint", max_nodes=4)
    constraint = route(dag.add_message("constraint", "carol", "只能离线运行，不能使用GPU", timestamp=1))
    current = route(dag.add_message("current", "bob", "请给出部署方案", timestamp=20))
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    assert [m.message_id for m in item.context.background] == [constraint.msg_id]
    for index in range(3):
        dag.add_message(f"future-{index}", "eve", "后来的其他任务", timestamp=21 + index)
    assert dag.get_node(constraint.msg_id) is None
    route(current, semantic_topic_decision=True, topic_confidence=.95)
    refreshed = refresh_topic_context(runtime, item)
    assert refreshed.context.messages[0].semantics.topic_id == "deployment"
    assert [m.message_id for m in refreshed.context.background] == [constraint.msg_id]
    assert refreshed.context.background[0].text == constraint.text


def test_refresh_drops_an_evicted_condition_when_jev_selects_a_different_topic():
    dag = ConversationDAG("evicted-condition-control")
    route(dag.add_message("constraint", "carol", "只能离线安装", timestamp=1))
    current = route(dag.add_message("current", "bob", "请给出方案", timestamp=20))
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    dag.prune(ttl_seconds=10, current_time=25)
    route(current, topic="dinner", semantic_topic_decision=True)
    assert refresh_topic_context(runtime, item).context.background == ()


def test_refresh_keeps_a_same_topic_condition_when_its_live_node_still_exists():
    dag = ConversationDAG("live-condition-control")
    route(dag.add_message("constraint", "carol", "只能离线运行，不能使用GPU", timestamp=10))
    current = route(dag.add_message("current", "bob", "请给出部署方案", timestamp=20))
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    route(current, semantic_topic_decision=True, topic_confidence=.95)
    refreshed = refresh_topic_context(runtime, item)
    assert refreshed.context.background[0].text == "只能离线运行，不能使用GPU"


def test_a_strictly_future_topic_update_uses_a_visible_excerpt_for_its_label():
    dag = ConversationDAG("strict-future-title-control")
    prior = route(dag.add_message("prior", "alice", "部署只能离线运行", timestamp=10))
    current = dag.add_message("current", "bob", "解释一下部署方法", timestamp=20)
    runtime = _runtime(dag)
    runtime.routing_state = RoutingState()
    TopicResolver.remember(runtime.routing_state, prior, "deployment", dag=dag)
    late = route(dag.add_message("late", "eve", "后到的密码重置请求", timestamp=21))
    TopicResolver.remember(runtime.routing_state, late, "deployment", dag=dag)
    topic = runtime.routing_state.topics["deployment"]
    topic.generated_title = topic.label = "后到的密码重置请求"
    descriptions, _, _ = build_topic_task(runtime, _snapshot(runtime, current).context, 21)
    assert descriptions["topic_0"] == {"label": prior.text, "excerpt": prior.text}


def test_refresh_still_keeps_a_platform_quote_after_automatic_eviction():
    dag = ConversationDAG("platform-quote-control", max_nodes=4)
    quote = dag.add_message("quote", "carol", "只能离线运行，不能使用GPU", timestamp=1)
    current = dag.add_message("current", "bob", "请给出部署方案", timestamp=20, reply_to_id=quote.msg_id)
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    for index in range(3):
        dag.add_message(f"future-{index}", "eve", "后来的其他任务", timestamp=21 + index)
    route(current, semantic_topic_decision=True)
    refreshed = refresh_topic_context(runtime, item)
    assert [m.message_id for m in refreshed.context.background] == [quote.msg_id]
    assert refreshed.context.messages[0].semantics.quoted_author_id == "carol"


@pytest.mark.parametrize("invalid", ["other-turn", "other-author", "missing-turn", "forward-index", "invalid-index"])
def test_fragment_edges_cannot_borrow_an_unrelated_or_unordered_piece(invalid):
    dag = ConversationDAG("invalid-fragment")
    root = dag.add_message("root", "alice", "别人的条件", timestamp=1,
                           metadata={"turn_id": "original", "turn_index": 0})
    quote = dag.add_message("quote", "alice", "部署请求", timestamp=2,
                            metadata={"turn_id": "original", "turn_index": 1})
    if invalid == "other-turn":
        root.metadata["turn_id"] = "other"
    elif invalid == "other-author":
        root.user_id = "eve"
    elif invalid == "missing-turn":
        root.metadata.pop("turn_id")
    elif invalid == "forward-index":
        root.metadata["turn_index"] = 2
    else:
        root.metadata["turn_index"] = True
    assert dag.link_related(quote.msg_id, root.msg_id, kind="fragment")
    current = dag.add_message("current", "bob", "请解释引用中的部署任务", timestamp=400, reply_to_id=quote.msg_id)
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == [quote.msg_id]


def test_a_long_quoted_turn_keeps_fragments_and_the_first_fragments_platform_parent():
    dag = ConversationDAG("long-fragment-turn")
    original = dag.add_message("original", "carol", "离线安装，禁止联网下载", timestamp=1)
    nodes = []
    for index in range(12):
        node = dag.add_message(f"piece-{index}", "alice", f"补充安装条件 {index}", timestamp=2 + index,
                               reply_to_id=original.msg_id if index == 0 else None,
                               metadata={"turn_id": "quoted-turn", "turn_index": index})
        if nodes:
            assert dag.link_related(node.msg_id, nodes[-1].msg_id, kind="fragment")
        nodes.append(node)
    current = dag.add_message("current", "bob", "请解释这组部署需求", timestamp=400, reply_to_id=nodes[-1].msg_id)
    background = _snapshot(_runtime(dag), current).context.background
    assert [m.message_id for m in background] == [original.msg_id, *(n.msg_id for n in nodes)]
    first_piece = next(m for m in background if m.message_id == nodes[0].msg_id)
    assert any(p.relation == "fragment" and "same_turn_fragment" in p.evidence for p in first_piece.context_evidence)
    assert all(len(m.text) <= 1200 for m in background)
    assert sum(len(m.text) for m in background) <= 6000


def test_fragment_expansion_preserves_the_conversational_parent_depth_limit():
    dag = ConversationDAG("quote-depth")
    previous = None
    for index in range(10):
        previous = dag.add_message(f"quote-{index}", "alice", "原始需求", timestamp=index + 1,
                                    reply_to_id=previous.msg_id if previous else None)
    current = dag.add_message("current", "bob", "请说明引用内容", timestamp=400, reply_to_id=previous.msg_id)
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == [f"quote-{i}" for i in range(2, 10)]


@pytest.mark.parametrize("relation", ["mention", "inferred_reply"])
def test_fragment_expansion_keeps_the_selection_uncertainty_of_its_entry_path(relation):
    dag = ConversationDAG("weak-fragment")
    root = dag.add_message("root", "alice", "只能离线安装", timestamp=1,
                           metadata={"turn_id": "original", "turn_index": 0})
    quote = dag.add_message("quote", "alice", "请按条件部署", timestamp=2,
                            metadata={"turn_id": "original", "turn_index": 1})
    assert dag.link_related(quote.msg_id, root.msg_id, kind="fragment")
    current = dag.add_message("current", "bob", "请继续", timestamp=20)
    if relation == "mention":
        assert dag.link_related(current.msg_id, quote.msg_id, kind=relation)
    else:
        assert dag.link_inferred_reply(current.msg_id, quote.msg_id, .9, "semantic_reply")
    background = _snapshot(_runtime(dag), current).context.background
    assert [m.message_id for m in background] == [root.msg_id, quote.msg_id]
    fragment_proof = background[0].context_evidence[0]
    entry_proof = background[1].context_evidence[0]
    assert fragment_proof.priority == entry_proof.priority > 0


def test_refresh_keeps_captured_facts_for_a_confirmed_topic_even_when_live_text_changes():
    dag = ConversationDAG("captured-condition")
    constraint = route(dag.add_message("constraint", "carol", "只能离线运行，不能使用GPU", timestamp=10,
                                     metadata={"sender_name": "原作者"}))
    current = route(dag.add_message("current", "bob", "请给出部署方案", timestamp=20))
    runtime = _runtime(dag)
    item = _snapshot(runtime, current)
    original = item.context.learning_payload()
    constraint.text = "后来改成了联网方案"
    constraint.metadata["sender_name"] = "后来改名"
    route(current, semantic_topic_decision=True, topic_confidence=.95)
    refreshed = refresh_topic_context(runtime, item)
    assert refreshed.context.background[0].text == "只能离线运行，不能使用GPU"
    assert refreshed.context.background[0].author_name == "原作者"
    assert item.context.learning_payload() == original


def test_legacy_topic_capture_also_excludes_same_timestamp_future_sources_from_titles():
    dag = ConversationDAG("legacy-title")
    prior = route(dag.add_message("prior", "alice", "原任务只能离线安装", timestamp=10))
    current = dag.add_message("current", "bob", "请解释部署方法", timestamp=20)
    runtime = _runtime(dag)
    runtime.routing_state = RoutingState()
    TopicResolver.remember(runtime.routing_state, prior, "deployment", dag=dag)
    late = dag.add_message("late", "eve", "后来的密码重置请求", timestamp=20)
    TopicResolver.remember(runtime.routing_state, late, "deployment", dag=dag)
    runtime.routing_state.topics["deployment"].generated_title = "密码重置任务"
    turn = replace(_snapshot(runtime, current).context, topic_candidates=None, visible_node_ids=None)
    descriptions, _, _ = build_topic_task(runtime, turn, 20)
    assert descriptions["topic_0"] == {"label": prior.text, "excerpt": prior.text}


@pytest.mark.asyncio
async def test_reply_agent_keeps_the_same_topic_condition_evicted_during_the_jev_request(jev_plugin, monkeypatch):
    p, bridge = jev_plugin
    event = MockEvent("小助手请给出部署方案", message_id="current", sender_id="bob", is_at_or_wake_command=True)
    p.jev.payload = answers(topic={"type": "choice", "choice": "topic_0", "confidence": .95})
    route_message = p._route_message

    def confirmed_local_route(runtime, node):
        result = route_message(runtime, node)
        if node.msg_id == event.message_id:
            route(node, addressee_ids=[runtime.bot_id], addressee_confidence=.9, addressee_ambiguous=False)
        return result

    monkeypatch.setattr(p, "_route_message", confirmed_local_route)
    try:
        await p.on_group_message(event)
        runtime = p._sessions[event.unified_msg_origin]
        constraint = route(runtime.dag.add_message("constraint", "carol", "必须离线，不能使用GPU",
                                                  timestamp=p.time_service.time() - 2))
        TopicResolver.remember(runtime.routing_state, constraint, "deployment", dag=runtime.dag)
        evaluate = p.jev.evaluate

        async def evict_during_evaluation(**kwargs):
            assert [m["message_id"] for m in kwargs["state"]["conversation"]["background"]] == [constraint.msg_id]
            runtime.dag.prune(max_nodes=1, current_time=p.time_service.time())
            return await evaluate(**kwargs)

        p.jev.evaluate = evict_during_evaluation
        await flush(p, event)
        await drain(p)
        assert len(p.jev.calls) == len(bridge.requests) == 1
        assert runtime.dag.get_node(constraint.msg_id) is None
        background = bridge.requests[0][0]["conversation"]["background"]
        assert [m["message_id"] for m in background] == [constraint.msg_id]
        assert background[0]["text"] == "必须离线，不能使用GPU"
    finally:
        await p.terminate()
