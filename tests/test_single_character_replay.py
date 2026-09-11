"""Replay short CJK turns before changing lexical routing evidence.

Single-character concepts currently have no lexical overlap. Keep explicit
lineage and addressivity independent of that limitation; do not promote filler
or ambiguous bystanders merely to improve lexical recall.
"""

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter


def replay(answer, *, quoted=False, mentioned=False):
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG())
    router = ThreadRouter(require_intense_dialogue=False)
    for mid, user, text, stamp in (
        ("pets", "Alice", "你最喜欢养什么宠物？", 1),
        ("money", "Bob", "买车预算还差多少钱？", 2),
    ):
        node = runtime.dag.add_message(mid, user, text, timestamp=stamp)
        router.route(runtime, node, bot_names=("bot",))
    node = runtime.dag.add_message(
        "answer", "Charlie", answer, timestamp=3,
        reply_to_id="pets" if quoted else None,
        mentioned_users=["bot"] if mentioned else [],
    )
    return node, router.route(runtime, node, bot_names=("bot",))


@pytest.mark.parametrize("answer", ["猫", "狗", "车", "钱", "嗯", "哦", "啊"])
def test_unaddressed_single_character_does_not_wake_bot(answer):
    _, result = replay(answer)
    assert not result.bot_is_addressee
    assert not result.parent_message_id


@pytest.mark.parametrize("answer", ["猫", "狗", "猫！"])
def test_single_character_explicit_reply_keeps_human_lineage(answer):
    node, result = replay(answer, quoted=True)
    assert "pets" in node.parent_ids
    assert result.parent_message_id == "pets"
    assert result.addressee_ids == ["Alice"]
    assert not result.bot_is_addressee


@pytest.mark.parametrize("answer", ["猫", "狗", "钱"])
def test_single_character_mention_keeps_bot_addressivity(answer):
    _, result = replay(answer, mentioned=True)
    assert result.bot_is_addressee
    assert "bot" in result.addressee_ids
