"""Jev (TypeSafe System One) as the plugin's turn decision layer.

The decision layer answers one bounded question per turn: whether to take part at
all, in what interaction state, how long the reply should be, which message it hangs
off, and why. This module builds those questions from the same immutable
`TurnContext` the model path uses, and maps the validated answers back into the very
same `TurnDecision` — so participation gates, decision traces, shadow telemetry and
delivery do not know, and do not need to know, which backend decided.

Two properties are load-bearing:

* **The vocabulary is closed.** Every question offers the plugin's own options, so a
  remote answer can never introduce an unknown action, state, length or reason.
  Answers are located by the option keys we sent, never parsed out of prose.
* **Below the confidence floor nothing is used.** A split or uncertain answer is not
  a decision: the local conservative plan runs instead, which replies to an explicit
  request and stays silent in ambient chatter.

Instructions and criteria are written in English because that is the language the
model documents as its strongest; the state it judges stays exactly as the
conversation is, untranslated. Only the response goal handed to the reply agent is
Chinese, matching the plugin's other response plans.

Nothing here performs IO; `core/integrations/typesafe.py` owns the transport.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .presence_policy import participation_policy
from .turn_decision import TurnContext, TurnDecision

ACTIONS = ("ignore", "acknowledge", "clarify", "reply", "close")
STATES = ("observing", "casual", "focused", "supportive", "playful", "disengaging")
LENGTHS = ("brief", "normal", "detailed")
# Bounded, snake_case reasons: the same shape TurnDecision.parse accepts, because
# they travel into traces and diagnostics under that contract.
REASONS = (
    "addressed_request",
    "addressed_question",
    "ongoing_thread",
    "open_group_topic",
    "social_signal",
    "other_recipient",
    "boundary_or_sensitive",
    "low_value_chatter",
)
REASON_PREFIX = "jev_"
MAX_TARGET_OPTIONS = 8
MAX_PERSONA_CHARS = 1200
MAX_TOTAL_STATE_CHARS = 12000
DEFAULT_MIN_CONFIDENCE = 0.6
JOIN_FLOOR = 0.5

ACTION_CRITERIA = {
    "ignore": "Stay out of it: nothing here needs this participant, or joining would cut into someone else's exchange",
    "acknowledge": "A short acknowledgement only: one line or a reaction, with no new content",
    "clarify": "One clarifying question: the target or the intent is not yet clear enough to answer",
    "reply": "A real reply: respond to the target message with substance",
    "close": "Wind the exchange down: a brief closing line that asks nothing new",
}
STATE_CRITERIA = {
    "observing": "Watching: stay in the background this turn and open no new topic",
    "casual": "Casual: relaxed small talk among the group",
    "focused": "Focused: the answer has to be accurate and on point",
    "supportive": "Supportive: someone's feeling has to be acknowledged first",
    "playful": "Playful: riffing and joking is welcome",
    "disengaging": "Disengaging: do not continue this exchange afterwards",
}
LENGTH_CRITERIA = {
    "brief": "Very short: one or two sentences, or a single line",
    "normal": "Normal: short but complete",
    "detailed": "Expanded: it takes points or explanation to be clear",
}
REASON_CRITERIA = {
    "addressed_request": "Someone named this participant or asked it to do something",
    "addressed_question": "Someone asked this participant a direct question",
    "ongoing_thread": "Continuing the same exchange this participant already took part in",
    "open_group_topic": "An open group discussion where this participant has something relevant and useful to add",
    "social_signal": "A poke or a reaction: a social signal, not a content request",
    "other_recipient": "The message is plainly aimed at someone else",
    "boundary_or_sensitive": "Privacy, conflict, or a request to stop: keep a distance",
    "low_value_chatter": "Chatter or fragments with nothing this participant needs to join",
}
_ACTION_GOALS = {
    "reply": "针对目标消息给出实质回复，不展开无关内容。",
    "acknowledge": "只做简短确认，不展开、不追问。",
    "clarify": "问一句最小的澄清，确认对象或意图。",
    "close": "简短收尾，不再抛出新问题。",
    "ignore": "本轮保持安静，继续旁听。",
}
_REASON_GOALS = {
    "addressed_question": "先直接回答被问到的问题。",
    "addressed_request": "按对方的请求给出可用结果。",
    "ongoing_thread": "延续与对方的同一话题，不重复已说过的内容。",
    "open_group_topic": "只补充一句与公开讨论相关的有用内容，越短越好。",
    "social_signal": "当作社交信号回应，不要当成内容请求或附件。",
}


def target_options(turn: TurnContext, *, limit: int = MAX_TARGET_OPTIONS) -> dict[str, str]:
    """Message IDs of this turn as Choice options, newest last.

    Current-turn messages carry provenance rather than text (`TurnContext.payload`
    deliberately holds the consolidated text once), so each option is described by
    position, author and its reply relationship instead of invented content.
    """
    options: dict[str, str] = {}
    messages = list(turn.messages or ())
    total = len(messages)
    for index, message in enumerate(messages):
        if len(options) >= max(1, limit):
            break
        message_id = str(getattr(message, "message_id", "") or "")
        if not message_id or message_id in options:
            continue
        author = str(getattr(message, "author", "") or "unknown")
        position = "the newest message" if index == total - 1 else f"message {index + 1} of {total}"
        reply_to = ", itself a reply to an earlier message" if getattr(message, "reply_to", "") else ""
        options[message_id] = f"{position}, sent by {author}{reply_to}"
    return options


def build_state(
    turn: TurnContext,
    *,
    previous_state: str = "observing",
    observations: Mapping[str, Any] | None = None,
    presence: str = "sensible",
    persona_prompt: str = "",
    max_chars: int = MAX_TOTAL_STATE_CHARS,
) -> dict:
    """The bounded state a System One model judges, never the raw unbounded context."""
    state = {
        "persona": str(persona_prompt or "").strip()[:MAX_PERSONA_CHARS],
        "previous_state": str(previous_state or "observing")[:32],
        "participation_policy": participation_policy(presence),
        "observations": dict(observations or {}),
        "conversation": turn.payload(),
    }
    return _shrink(state, max_chars)


def _size(state: dict) -> int:
    try:
        return len(json.dumps(state, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _shrink(state: dict, max_chars: int) -> dict:
    """Drop the least load-bearing context first until the state fits its budget."""
    if max_chars <= 0 or _size(state) <= max_chars:
        return state
    conversation = state.get("conversation")
    if not isinstance(conversation, dict):
        return state
    background = list(conversation.get("background") or ())
    while background and _size(state) > max_chars:
        background.pop(0)
        conversation["background"] = background
    if _size(state) > max_chars:
        for key in ("messages", "background"):
            for message in conversation.get(key) or ():
                if isinstance(message, dict) and len(str(message.get("text") or "")) > 240:
                    message["text"] = str(message["text"])[:240]
    if _size(state) > max_chars:
        conversation["text"] = str(conversation.get("text") or "")[:4000]
        conversation["background"] = list(conversation.get("background") or ())[-4:]
    if _size(state) > max_chars:
        state["observations"] = {}
    if _size(state) > max_chars:
        state["persona"] = ""
    return state


def build_questions(turn: TurnContext, *, limit: int = MAX_TARGET_OPTIONS) -> dict[str, dict]:
    """One call, five or six independent questions about the same state.

    `join` is the calibrated yes/no (a Noul), `action`, `state`, `length` and
    `reason` are Choices over the plugin's own vocabulary, and `target` is a Choice
    over this turn's message IDs when more than one candidate exists.
    """
    questions: dict[str, dict] = {
        "join": {
            "type": "noul",
            "instructions": {
                "question": (
                    "Should this participant speak in this turn, given `persona`, "
                    "`participation_policy`, `previous_state`, `observations` and `conversation`?"
                ),
                "focus": (
                    "Judge whether speaking now is useful and appropriate, not whether the "
                    "content is interesting. Being addressed or continuing a live exchange is "
                    "usually yes; an exchange between other people, private matters, conflict, "
                    "or a request to stop is no."
                ),
            },
            "criteria": {
                "true": "Yes: speaking now is useful and interrupts nobody",
                "false": "No: speaking now adds nothing, or would interrupt someone else",
            },
        },
        "action": {
            "type": "choice",
            "instructions": {
                "question": "If this participant does take part, how should it respond?",
                "focus": "Pick the most restrained form that still fits; pick ignore when nothing should be answered.",
            },
            "criteria": ACTION_CRITERIA,
        },
        "state": {
            "type": "choice",
            "instructions": {
                "question": "Which interaction state is this participant in after this turn?",
                "focus": "The state describes the overall mood and engagement, which is a separate question from reply length.",
            },
            "criteria": STATE_CRITERIA,
        },
        "length": {
            "type": "choice",
            "instructions": {
                "question": "How long should the reply be?",
                "focus": "Group chat defaults to short; expand only when explaining or listing is required.",
            },
            "criteria": LENGTH_CRITERIA,
        },
        "reason": {
            "type": "choice",
            "instructions": {
                "question": "Which category best describes the main reason for this decision?",
                "focus": "This is a diagnostic category, not an explanation: choose the closest one.",
            },
            "criteria": REASON_CRITERIA,
        },
    }
    options = target_options(turn, limit=limit)
    if len(options) > 1:
        questions["target"] = {
            "type": "choice",
            "instructions": {
                "question": "Which message should the reply attach to?",
                "focus": (
                    "Usually the newest one; choose an earlier one only when that is the "
                    "message actually being answered."
                ),
            },
            "criteria": options,
        }
    return questions


def _choice(answers: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> tuple[str, float] | None:
    answer = answers.get(key)
    if not isinstance(answer, Mapping) or answer.get("type") != "choice":
        return None
    value = answer.get("choice")
    confidence = answer.get("confidence")
    if value not in allowed or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    score = float(confidence)
    if score != score or not 0.0 <= score <= 1.0:
        return None
    return str(value), score


def _targets(turn: TurnContext, answers: Mapping[str, Any], *, needed: bool) -> tuple[str, ...]:
    """The reply's target IDs: Jev's pick first, the rest of the turn behind it."""
    fallback = tuple(str(m.message_id) for m in turn.messages or ())
    if not needed:
        return fallback
    answer = answers.get("target")
    picked = answer.get("choice") if isinstance(answer, Mapping) else None
    # Only a current-turn message may head the reply: the target question offers
    # nothing else, so an ID from the background would answer a question never
    # asked, and the turn's own messages stand instead.
    if not isinstance(picked, str) or picked not in set(fallback):
        return fallback
    return tuple(dict.fromkeys((picked, *fallback)))


def _goal(action: str, reason: str) -> str:
    return (_ACTION_GOALS.get(action, _ACTION_GOALS["ignore"]) + _REASON_GOALS.get(reason, ""))[:600]


def decision_from_answers(
    turn: TurnContext,
    answers: Mapping[str, Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    join_floor: float = JOIN_FLOOR,
) -> TurnDecision:
    """Map validated System One answers onto the plugin's own decision contract.

    Anything that cannot be mapped exactly — a missing answer, an option outside the
    vocabulary, a confidence below the floor — returns the local conservative plan
    with a `jev_*` reason instead of an approximation of what Jev might have meant.
    """
    action = _choice(answers, "action", ACTIONS)
    state = _choice(answers, "state", STATES)
    length = _choice(answers, "length", LENGTHS)
    reason = _choice(answers, "reason", REASONS)
    if action is None or state is None or length is None or reason is None:
        return TurnDecision.fallback(turn, "jev_invalid_answer")
    floor = float(min_confidence)
    if min(action[1], state[1], length[1], reason[1]) < floor:
        return TurnDecision.fallback(turn, "jev_low_confidence")
    chosen, reason_code = action[0], REASON_PREFIX + reason[0]
    join = answers.get("join")
    if isinstance(join, Mapping) and join.get("type") == "noul":
        probability = join.get("noul")
        if isinstance(probability, (int, float)) and not isinstance(probability, bool):
            value = float(probability)
            if value != value or not 0.0 <= value <= 1.0:
                return TurnDecision.fallback(turn, "jev_invalid_answer")
            if chosen != "ignore" and value < float(join_floor):
                chosen, reason_code = "ignore", REASON_PREFIX + "join_declined"
    return TurnDecision(
        chosen,
        state[0],
        _targets(turn, answers, needed=chosen != "ignore"),
        _goal(chosen, reason[0]),
        length[0],
        reason_code,
    )


def describe_answers(answers: Mapping[str, Any]) -> dict:
    """Compact, serializable evidence for diagnostics and the console panel.

    The `confidence` entry is the weakest confidence among the required answers,
    which is what the acceptance floor is compared against; 0.0 means the answers
    were incomplete and nothing was accepted from them.
    """
    described: dict[str, Any] = {}
    for key in ("join", "action", "state", "length", "reason", "target"):
        answer = answers.get(key)
        if not isinstance(answer, Mapping):
            continue
        kind = answer.get("type")
        entry: dict[str, Any] = {"type": kind}
        if kind == "choice":
            entry["choice"] = answer.get("choice")
            entry["confidence"] = answer.get("confidence")
        elif kind == "noul":
            entry["noul"] = answer.get("noul")
        elif kind == "score":
            entry["score"] = answer.get("score")
            entry["confidence"] = answer.get("confidence")
        described[key] = entry
    described["confidence"] = decision_confidence(answers)
    return described


def decision_confidence(answers: Mapping[str, Any]) -> float:
    """The weakest confidence among the required answers; 0.0 when incomplete."""
    values = []
    for key in ("action", "state", "length", "reason"):
        answer = answers.get(key)
        confidence = answer.get("confidence") if isinstance(answer, Mapping) else None
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return 0.0
        values.append(float(confidence))
    return min(values) if values else 0.0
