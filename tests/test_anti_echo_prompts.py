"""Regression checks for anti-echo / opinionated & playful banter guidance."""

from __future__ import annotations

import json

from astrbot_plugin_chat_dynamics.core.decision_compaction import (
    QUESTION_CRITERIA,
    QUESTION_FOCUS,
)
from astrbot_plugin_chat_dynamics.core.narrative import REPLY_GUIDANCE
from astrbot_plugin_chat_dynamics.core.presence_policy import participation_policy
from astrbot_plugin_chat_dynamics.core.prompt_policy import (
    DEFAULT_DECISION_PROMPT,
    DEFAULT_REPLY_PROMPT,
    PRE_OPINION_DEFAULT_DECISION_PROMPT,
    PRE_OPINION_DEFAULT_REPLY_PROMPT,
    resolve_prompt_default,
)
from astrbot_plugin_chat_dynamics.core.turn_decision import REPLY_INSTRUCTIONS


def test_decision_and_presence_policies_discourage_hollow_echoing():
    assert "join=false" in QUESTION_FOCUS["join"]
    assert "echo" in QUESTION_FOCUS["join"].lower()
    assert "not generic agreement" in QUESTION_CRITERIA["action"]["acknowledge"].lower()
    assert "echo" in QUESTION_CRITERIA["action"]["ignore"].lower()
    assert "playful riff" in QUESTION_CRITERIA["action"]["reply"].lower()

    lively = participation_policy("lively")["guidance"].lower()
    assert "yes-man" in lively
    assert "own take" in lively or "playful riff" in lively


def test_reply_guidance_and_instructions_encourage_banter_and_own_opinion():
    assert "应声虫" in REPLY_GUIDANCE
    assert "整活" in REPLY_GUIDANCE
    assert "见解" in REPLY_GUIDANCE
    assert "silent" in REPLY_GUIDANCE

    assert "空泛附和" in REPLY_INSTRUCTIONS
    assert "应声虫" in REPLY_INSTRUCTIONS
    assert "distinct personal take" in REPLY_INSTRUCTIONS


def test_previous_default_prompts_auto_upgrade_while_custom_preserved():
    assert resolve_prompt_default("decision_prompt", "") == DEFAULT_DECISION_PROMPT
    assert (
        resolve_prompt_default("decision_prompt", PRE_OPINION_DEFAULT_DECISION_PROMPT)
        == DEFAULT_DECISION_PROMPT
    )
    assert resolve_prompt_default("reply_prompt", "") == DEFAULT_REPLY_PROMPT
    assert (
        resolve_prompt_default("reply_prompt", PRE_OPINION_DEFAULT_REPLY_PROMPT)
        == DEFAULT_REPLY_PROMPT
    )
    assert resolve_prompt_default("reply_prompt", "我的自定义提示词") == "我的自定义提示词"

    # Keep prompt payload compact for Jev / prefix cache budgets.
    assert len(json.dumps(QUESTION_FOCUS, ensure_ascii=False)) < 3500
    assert len(json.dumps(QUESTION_CRITERIA, ensure_ascii=False)) < 4500
