"""Acknowledgment / emoji reaction policy tests."""

from __future__ import annotations

from astrbot_plugin_chat_dynamics.core.reactions import ReactionPolicy
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode


def test_laugh_trigger_gets_laugh_emoji_in_banter():
    policy = ReactionPolicy()
    decision = policy.decide(
        reply_text="确实离谱",
        mode=GroupChatMode.FAST_BANTER,
        trigger_text="哈哈这个梗绝了",
        enabled=True,
    )
    assert decision.emoji == "😂"
    assert policy.apply("确实离谱", mode=GroupChatMode.FAST_BANTER, trigger_text="哈哈这个梗绝了", enabled=True).endswith("😂")


def test_support_trigger_gets_ack_emoji():
    policy = ReactionPolicy()
    decision = policy.decide(
        reply_text="先休息一下吧",
        mode=GroupChatMode.CHILL_FADE,
        trigger_text="今天好累好难受",
        enabled=True,
    )
    assert decision.emoji == "🙏"


def test_negated_and_contrastive_triggers_do_not_get_deterministic_emoji():
    policy = ReactionPolicy()
    for trigger in ("不开心", "不烦", "开心但很累"):
        decision = policy.decide(
            reply_text="嗯",
            mode=GroupChatMode.FAST_BANTER,
            trigger_text=trigger,
            enabled=True,
        )
        assert decision.emoji == "", trigger


def test_reactions_are_skipped_for_serious_private_long_or_disabled():
    policy = ReactionPolicy()
    serious = policy.decide(reply_text="可以", mode=GroupChatMode.SERIOUS_INQUIRY, trigger_text="哈哈", enabled=True)
    private = policy.decide(
        reply_text="可以",
        mode=GroupChatMode.FAST_BANTER,
        trigger_text="别告诉别人，私聊说",
        enabled=True,
    )
    technical = policy.decide(
        reply_text="看日志",
        mode=GroupChatMode.FAST_BANTER,
        trigger_text="这个 Python 接口报错了",
        enabled=True,
    )
    long_reply = policy.decide(
        reply_text="长" * 80,
        mode=GroupChatMode.FAST_BANTER,
        trigger_text="哈哈",
        enabled=True,
    )
    disabled = policy.decide(reply_text="可以", mode=GroupChatMode.FAST_BANTER, trigger_text="哈哈", enabled=False)
    assert serious.emoji == ""
    assert private.emoji == ""
    assert technical.emoji == ""
    assert long_reply.emoji == ""
    assert disabled.emoji == ""
