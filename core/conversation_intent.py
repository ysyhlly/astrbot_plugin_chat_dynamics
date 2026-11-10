"""Bounded, tentative intent and humor estimates for the current speaker."""

from __future__ import annotations

from typing import Any, Mapping

INTENT_CRITERIA = {
    "communication_intent": {
        "task": "Requests an answer, action, explanation or practical help",
        "sharing": "Shares an experience or observation and invites a personal reaction",
        "venting": "Vents frustration; being heard matters before unsolicited solutions",
        "support": "Seeks reassurance, comfort or companionship",
        "banter": "Invites playful exchange or a joke response",
        "affection": "Expresses care or seeks a relationship acknowledgement",
        "unclear": "No reliable primary communication purpose is established",
    },
    "expression_mode": {
        "literal": "Mainly literal wording, with no supported nonliteral cue",
        "joke": "A playful joke supported by this exchange",
        "meme": "A recognizable shared/internet meme used here as a joke or shorthand",
        "callback": "Calls back to an actual earlier joke or shared moment in the supplied evidence",
        "sarcasm": "Intended meaning differs from literal praise/thanks; context supports irony",
        "hyperbole": "Casual exaggeration, not a literal factual claim",
        "mixed": "Humorous wording accompanies a real request, concern or feeling",
        "unclear": "Insufficient evidence to decide whether this is a joke, meme or literal statement",
    },
    "turn_update": {
        "adds_detail": "Adds a concrete detail to the existing exchange",
        "answers_question": "Answers a question the bot actually asked",
        "corrects_previous": "Corrects an earlier assumption, answer or condition",
        "shares_feeling": "Adds or clarifies a feeling about the situation",
        "continues_joke": "Develops the current joke rather than asking for an explanation",
        "acknowledges": "Acknowledges/reacts without a new point that needs expansion",
        "repeats": "Repeats or emphasizes a point, possibly because it was not answered",
        "changes_topic": "Introduces a different topic or request",
        "unclear": "No reliable comparison with the prior exchange is possible",
    },
}


def build_intent_task(state: Mapping[str, Any], questions: Mapping[str, Any], *, max_chars=None, compact=False) -> dict:
    """Add semantic questions only from unused request capacity.

    Args:
        state: Already bounded decision input.
        questions: Existing participation, context and addressee questions.
        max_chars: Optional test/operator bound; defaults to the normal request limit.

    Returns:
        Additional closed questions, without shrinking or mutating input context.
    """
    from .jev_decision import MAX_TOTAL_REQUEST_CHARS, request_size

    limit = MAX_TOTAL_REQUEST_CHARS if max_chars is None else max_chars
    instructions = {
        "communication_intent": {
            "question": "What response is the current speaker mainly seeking in this turn?",
            "focus": "Use the actual current message, addressee, prior bot reply and user updates. "
            "Sharing or venting is not automatically a request for a solution. "
            "An explicit request for help still takes priority even with joking wording. "
            "This is a revisable estimate, not a fact about someone's mind or permission to reply.",
        },
        "expression_mode": {
            "question": "How should the current speaker's wording be interpreted in this context?",
            "focus": "Interpret the whole exchange, target and quoted speaker, not isolated slang. "
            "For example, '你可真是个天才' may be praise or sarcasm; '绷不住了' may be a joke or real distress. "
            "Do not infer a meme solely from an emoji or catchphrase, invent its origin, or fabricate an earlier shared joke. "
            "Use mixed when a real problem is expressed humorously. Choose unclear when uncertain; "
            "joking style does not establish consent, excuse a boundary violation, or erase concrete distress.",
        },
        "turn_update": {
            "question": "What does this current message contribute beyond the preceding exchange?",
            "focus": "Compare with conversation.dialogue.last_bot_text, pending_question and user_updates. "
            "Identify what is newly added, answered or corrected; do not simply repeat the prior response. "
            "A repeated point may have been missed, not be spam. A laugh need not end the whole topic, "
            "but it also need not receive another question. Use unclear without adequate prior evidence.",
        },
    }
    # Shared safeguards remain in the reply policy. Keep each closed question
    # concise enough to coexist with current-recipient attribution.
    focus = {
        "communication_intent": "Use current speaker, recipient and dialogue. Sharing/venting need not seek advice; explicit help still matters with joking wording. Estimate only, not permission.",
        "expression_mode": "Use whole exchange and target. Praise may be sarcasm; slang may express real distress. Require supplied evidence for memes/callbacks, never invent origins. Use mixed for humor plus real concern; humor grants no consent.",
        "turn_update": "Compare last_bot_text, pending_question and user_updates. Find new detail, answer or correction; a repeated point may be unanswered. A laugh need not close the topic or demand a new question.",
    }
    added = {}
    for key, criteria in INTENT_CRITERIA.items():
        question = {"type": "choice", "instructions": {**instructions[key], "focus": focus[key]}, "criteria": dict(criteria)}
        if compact:
            from .decision_compaction import compact_question
            question = compact_question(key, question)
        if request_size(state, {**questions, **added, key: question}) <= limit:
            added[key] = question
    return added


def semantic_coverage(offered, answers, *, floor=0.7):
    """Explain missing estimates without recording any conversation content."""
    result = {}
    for key, criteria in INTENT_CRITERIA.items():
        raw = answers.get(key) if isinstance(answers, Mapping) else None
        asked = key in offered
        returned = raw is not None
        valid = (asked and isinstance(raw, Mapping) and raw.get("type") == "choice"
                 and isinstance(raw.get("choice"), str) and raw["choice"] in criteria
                 and type(raw.get("confidence")) in (int, float) and 0 <= raw["confidence"] <= 1)
        accepted = bool(valid and raw["confidence"] >= max(floor, 0.8 if key == "expression_mode" else 0.7))
        reason = ("budget" if not asked else "missing" if not returned else "invalid" if not valid
                  else "low_confidence" if not accepted else "accepted")
        result[key] = {"asked": asked, "returned": returned, "valid": bool(valid), "accepted": accepted,
                       "recognized": bool(accepted and raw["choice"] != "unclear"), "reason": reason}
    return result


def reply_interaction(turn, frame, *, floor=0.7) -> dict:
    """Validate turn-bound estimates and produce constrained reply guidance.

    Args:
        turn: Actual immutable reply turn.
        frame: Offered model answers bound to this session, speaker and trigger.
        floor: Minimum semantic confidence; humor uses at least 0.8.

    Returns:
        A small tentative assessment, or an empty object for stale/missing input.
    """
    if (
        not isinstance(frame, Mapping)
        or not turn.messages
        or frame.get("session_key") != turn.session_key
        or frame.get("speaker_id") != turn.author
        or frame.get("trigger_message_id") != turn.messages[-1].message_id
    ):
        return {}
    answers = frame.get("answers")
    if not isinstance(answers, Mapping):
        return {}
    result = {
        "basis": "tentative_model_estimate",
        "speaker_id": turn.author,
        "trigger_message_id": turn.messages[-1].message_id,
    }
    tentative = {}
    for key, criteria in INTENT_CRITERIA.items():
        answer = answers.get(key, {})
        value, confidence = "unclear", 0.0
        if isinstance(answer, Mapping) and answer.get("type") == "choice":
            raw = answer.get("confidence")
            if type(raw) in (int, float) and 0 <= raw <= 1:
                confidence = float(raw)
                threshold = max(float(floor), 0.8 if key == "expression_mode" else 0.7)
                if isinstance(answer.get("choice"), str) and answer["choice"] in criteria and confidence >= threshold:
                    value = answer["choice"]
        result[key] = {"choice": value, "confidence": confidence}
        # A low-confidence closed label remains a hypothesis for the reply
        # model, never an admission signal or a fact about another member.
        if (value == "unclear" and isinstance(answer, Mapping) and answer.get("type") == "choice"
                and isinstance(answer.get("choice"), str) and answer["choice"] in criteria
                and answer["choice"] != "unclear" and confidence >= .2):
            tentative[key] = {"choice": answer["choice"], "confidence": confidence}
    if tentative:
        result["tentative_understanding"] = tentative
        result["tentative_guidance"] = (
            "这些只是待核实的理解候选，须结合原句、对象和前后文判断；不据此推断心理、同意或参与权限。"
            "梗的细分类别不确定时可以自然回应，不编造或解释不存在的出处；真实求助和感受优先。")
    intent_focus = {
        "task": "先回应明确问题或请求，保留当前条件；幽默语气不应淹没实质答案。",
        "sharing": "对分享中的一个具体细节作个人化回应，可以补一个相关看法，不自动给整套建议。",
        "venting": "先接住具体处境和不满，不机械安慰或马上开解决方案；对方明确求助时再提供帮助。",
        "support": "具体回应对方在意的感受和处境，不推断未表达的心理，不套用安慰模板。",
        "banter": "理解笑点和对象后自然接一句，不把聊天中的梗写成百科解释。",
        "affection": "回应这次具体的关心或亲近表达，分寸依据真实关系，不自动升级亲密程度。",
    }
    update_focus = {
        "adds_detail": "优先回应这次新增的细节，不重新复述上一轮全部内容。",
        "answers_question": "接住对方已经给出的答案并往下聊，不再次询问同一个问题。",
        "corrects_previous": "采用当前更正，必要时简短承认先前误解，然后回应改变的部分。",
        "shares_feeling": "接住新增的感受，不重复上一轮同义安慰。",
        "continues_joke": "在现有笑点上轻轻接续，不重复原梗、不扩大攻击对象，也不强行认真说教。",
        "acknowledges": "若参与计划要求回应，简短具体即可，不硬加新问题或话题。",
        "repeats": "检查上一轮是否答到重点，补足遗漏或换个说法，不责怪对方重复。",
        "changes_topic": "回应新话题，不强行拉回旧任务或沿用旧话题的情绪。",
    }
    result["reply_focus"] = [
        value
        for value in (
            intent_focus.get(result["communication_intent"]["choice"]),
            update_focus.get(result["turn_update"]["choice"]),
        )
        if value
    ]
    return result
