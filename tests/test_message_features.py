from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.message_features import analyze_text, message_features, _analyze_text
from astrbot_plugin_chat_dynamics.core.topic_formation import topic_text


def test_compatibility_views_remain_distinct():
    facts = analyze_text("然后呢")
    assert facts.short_followup
    assert not facts.quoted_subject_followup
    assert analyze_text("为什么失败了").is_question
    assert not analyze_text("为什么失败了").question_ending
    assert analyze_text("顺便查一下配置").answer_boundary
    assert not analyze_text("顺便查一下配置").is_topic_boundary
    assert analyze_text("a" * 20).is_answer_like
    assert not analyze_text("a" * 20).is_short
    assert not analyze_text("a" * 33).is_answer_like


def test_source_changes_and_aliases_are_fresh_and_read_only():
    dag = ConversationDAG()
    node = dag.add_message("m", "u", "助手你怎么看？", timestamp=1)
    before = deepcopy(node)
    assert message_features(node, ("助手",), "bot").is_vocative
    assert node == before
    assert not message_features(node, ("另一个",), "bot").is_vocative
    node.text = "好的"
    node.mentioned_users = ["bot"]
    node.reply_to_id = "old"
    facts = message_features(node, ("助手",), "bot")
    assert facts.is_ack and facts.has_explicit_mention and facts.has_explicit_reply
    assert facts.bot_reference.mention and not facts.is_vocative
    node.metadata["routing"] = {"explicit_mention": True, "bot_is_addressee": True}
    node.mentioned_users = []
    assert not message_features(node).has_explicit_mention
    with pytest.raises(FrozenInstanceError):
        facts.is_ack = False


def test_topic_authored_view_is_not_caption_view():
    dag = ConversationDAG()
    node = dag.add_message("m", "u", "图片里有完整的网络配置方案", timestamp=1)
    node.metadata["topic_source_text"] = "[图片] 好的"
    assert message_features(node).can_start_topic
    assert not message_features(node, text=topic_text(node)).can_start_topic


def test_long_text_is_not_retained_and_cache_is_bounded():
    _analyze_text.cache_clear()
    analyze_text("内容" * 1100)
    assert _analyze_text.cache_info().currsize == 0
    for i in range(520):
        analyze_text(str(i))
    assert _analyze_text.cache_info().currsize == 512
