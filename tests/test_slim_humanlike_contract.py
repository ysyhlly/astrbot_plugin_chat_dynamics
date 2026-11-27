"""Contract & prefix-cache regression tests for exp/reply-humanlike-slim branch."""

from __future__ import annotations

import json
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.conversation_intent import compact_reply_signals
from astrbot_plugin_chat_dynamics.core.integrations.scene_learning import labels as scene_labels
from astrbot_plugin_chat_dynamics.core.narrative import REPLY_GUIDANCE
from astrbot_plugin_chat_dynamics.core.prompt_policy import DEFAULT_REPLY_PROMPT
from astrbot_plugin_chat_dynamics.core.reply_adaptation import GUIDANCE as ADAPTATION_GUIDANCE
from astrbot_plugin_chat_dynamics.core.reply_request_prompt import (
    BOUNDARY_AUTHORIZATION,
    MATERIAL_FIELD_GUIDE,
    REPLY_PROTOCOL,
    assemble_reply_prompts,
    deduplicate_reply_prompt_against_history,
)
from astrbot_plugin_chat_dynamics.core.turn_decision import (
    REPLY_INSTRUCTIONS,
    MessageSnapshot,
    TurnContext,
    TurnDecision,
    reply_prompt,
)


def _turn(text: str = "今天又打了一晚上，这破关怎么这么难", *, wake_kind: str = "none") -> TurnContext:
    return TurnContext(
        "qq:GroupMessage:100",
        "u1",
        text,
        (MessageSnapshot("m1", "u1", text, author_name="小明", platform="qq"),),
        (),
        0,
        0,
        100.0,
        False,
        wake_kind=wake_kind,
        bot_id="bot1",
    )


def test_static_plugin_layer_is_ultra_compact_pure_chinese():
    static_overhead = "\n\n".join(
        p for p in (REPLY_INSTRUCTIONS, ADAPTATION_GUIDANCE, MATERIAL_FIELD_GUIDE, REPLY_PROTOCOL, BOUNDARY_AUTHORIZATION) if p
    )
    assert len(static_overhead) < 1500
    assert len(REPLY_GUIDANCE) < 100
    assert "Respond to the target messages" not in REPLY_INSTRUCTIONS
    assert "attention_plan" not in REPLY_INSTRUCTIONS
    assert "事实与回执真实性" in REPLY_INSTRUCTIONS
    assert "发言者与对象边界" in REPLY_INSTRUCTIONS
    assert "篇幅与气泡分段" in REPLY_INSTRUCTIONS


def test_per_turn_json_strips_rubric_prose_trace_fields_and_unclear_noise():
    turn = _turn()
    decision = TurnDecision(
        action="reply",
        state="casual",
        target_message_ids=("m1",),
        response_goal="针对目标消息给出实质回复，不展开无关内容。",
        length="normal",
        reason_code="jev_open_group_topic",
        reason_category="question_open",
        confidence=(("action", 0.85), ("state", 0.76), ("length", 0.72)),
        assessment=(("addressee", "group"), ("completeness", "complete")),
    )
    # Ordinary literal/unclear turn omits turn_signals completely
    literal_frame = {
        "session_key": turn.session_key,
        "speaker_id": turn.author,
        "trigger_message_id": "m1",
        "answers": {
            "communication_intent": {"type": "choice", "choice": "unclear", "confidence": 0.4},
            "expression_mode": {"type": "choice", "choice": "literal", "confidence": 0.9},
            "speaker_stance": {"type": "choice", "choice": "unclear", "confidence": 0.3},
            "turn_update": {"type": "choice", "choice": "unclear", "confidence": 0.2},
        },
    }
    plain_data = json.loads(reply_prompt(turn, decision, interaction_frame=literal_frame, include_reply_guidance=False))
    assert "response_goal" not in plain_data["response_plan"]
    assert "confidence" not in plain_data["response_plan"]
    assert "reason_category" not in plain_data["response_plan"]
    assert "assessment" not in plain_data["response_plan"]
    assert "attention_plan" not in plain_data
    assert "interaction_understanding" not in plain_data
    assert "turn_signals" not in plain_data

    # Venting + sarcasm turn emits only high-signal compact keys
    signal_frame = {
        "session_key": turn.session_key,
        "speaker_id": turn.author,
        "trigger_message_id": "m1",
        "tone_sources": {"m0": "sig"},
        "answers": {
            "communication_intent": {"type": "choice", "choice": "venting", "confidence": 0.88},
            "expression_mode": {"type": "choice", "choice": "sarcasm", "confidence": 0.91},
            "speaker_stance": {"type": "choice", "choice": "venting", "confidence": 0.86},
            "turn_update": {"type": "choice", "choice": "unclear", "confidence": 0.3},
        },
        "evaluative_reading": {"choice": "ironic_praise", "reason": "cited_outcome", "source_message_id": "m0"},
    }
    sig_data = json.loads(reply_prompt(turn, decision, interaction_frame=signal_frame, include_reply_guidance=False))
    assert sig_data["turn_signals"] == {
        "intent": "venting",
        "expression": "sarcasm",
        "stance": "venting",
        "evaluative_reading": {"choice": "ironic_praise", "source_message_id": "m0"},
    }
    # Downstream scene_learning.labels seamlessly reads turn_signals
    assert scene_labels(sig_data["turn_signals"]) == {
        "communication_intent": "venting",
        "expression_mode": "sarcasm",
        "turn_update": "unclear",
    }
    assert compact_reply_signals(turn, literal_frame) == {}


def test_narrative_mode_hoists_config_reply_prompt_and_preserves_byte_identical_prefix():
    # Empty reply_prompt in config means no extra style block injected
    cfg_empty, warnings = parse_runtime_config({"reply_prompt": ""})
    assert not warnings and cfg_empty.reply_prompt == ""

    cfg_default, warnings = parse_runtime_config({})
    assert not warnings and cfg_default.reply_prompt == DEFAULT_REPLY_PROMPT

    managed = {"character": {"core_prompt": "完整夏目安安人设正文" * 100, "group_rules": ""}}
    tools = SimpleNamespace(tools=[])
    turn = _turn()
    decision = TurnDecision("reply", "casual", ("m1",), "目标", "normal", "jev_open_group_topic")

    # Simulate 3 heterogeneous turns in narrative mode with style slot enabled
    systems = []
    for extra_flags in (
        {},
        {"purpose": "history_quote"},
        {"poke": True},
    ):
        raw_prompt = reply_prompt(
            turn,
            decision,
            reply_guidance=cfg_default.reply_prompt,
            include_reply_guidance=True,
        )
        compacted, hoisted = deduplicate_reply_prompt_against_history(raw_prompt, "[]")
        assert "reply_guidance" not in json.loads(compacted)
        assert hoisted == DEFAULT_REPLY_PROMPT

        req = SimpleNamespace(system_prompt="host_base")
        ev = SimpleNamespace(get_extra=lambda _k, d=False: d)
        m = {**managed, **({"purpose": "history_quote"} if extra_flags.get("purpose") else {})}
        sys_prompt, _turn_note = assemble_reply_prompts(
            req,
            ev,
            native_persona="ignored_when_managed",
            managed=m,
            internal=False,
            poke_supported=True,
            boundary_state=None,
            tools=tools,
            reply_guidance=hoisted,
        )
        systems.append(sys_prompt)

    # 100% byte-identical system_prompt across heterogeneous turns -> prefix cache stays hot!
    assert len(set(systems)) == 1
    assert "[风格偏好]" in systems[0]
    assert DEFAULT_REPLY_PROMPT in systems[0]
    assert REPLY_GUIDANCE not in systems[0]

    # When reply_prompt is empty, [风格偏好] is omitted cleanly
    req_empty = SimpleNamespace(system_prompt="host_base")
    ev_empty = SimpleNamespace(get_extra=lambda _k, d=False: d)
    sys_empty, _ = assemble_reply_prompts(
        req_empty,
        ev_empty,
        native_persona="",
        managed=managed,
        internal=False,
        poke_supported=True,
        boundary_state=None,
        tools=tools,
        reply_guidance="",
    )
    assert "[风格偏好]" not in sys_empty
