"""Snapshot context follows explicit edges and bounded relevance supplements."""

from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.persona_engine import snapshot_turn


def _runtime(dag: ConversationDAG, bot_id: str = "bot-42") -> SimpleNamespace:
    return SimpleNamespace(
        dag=dag,
        session_key=dag.session_id,
        bot_id=bot_id,
        epoch=0,
        user_revisions={},
    )


def _snapshot(runtime: SimpleNamespace, node, *, reply_to_id: str = ""):
    parsed = SimpleNamespace(
        message_id=node.msg_id,
        sender_id=node.user_id,
        text=node.text,
        reply_to_id=reply_to_id,
        media_component_types=[],
    )
    result = SimpleNamespace(
        user_id=node.user_id,
        consolidated_text=node.text,
        raw_events=[parsed],
        start_time=node.timestamp,
        metadata={},
        last_event=parsed,
    )
    return snapshot_turn(runtime, result, [node.msg_id], [parsed], True, {}, False)


def test_snapshot_includes_mentioned_bot_parent_without_platform_reply():
    dag = ConversationDAG(session_id="mock:room")
    bot = dag.add_message("bot-answer", "bot-42", "第一点的实现方式是这样。", timestamp=1.0)
    current = dag.add_message(
        "user-followup",
        "user-1",
        "第二点没懂",
        timestamp=2.0,
        mentioned_users=["bot-42"],
    )
    runtime = _runtime(dag)

    assert current.reply_to_id is None
    assert current.parent_ids == {bot.msg_id}
    assert current.edge_kinds[bot.msg_id] == "mention"

    turn = _snapshot(runtime, current)

    assert turn is not None
    assert [message.message_id for message in turn.context.background] == [bot.msg_id]
    assert turn.context.background[0].text == bot.text


def test_snapshot_excludes_semantic_only_parent_edge():
    dag = ConversationDAG(session_id="mock:room")
    semantic_parent = dag.add_message("semantic-parent", "user-2", "语义上相似的旧消息", timestamp=1.0)
    current = dag.add_message("current", "user-1", "一个完全不同的请求", timestamp=2.0)
    assert dag.link_related(current.msg_id, semantic_parent.msg_id, kind="semantic")
    runtime = _runtime(dag)

    turn = _snapshot(runtime, current)

    assert turn is not None
    assert turn.context.background == ()


def test_snapshot_keeps_same_user_background_without_importing_unrelated_semantic_user():
    dag = ConversationDAG(session_id="mock:room")
    same_user = dag.add_message("same-user", "user-1", "我上一条需求的背景", timestamp=1.0)
    semantic_parent = dag.add_message("semantic-parent", "user-2", "语义上相似的旧消息", timestamp=1.5)
    current = dag.add_message("current", "user-1", "一个完全不同的新请求", timestamp=2.0)
    assert dag.link_related(current.msg_id, semantic_parent.msg_id, kind="semantic")
    runtime = _runtime(dag)

    turn = _snapshot(runtime, current)

    assert turn is not None
    assert [message.message_id for message in turn.context.background] == [same_user.msg_id]


def test_snapshot_includes_bot_reply_to_selected_parent_as_background():
    dag = ConversationDAG(session_id="mock:room")
    selected_parent = dag.add_message("user-parent", "user-1", "请先看这个问题", timestamp=1.0)
    bot_reply = dag.add_message(
        "bot-reply",
        "bot-42",
        "这是针对该问题的上一条回答。",
        timestamp=2.0,
        reply_to_id=selected_parent.msg_id,
    )
    current = dag.add_message(
        "current",
        "user-1",
        "请继续说明。",
        timestamp=3.0,
        reply_to_id=selected_parent.msg_id,
    )
    runtime = _runtime(dag)

    turn = _snapshot(runtime, current, reply_to_id=selected_parent.msg_id)

    assert turn is not None
    assert [message.message_id for message in turn.context.background] == [
        selected_parent.msg_id,
        bot_reply.msg_id,
    ]


def test_snapshot_keeps_reply_mapping_and_follows_reply_parent():
    dag = ConversationDAG(session_id="mock:room")
    bot = dag.add_message("bot-reply", "bot-42", "这是上一条回答。", timestamp=1.0)
    current = dag.add_message(
        "user-reply",
        "user-1",
        "请解释上一条回答。",
        timestamp=2.0,
        reply_to_id=bot.msg_id,
    )
    runtime = _runtime(dag)

    turn = _snapshot(runtime, current, reply_to_id=bot.msg_id)

    assert turn is not None
    assert turn.context.messages[0].reply_to == bot.msg_id
    assert [message.message_id for message in turn.context.background] == [bot.msg_id]


def test_snapshot_deduplicates_explicit_parents_and_respects_context_budget():
    dag = ConversationDAG(session_id="mock:room")
    parents = [
        dag.add_message(f"parent-{index}", "bot-42", "x" * 1200, timestamp=float(index))
        for index in range(7)
    ]
    current = dag.add_message(
        "current",
        "user-1",
        "请继续说明。",
        timestamp=7.0,
        reply_to_id=parents[0].msg_id,
    )
    for parent in parents[1:]:
        assert dag.link_related(current.msg_id, parent.msg_id, kind="mention")
    runtime = _runtime(dag)

    turn = _snapshot(runtime, current, reply_to_id=parents[0].msg_id)

    assert turn is not None
    background = turn.context.background
    assert len(background) == 5
    assert len({message.message_id for message in background}) == len(background)
    assert sum(len(message.text) for message in background) == 6000
    assert [message.message_id for message in background] == [
        parent.msg_id for parent in parents[2:]
    ]
