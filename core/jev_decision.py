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
* **Wake types have separate admission rules.** A real platform @ always replies
  without this decision call. A quote/name wake needs a bot recipient at the lower
  recipient floor; ambient chatter needs the normal join and confidence floors.
  An unavailable or uncertain decision never admits a quote/name wake by itself.

Instructions and criteria are written in English because that is the language the
model documents as its strongest; the state it judges stays exactly as the
conversation is, untranslated. Only the response goal handed to the reply agent is
Chinese, matching the plugin's other response plans.

Nothing here performs IO; the AstrBot native model catalog owns transport.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .presence_policy import participation_policy
from .member_identity import IDENTITY_INSTRUCTIONS
from .prompt_policy import DEFAULT_DECISION_PROMPT, MAX_PROMPT_CHARS
from .turn_decision import TurnContext, TurnDecision, clip_conversation_text
from .context_retrieval import ContextEvidence, clip_text as _clip_text
from .integrations.affection import AFFECTION_INSTRUCTIONS, normalize_affection
from .decision_persona import MAX_PERSONA_CHARS as MAX_PERSONA_CHARS, decision_persona_summary
from .reply_length import REPLY_LENGTH_POLICY

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
MAX_TOTAL_STATE_CHARS = 12000
MAX_TOTAL_REQUEST_CHARS = 18000
_ESSENTIAL_OBSERVATIONS = frozenset({"completion_waited", "utterance_pause_seconds",
    "utterance_age_seconds", "queue_delay_seconds", "pending_input", "ambient_openings_used"})


class StateBudgetExceeded(ValueError):
    """Required identities, relationships and retained context cannot fit the budget."""


DEFAULT_MIN_CONFIDENCE = 0.6
WAKE_RECIPIENT_FLOOR = 0.35
# Participation floor for the calibrated join answer: a non-ignore action
# whose join probability is below this value is downgraded to ignore.
JOIN_FLOOR = 0.7

ACTION_CRITERIA = {
    "ignore": "No response: another recipient, no useful contribution, or intrusion",
    "acknowledge": "Greeting, reaction or receipt confirmation; no new content or follow-up needed",
    "clarify": "Ask one necessary question: a missing fact blocks a useful answer",
    "reply": "Answer, advice, useful information or emotional support; new content is needed",
    "close": "Explicitly end or decline the exchange; do not continue afterwards",
}
STATE_CRITERIA = {
    "observing": "No response planned for this turn",
    "casual": "Greeting or relaxed small talk; no task, distress or explicit joke",
    "focused": "Solve a task or factual question, including frustrating errors",
    "supportive": "Comfort or listen to feelings (e.g. loneliness); task solving is secondary",
    "playful": "An explicit joke or playful exchange",
    "disengaging": "Closing or refusal; end participation in this exchange",
}
LENGTH_CRITERIA = REPLY_LENGTH_POLICY.criteria
REASON_CRITERIA = {
    "addressed_request": "Direct task request to this participant; naming it alone is insufficient",
    "addressed_question": "Direct information question to this participant; use addressed_request for tasks",
    "ongoing_thread": "Continue this participant's exchange without a new direct task or information question",
    "open_group_topic": "Useful public contribution without direct address or existing exchange",
    "social_signal": "Only greeting, thanks, poke or reaction; no content request",
    "other_recipient": "Explicitly addresses someone else; discussing this participant does not address it",
    "boundary_or_sensitive": "Privacy, conflict or an explicit stop calls for restraint",
    "low_value_chatter": "No direct request, continuation, useful public contribution or social signal",
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
    for index in range(total - 1, -1, -1):
        if len(options) >= max(1, limit):
            break
        message = messages[index]
        message_id = str(getattr(message, "message_id", "") or "")
        if not message_id or message_id in options:
            continue
        author = str(getattr(message, "author", "") or "unknown")
        position = "the newest message" if index == total - 1 else f"message {index + 1} of {total}"
        reply_to = ", itself a reply to an earlier message" if getattr(message, "reply_to", "") else ""
        options[message_id] = f"{position}, sent by {author}{reply_to}"
    return dict(reversed(tuple(options.items())))


def build_state(
    turn: TurnContext,
    *,
    previous_state: str = "observing",
    observations: Mapping[str, Any] | None = None,
    presence: str = "sensible",
    persona_prompt: str = "",
    decision_prompt: str = DEFAULT_DECISION_PROMPT,
    max_chars: int = MAX_TOTAL_STATE_CHARS,
    active_topics: Mapping[str, Any] | None = None,
    affection: Mapping[str, Any] | None = None,
) -> dict:
    """The bounded state a System One model judges, never the raw unbounded context."""
    state = {
        "conversation": turn.payload(),
        "identity_policy": IDENTITY_INSTRUCTIONS,
        "participation_policy": participation_policy(presence),
        "decision_prompt": str(decision_prompt or DEFAULT_DECISION_PROMPT).strip()[:MAX_PROMPT_CHARS],
        "previous_state": str(previous_state or "observing")[:32],
        "observations": dict(observations or {}),
        "persona": decision_persona_summary(str(persona_prompt or "")),
    }
    if active_topics is not None:
        state["active_topics"] = dict(active_topics)
    relationship = normalize_affection(affection, user_id=turn.author)
    if relationship is not None:
        state["affection"] = relationship
        state["affection_policy"] = AFFECTION_INSTRUCTIONS
    return _shrink(_compact_state(state), max_chars)




def _size(state: dict) -> int:
    return len(json.dumps(state, ensure_ascii=False, allow_nan=False))


def request_size(state: dict, questions: dict, *, model: str | None = None) -> int:
    """Match native instruction serialization and reserve 256 chars for the model ID."""
    normalized = {key: {**spec, "instructions": json.dumps(spec["instructions"], ensure_ascii=False)
                        if isinstance(spec.get("instructions"), dict) else spec.get("instructions")}
                  for key, spec in questions.items()}
    return _size({"model": "m" * 256 if model is None else model, "state": state, "questions": normalized})


def bound_request_state(state: dict, questions: dict, *, max_chars=MAX_TOTAL_REQUEST_CHARS) -> dict:
    overhead = request_size({}, questions) - 2
    budget = min(MAX_TOTAL_STATE_CHARS, max_chars - overhead)
    if budget <= 0:
        raise StateBudgetExceeded("request_budget_exceeded")
    return _shrink(state, budget)


_RELATION_FIELDS = frozenset({
    "recipient_ids", "basis", "certainty", "mentioned_user_ids", "quoted_message_id", "quoted_author_id",
    "subject_user_ids", "subject_is_bot", "bot_is_addressee", "intent", "topic_id", "topic_confidence",
    "topic_ambiguous", "parent_message_id", "parent_confidence", "addressee_confidence", "addressee_ambiguous",
    "routing_evidence", "evidence_message_id", "bot_addressee_confidence", "routing_ambiguous",
    "scenes", "emotions",
})


def _compact_semantics(message: dict) -> None:
    semantics = message.get("semantics")
    if not isinstance(semantics, dict):
        return
    # False ambiguity flags and zero confidence are evidence, not empty values.
    compact = {key: value for key, value in semantics.items() if key in _RELATION_FIELDS
               and value != "unknown"}
    if compact.get("parent_message_id") and compact.get("parent_message_id") == compact.get("quoted_message_id"):
        compact.pop("parent_message_id")
    if compact.get("evidence_message_id") in {compact.get("parent_message_id"), compact.get("quoted_message_id")}:
        compact.pop("evidence_message_id", None)
    if ("topic_ambiguous" in compact and "addressee_ambiguous" in compact
            and compact.get("routing_ambiguous") == (compact["topic_ambiguous"] or compact["addressee_ambiguous"])):
        compact.pop("routing_ambiguous", None)
    message["semantics"] = compact
    if message.get("reply_to") and message.get("reply_to") == compact.get("quoted_message_id"):
        message.pop("reply_to")


def _compact_fragments(conversation: dict) -> None:
    """Preserve every current ID and quote account; share repeated identities."""
    for message in list(conversation.get("messages") or ())[:-1]:
        if message.get("author_identity") == conversation.get("speaker_identity"):
            message.pop("author_identity", None)
        quoted = message.get("quoted_author_identity")
        if isinstance(quoted, dict) and quoted.get("user_id"):
            shared = conversation.setdefault("quoted_identities", {})
            if shared.setdefault(quoted["user_id"], quoted) == quoted:
                message.pop("quoted_author_identity", None)
        _compact_semantics(message)
    messages = list(conversation.get("messages") or ())
    if len(messages) < 4:
        return
    # Share only values equal across every fragment. Keep the latest message
    # explicit for consumers that read it directly; older values inherit defaults.
    defaults = conversation.setdefault("fragment_defaults", {})
    for key in ("age_seconds", "text_excerpt"):
        if key in messages[-1] and all(m.get(key) == messages[-1][key] and key in m for m in messages):
            defaults[key] = messages[-1][key]
            for message in messages[:-1]:
                message.pop(key)
    semantics = [m.get("semantics") for m in messages]
    if all(isinstance(s, dict) for s in semantics):
        identity_fields = {"recipient_ids", "quoted_message_id", "quoted_author_id", "mentioned_user_ids",
                           "subject_user_ids", "parent_message_id"}
        common = {key: value for key, value in semantics[0].items()
                  if key not in identity_fields and all(key in s and s[key] == value for s in semantics[1:])}
        if common:
            defaults["semantics"] = {**defaults.get("semantics", {}), **common}
            for semantic in semantics[:-1]:
                for key in common:
                    semantic.pop(key)
    if not defaults:
        conversation.pop("fragment_defaults")


def _clean_fields(value):
    """Remove empty containers/text, keeping false and numeric zero unchanged."""
    if isinstance(value, dict):
        cleaned = {key: _clean_fields(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items()
                if item is not None and item != "" and item != {} and item != [] and item != ()}
    if isinstance(value, (list, tuple)):
        # Lists such as [message_id, text] and text spans have positional meaning.
        items = [_clean_fields(item) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    return value


def _compact_state(state: dict) -> dict:
    """Lossless field cleanup on every request, before applying text budgets."""
    conversation = state["conversation"]
    _compact_fragments(conversation)
    known = [conversation.get("speaker_identity"), conversation.get("bot_identity")]
    known.extend(m.get("author_identity") for m in conversation.get("messages", ()))
    background = conversation.get("background", ())
    by_account: dict[str, list[dict]] = {}
    for message in background:
        identity = message.get("author_identity")
        if isinstance(identity, dict) and identity.get("user_id") and identity not in known:
            by_account.setdefault(identity["user_id"], []).append(identity)
    shared = {uid: identities[0] for uid, identities in by_account.items()
              if len(identities) > 1 and all(i == identities[0] for i in identities)}
    if shared:
        conversation["member_identities"] = shared
    for message in background:
        _compact_semantics(message)
        identity = message.get("author_identity")
        if identity in known or (isinstance(identity, dict) and identity == shared.get(identity.get("user_id"))):
            message.pop("author_identity", None)
    for message in conversation.get("messages", ()):
        _compact_semantics(message)
    has_topics = "active_topics" in state
    state = _clean_fields(state)
    # Stable public containers; an empty background/topic list is a real result.
    state["conversation"].setdefault("messages", [])
    state["conversation"].setdefault("background", [])
    state.setdefault("observations", {})
    state.setdefault("persona", "")
    if has_topics:
        state.setdefault("active_topics", {})
    return state


def _drop_optional_topic(topics: dict) -> bool:
    for key in reversed(topics):
        if not topics[key].get("required"):
            topics.pop(key)
            return True
    return False


def _background_priority(message: dict) -> int:
    return min((ContextEvidence(str(proof.get("relation", "")), "", 0,
                                tuple(proof.get("evidence") or ())).priority
                for proof in message.get("context_evidence") or () if isinstance(proof, dict)), default=7)


def _drop_weakest(background: list) -> None:
    # Background is chronological: remove the oldest within the weakest tier.
    index = max(range(len(background)), key=lambda i: (_background_priority(background[i]), -i))
    background.pop(index)


def _shrink(state: dict, max_chars: int) -> dict:
    """Drop the least load-bearing context first until the state fits its budget."""
    if max_chars <= 0 or _size(state) <= max_chars:
        return state
    conversation = state["conversation"]
    background = list(conversation.get("background") or ())
    conversation["background"] = background
    for message in background:
        message["text"] = _clip_text(str(message.get("text") or ""), 240)
        _compact_semantics(message)
    _compact_fragments(conversation)
    if _size(state) <= max_chars:
        return state

    # Optional social preferences must not crowd out the current request or its evidence.
    state.pop("affection", None)
    state.pop("affection_policy", None)
    if _size(state) <= max_chars:
        return state

    # Reserve space for the current request and real quotes before removing them.
    # Keep operator guidance intact whenever the remaining context can fit.
    state["observations"] = {key: value for key, value in state.get("observations", {}).items()
                             if key in _ESSENTIAL_OBSERVATIONS}
    topics = dict(state.get("active_topics") or {})
    if "active_topics" in state:
        state["active_topics"] = topics
        while topics and _size(topics) > max_chars // 4:
            if not _drop_optional_topic(topics):
                break
    if _size(state) <= max_chars:
        return state
    state["persona"] = _clip_text(str(state.get("persona") or ""), min(400, max_chars // 20))
    dialogue = conversation.get("dialogue")
    if isinstance(dialogue, dict):
        dialogue["last_bot_text"] = _clip_text(str(dialogue.get("last_bot_text") or ""), 240)
        dialogue["user_updates"] = [(mid, _clip_text(text, 120))
                                    for mid, text in dialogue.get("user_updates", ())]
    if _size(state) > max_chars:
        clip_conversation_text(conversation, 4000)
    # Keep at least the strongest credible background item, including an
    # unquoted same-topic/recipient supplement. Same-author noise is expendable.
    while (background and _size(state) > max_chars and any(_background_priority(m) > 0 for m in background)
           and (len(background) > 1 or _background_priority(background[0]) >= 6)):
        _drop_weakest(background)
    # Topic descriptions are ordered by query relevance. Keep state, question
    # choices and the acceptance mapping aligned in the caller after this cut.
    while topics and _size(state) > max_chars:
        if not _drop_optional_topic(topics):
            break
    if _size(state) > max_chars:
        for message in background:
            message["text"] = _clip_text(str(message.get("text") or ""), 120)
    while len(background) > 1 and _size(state) > max_chars:
        _drop_weakest(background)

    # Preserve a meaningful current utterance and every message/account ID. If
    # those facts plus the strongest credible background cannot fit, deny the call.
    text = str(conversation.get("text") or "")
    minimum = min(len(text), 256)
    while len(text) > minimum and _size(state) > max_chars:
        overflow = _size(state) - max_chars
        clip_conversation_text(conversation, max(minimum, len(text) - overflow - 32))
        text = conversation["text"]
    guidance = str(state.get("decision_prompt") or "")
    minimum_guidance = min(len(guidance), max(1, min(1500, max_chars // 8)))
    while len(guidance) > minimum_guidance and _size(state) > max_chars:
        overflow = _size(state) - max_chars
        guidance = _clip_text(guidance, max(minimum_guidance, len(guidance) - overflow - 32))
        state["decision_prompt"] = guidance
    if _size(state) > max_chars:
        raise StateBudgetExceeded("state_budget_exceeded")
    return state


def build_questions(turn: TurnContext, *, limit: int = MAX_TARGET_OPTIONS) -> dict[str, dict]:
    """One call, with bounded questions about the same state.

    `completion` judges whether to wait for the speaker, `join` is the
    calibrated yes/no (a Noul), `action`, `state`, `length` and
    `reason` are Choices over the plugin's own vocabulary, and `target` is a Choice
    over this turn's message IDs when more than one candidate exists. Soft wakes
    ask who the actual recipient is instead of the unused join probability.
    """
    questions: dict[str, dict] = {
        "completion": {
            "type": "choice",
            "instructions": {
                "question": "Has the current speaker finished this utterance so a response can begin now?",
                "focus": (
                    "Read conversation.text and context. Complete questions, statements, greetings and "
                    "short reactions need no final punctuation. A trailing comma alone does not imply "
                    "continuation. Wait only for an unfinished thought, promised input or explicit wait "
                    "request. Use observations for actual timing; queue delay is not speaker silence. "
                    "Do not wait for more people or merely to be polite."
                ),
            },
            "criteria": {
                "complete": "The utterance is complete enough to answer or react to now",
                "wait": "The speaker has not finished; retain these fragments and wait briefly for a continuation",
            },
        },
        "join": {
            "type": "noul",
            "instructions": {
                "question": (
                    "Should this participant speak in this turn, given `persona`, "
                    "`participation_policy`, `decision_prompt`, `previous_state`, `observations` and `conversation`?"
                ),
                "focus": (
                    "Follow the current participation_policy and operator decision_prompt. "
                    "When supplied, consider affection only under affection_policy. Judge useful, "
                    "appropriate participation, not interest alone. Addressed turns and live exchanges "
                    "usually warrant speaking; dialogue supplies prior replies and pending questions, "
                    "never proof of the current recipient. Explicit other recipients, privacy, conflict "
                    "and stop requests take precedence. A quote/name wake needs an actual bot addressee, "
                    "not topic value. Respect ambient openings used; continuing an exchange is not a new opening."
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
                "question": "What single response action fits the current speaker's message?",
                "focus": (
                    "Use current text and attribution. Background is context, never the request. "
                    "Respect persona boundaries, participation_policy and operator decision_prompt. "
                    "Decide whether a response is appropriate before selecting its purpose."
                ),
            },
            "criteria": ACTION_CRITERIA,
        },
        "state": {
            "type": "choice",
            "instructions": {
                "question": "Which interaction state is this participant in after this turn?",
                "focus": (
                    "Choose the current turn's primary purpose. Background mood and persona do not override it."
                ),
            },
            "criteria": STATE_CRITERIA,
        },
        "length": {
            "type": "choice",
            "instructions": {
                "question": "How long should the reply be?",
                "focus": REPLY_LENGTH_POLICY.decision_focus,
            },
            "criteria": LENGTH_CRITERIA,
        },
        "reason": {
            "type": "choice",
            "instructions": {
                "question": "Which category best describes the main reason for this decision?",
                "focus": (
                    "Choose one category for the current message. When several apply, use this "
                    "priority: boundary_or_sensitive, other_recipient, addressed_request or "
                    "addressed_question (task versus information), social_signal, ongoing_thread, "
                    "open_group_topic, low_value_chatter. A name or quote alone is not a direct request."
                ),
            },
            "criteria": REASON_CRITERIA,
        },
    }
    if turn.soft_wake:
        questions.pop("join")
        questions["recipient"] = {
            "type": "choice",
            "instructions": {
                "question": "Who is the current message actually asking to respond?",
                "focus": (
                    "Judge the current speaker's intent using conversation.text and attributed context. "
                    "A reply quoting the bot, its nickname or a wake name is a wake candidate, "
                    "not proof of the addressee. Distinguish calling the bot from discussing it "
                    "in the third person. If the speaker is addressing the bot, choose bot even "
                    "for short chatter, a fragment or a simple greeting. A quoted author's words "
                    "are context and do not themselves address the bot."
                ),
            },
            "criteria": {
                "bot": "The current speaker is addressing this bot and expects its response",
                "other": "The current speaker is addressing someone else or merely discussing the bot",
                "unclear": "There is not enough evidence to tell who should respond",
            },
        }
    options = target_options(turn, limit=limit)
    if len(options) > 1:
        questions["target"] = {
            "type": "choice",
            "instructions": {
                "question": "Which message should the reply attach to?",
                "focus": (
                    "Use messages.text_spans or text_excerpt to identify each fragment. Usually attach "
                    "to the newest message; select an earlier one only when its content is being answered."
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


def _reply_style(
    action: str,
    state: tuple[str, float] | None,
    length: tuple[str, float] | None,
    reason: tuple[str, float] | None,
    floor: float,
) -> tuple[str, str, str]:
    """Uncertain presentation must not veto an admitted response."""
    default_state = {"ignore": "observing", "close": "disengaging"}.get(action, "focused")
    chosen_state = state[0] if state is not None and state[1] >= floor else default_state
    if action == "ignore" or chosen_state == "observing":
        chosen_state = default_state
    chosen_length = length[0] if length is not None and length[1] >= floor else "brief"
    chosen_reason = reason[0] if reason is not None and reason[1] >= floor else ""
    return chosen_state, chosen_length, chosen_reason


def decision_from_answers(
    turn: TurnContext,
    answers: Mapping[str, Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    join_floor: float = JOIN_FLOOR,
    prefix: str = REASON_PREFIX,
) -> TurnDecision:
    """Map validated System One answers onto the plugin's own decision contract.

    Invalid answers or an uncertain action return the local conservative plan.
    A valid, uncertain state/length/reason uses neutral presentation defaults;
    only the action confidence and join probability admit an ambient response.

    `prefix` identifies the source of the diagnostic reason in a decision trace.
    """
    if turn.mandatory_reply:
        return TurnDecision.fallback(turn, "at_mandatory")
    completion = _choice(answers, "completion", ("complete", "wait"))
    if completion is not None and completion[0] == "wait" and completion[1] >= min_confidence:
        return TurnDecision("ignore", "observing", _targets(turn, answers, needed=False), "",
                            "brief", prefix + "waiting_for_completion")
    if turn.soft_wake:
        recipient = _choice(answers, "recipient", ("bot", "other", "unclear"))
        if recipient is None or recipient[1] < WAKE_RECIPIENT_FLOOR:
            return TurnDecision.fallback(turn, prefix + "wake_uncertain")
        if recipient[0] != "bot":
            return TurnDecision.fallback(turn, prefix + "wake_" + recipient[0])
        # A confirmed bot addressee is sufficient for soft wake. The other
        # questions tune the reply, rather than imposing ambient participation.
        action = _choice(answers, "action", ACTIONS)
        state = _choice(answers, "state", STATES)
        length = _choice(answers, "length", LENGTHS)
        reason = _choice(answers, "reason", REASONS)
        chosen = action[0] if action is not None and action[1] >= min_confidence and action[0] != "ignore" else "reply"
        chosen_state, chosen_length, chosen_reason = _reply_style(chosen, state, length, reason, min_confidence)
        if chosen_reason == "boundary_or_sensitive" and chosen == "reply":
            chosen = "close"
            chosen_state = "disengaging"
        return TurnDecision(
            chosen, chosen_state,
            _targets(turn, answers, needed=True),
            _goal(chosen, chosen_reason),
            chosen_length, prefix + "wake_addressed",
        )
    action = _choice(answers, "action", ACTIONS)
    state = _choice(answers, "state", STATES)
    length = _choice(answers, "length", LENGTHS)
    reason = _choice(answers, "reason", REASONS)
    if action is None or state is None or length is None or reason is None:
        return TurnDecision.fallback(turn, prefix + "invalid_answer")
    floor = float(min_confidence)
    if action[1] < floor:
        return TurnDecision.fallback(turn, prefix + "low_confidence")
    chosen = action[0]
    chosen_state, chosen_length, chosen_reason = _reply_style(chosen, state, length, reason, floor)
    reason_code = prefix + (chosen_reason or "action_accepted")
    explicit_wake = bool(turn.explicit and turn.wake_kind == "legacy"
                         and chosen_reason != "boundary_or_sensitive")
    if chosen == "ignore" and explicit_wake:
        return TurnDecision.fallback(turn, prefix + "explicit_wake")
    join = answers.get("join")
    if isinstance(join, Mapping) and join.get("type") == "noul":
        probability = join.get("noul")
        if isinstance(probability, (int, float)) and not isinstance(probability, bool):
            value = float(probability)
            if value != value or not 0.0 <= value <= 1.0:
                return TurnDecision.fallback(turn, prefix + "invalid_answer")
            if chosen != "ignore" and value < float(join_floor) and not explicit_wake:
                chosen, reason_code = "ignore", prefix + "join_declined"
    return TurnDecision(
        chosen,
        "observing" if chosen == "ignore" else chosen_state,
        _targets(turn, answers, needed=chosen != "ignore"),
        _goal(chosen, chosen_reason),
        chosen_length,
        reason_code,
    )


def describe_answers(answers: Mapping[str, Any]) -> dict:
    """Compact, serializable evidence for diagnostics and the console panel.

    `confidence` reports the action's confidence, which gates ambient responses.
    Each presentation answer retains its own confidence for diagnosis.
    """
    described: dict[str, Any] = {}
    for key in ("completion", "join", "action", "state", "length", "reason", "target", "recipient", "topic"):
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
    """Action confidence; 0.0 when that answer is invalid or unavailable."""
    action = _choice(answers, "action", ACTIONS)
    return action[1] if action is not None else 0.0
