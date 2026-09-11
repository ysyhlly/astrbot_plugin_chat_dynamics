from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from astrbot_plugin_chat_dynamics.core.bot_identity import BotIdentityMatcher
from astrbot_plugin_chat_dynamics.core.recipient_resolver import RecipientResolver
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG


@pytest.mark.parametrize("name,text,vocative,subject", [
    ("助手", "这个助手席怎么拆？", False, False),
    ("bot", "both options work", False, False),
    ("卡", "显卡怎么选", False, False),
    ("卡", "这个游戏好卡", False, False),
    ("卡", "卡你怎么看？", True, False),
    ("卡", "卡，帮我看看", True, False),
    ("群间", "@小明 群间你怎么看？", True, False),
    ("群间", "群间刚才说的方案不太对", False, True),
    ("群间", "群间，你刚刚为什么这么说？", True, True),
    ("群间", "群间刚才的方案不对，群间，你再看看？", True, True),
])
def test_independent_identity_evidence(name, text, vocative, subject):
    result = BotIdentityMatcher.match(text, (name,))
    assert result.vocative == vocative
    assert result.subject == subject


def test_recipient_inference_is_read_only_and_immutable():
    dag = ConversationDAG()
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=dag)
    node = dag.add_message("m", "sender", "群间你怎么看？", timestamp=1,
                           mentioned_users=["human"])
    before = deepcopy(dag.nodes)
    result = RecipientResolver().infer(node=node, dag=dag, runtime=runtime,
                                      topic_id="", bot_names=("群间",))
    assert result.recipient_ids == ("human", "bot")
    assert result.explicit and result.bot_targeted
    assert dag.nodes == before
    assert runtime.pending_hover is None
    assert runtime.last_interlocutor == ""
    with pytest.raises(FrozenInstanceError):
        result.confidence = 0


def test_strip_removes_only_address_not_subject_occurrences():
    assert BotIdentityMatcher.strip_vocative("助手，你说助手席是什么？", ("助手",)) == "，你说助手席是什么？"


@pytest.mark.parametrize("text, expected", [
    # One opening occurrence only, so an inner mention stays measurable content.
    ("小助手还没说完", "还没说完"),
    # Indentation is preserved; only one name occurrence is removed.
    ("  小助手，你看看", "  ，你看看"),
    ("请小助手看看", "请小助手看看"),
])
def test_strip_leading_name_drops_one_opening_occurrence(text, expected):
    assert BotIdentityMatcher.strip_leading_name(text, ("小助手",)) == expected


def test_strip_leading_name_keeps_ascii_token_boundary():
    assert BotIdentityMatcher.strip_leading_name("both options", ("bot",)) == "both options"
    assert BotIdentityMatcher.strip_leading_name("bot please check", ("bot",)) == " please check"
