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
  without this decision call. Quotes use the lower recipient floor; names and
  supplements need a confident bot recipient and a confident non-ignore action.
  Ambient chatter needs the normal join and confidence floors.
  An unavailable or uncertain decision never admits a quote/name wake by itself.

Instructions and criteria are written in English because that is the language the
model documents as its strongest; the state it judges stays exactly as the
conversation is, untranslated. Only the response goal handed to the reply agent is
Chinese, matching the plugin's other response plans.

Nothing here performs IO; the AstrBot native model catalog owns transport.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, Mapping

from .presence_policy import participation_policy
from .member_identity import IDENTITY_INSTRUCTIONS
from .prompt_policy import DEFAULT_DECISION_PROMPT, MAX_PROMPT_CHARS
from .turn_decision import TurnContext, TurnDecision, clip_conversation_text
from .context_retrieval import ContextEvidence, clip_text as _clip_text, is_short_peer_gesture
from .integrations.affection import AFFECTION_INSTRUCTIONS, normalize_affection
from .semantic_excerpt import excerpt
from .decision_persona import MAX_PERSONA_CHARS as MAX_PERSONA_CHARS, decision_persona_summary
from .reply_length import REPLY_LENGTH_POLICY
from .message_features import analyze_text

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
# Per-message addressee questions (core.conversation_map) use this key prefix.
ADDRESSEE_PREFIX = "addressee_"
MAX_TARGET_OPTIONS = 8
MAX_TOTAL_STATE_CHARS = 14000
MAX_TOTAL_REQUEST_CHARS = 20000
_ESSENTIAL_OBSERVATIONS = frozenset(
    {
        "completion_waited",
        "utterance_pause_seconds",
        "utterance_age_seconds",
        "queue_delay_seconds",
        "pending_input",
        "ambient_openings_used",
        "context_completion",
    }
)


class StateBudgetExceeded(ValueError):
    """Required identities, relationships and retained context cannot fit the budget."""


DEFAULT_MIN_CONFIDENCE = 0.6
WAKE_RECIPIENT_FLOOR = 0.35
# Participation floor for the calibrated join answer: a non-ignore action
# whose join probability is below this value is downgraded to ignore.
JOIN_FLOOR = 0.7

ACTION_CRITERIA = {
    "ignore": "No response: another recipient, no useful contribution, or intrusion",
    "acknowledge": "A simple greeting, reaction or receipt needs no new content. A direct expression of affection, care, playful fondness or relationship confirmation is substantive; choose reply.",
    "clarify": "Ask one necessary question: a missing fact blocks a useful answer",
    "reply": "Answer, advice, useful information or emotional support; new content is needed",
    "close": "Naturally finish an exchange or acknowledge an addressed ending; no new question or topic. Closing can be warm and does not imply rejection.",
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
    "acknowledge": "简短、具体地回应目标消息；简单确认可简短确认，表达感受或关系确认要回答其在意之处。避免无关展开。",
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
    character_state: Mapping[str, Any] | None = None,
    character_rules: str | None = None,
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
    from .nearby_context import decision_payload, POLICY
    nearby = decision_payload(turn)
    if nearby:
        state['conversation']['nearby_context'] = nearby
        state['conversation']['nearby_context_policy'] = POLICY
    if active_topics is not None:
        state["active_topics"] = dict(active_topics)
    if character_state is not None:
        state["character_state"] = dict(character_state)
    if character_rules is not None:
        state["character_rules"] = character_rules[:2000]
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


def bound_request_with_optional(
    state: dict, questions: dict, optional: dict, *, max_chars=MAX_TOTAL_REQUEST_CHARS,
) -> tuple[dict, dict]:
    """Reserve core context first; optional estimates must never cancel its call.

    Optional questions are ordered by priority. Try all of them normally, then
    remove the lowest-priority estimate until required context fits. Each attempt
    starts from the original bounded view, not a previously truncated failure.
    Required identities, quotes, boundaries and core questions are never removed.
    """
    items = list(optional.items())
    for count in range(len(items), -1, -1):
        retained = dict(items[:count])
        try:
            bounded = bound_request_state(deepcopy(state), {**questions, **retained}, max_chars=max_chars)
        except StateBudgetExceeded:
            if count == 0:
                raise
            continue
        # A secondary label must not force a further cut of the actual request.
        # Re-pack from the original view with fewer labels before clipping it.
        if count and bounded['conversation'].get('text') != state['conversation'].get('text'):
            continue
        if (count and state['conversation'].get('nearby_context')
                and not bounded['conversation'].get('nearby_context')):
            # A secondary label is less useful than the actual preceding
            # exchange. Retry with fewer labels before losing the entire scene.
            continue
        return bounded, retained


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
    nearby = conversation.get('nearby_context')
    if isinstance(nearby, list):
        # Optional scene context must never displace the current request or a
        # strong quote. Remove the oldest observations before any other cut.
        while nearby and _size(state) > max_chars:
            nearby.pop(0)
        if not nearby:
            conversation.pop('nearby_context', None)
            conversation.pop('nearby_context_policy', None)
        if _size(state) <= max_chars:
            return state
    background = list(conversation.get("background") or ())
    conversation["background"] = background
    for message in background:
        message["text"] = _clip_text(str(message.get("text") or ""), 240)
        _compact_semantics(message)
    _compact_fragments(conversation)
    if _size(state) <= max_chars:
        return state

    # Optional social preferences must not crowd out the current request or its evidence.
    character_state = state.get("character_state")
    if isinstance(character_state, dict):
        # Keep one attributed relationship and one recalled source; prune weak
        # extra material instead of dropping every relationship in one step.
        rows = character_state.get("context_items", [])
        social = [r for r in rows if r.get('subject_id') and r.get('provider') == 'self_learning']
        memory = [r for r in rows if r.get('provider') == 'livingmemory']
        character_state["context_items"] = [dict(r, text=excerpt(r.get('text', ''),140))
                                             for r in (social[:1] + memory[:1])]
        character_state["summary"] = ""
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
    # Persona selection already has its own 1200-character bound. Running the
    # generic omission-based cutter again can erase the whole selected role.
    # Keep that view; trim conversation prose below, or fail the budget closed.
    state["persona"] = decision_persona_summary(str(state.get("persona") or ""))
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


def build_questions(
    turn: TurnContext,
    *,
    limit: int = MAX_TARGET_OPTIONS,
    narrative: bool = False,
    context_completion: bool = False,
    context_completed: bool = False,
) -> dict[str, dict]:
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
                    "Judge conversation.text in context. No punctuation or a trailing comma alone does not mean "
                    "unfinished. Wait only for unfinished thought, promised input or explicit wait. Use observation "
                    "timing, not queue delay; never wait for other speakers or politeness."
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
                "focus": "Resolve speaker/recipient/task; follow participation_policy and decision_prompt. Quotes/topics "
                         "do not establish recipient. Useful public contributions and directly addressed kindness with "
                         "scoped closeness can invite reply; never hijack, repeat or override stop/privacy/consent. "
                         "Continuation is not a new opening.",
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
                "focus": "Resolve current recipient/task from quotes, dialogue, confirmed topics and group_events; "
                         "current corrections win. Do not answer background requests. Clarify blocking references "
                         "only if asked; unclear recipient favors ignore. Addressed kindness/affection merits substance, "
                         "especially with scoped closeness. Distinguish attacks, quoted defense and feedback; "
                         "past refusal/affection alone does not settle consent.",
            },
            "criteria": ACTION_CRITERIA,
        },
        "state": {
            "type": "choice",
            "instructions": {
                "question": "Which interaction state is this participant in after this turn?",
                "focus": "Use this turn's purpose and criteria, not habitual mood. Disengaging means actually ending. "
                         "Closeness can warm an addressed reply, never redirect recipients. Unknown/stale energy "
                         "does not mean tiredness; past refusal does not settle current willingness.",
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
                    "For this message, priority is boundary_or_sensitive, other_recipient, addressed_request/"
                    "addressed_question (task/information), social_signal, ongoing_thread, open_group_topic, "
                    "low_value_chatter. A name or quote alone is not a request."
                ),
            },
            "criteria": REASON_CRITERIA,
        },
    }
    if turn.soft_wake:
        questions.pop("join")
    if turn.soft_wake or (turn.dialogue is not None and turn.dialogue.continuation_kind):
        questions["recipient"] = {
            "type": "choice",
            "instructions": {
                "question": "Who is the current message actually asking to respond?",
                "focus": "Mentions/explicit wording outweigh topics, affection and history; quotes alone do not address "
                         "their author. 安安你怎么看 calls the bot; 安安昨天说过 discusses it; 晚安安/平平安安 prove no call. "
                         "Check actual recipient even in continuation. Group greetings need not address the bot; "
                         "group_events newcomers need not be the speaker. Use unclear without evidence.",
            },
            "criteria": {
                "bot": "The current speaker is addressing this bot and expects its response",
                "other": "The current speaker is addressing someone else or merely discussing the bot",
                "unclear": "There is not enough evidence to tell who should respond",
            },
        }
    if turn.dialogue is not None and turn.dialogue.continuation_kind:
        questions["continuity"] = {
            "type": "choice",
            "instructions": {
                "question": "How does this message relate to the delivered exchange in conversation.dialogue?",
                "focus": "Compare current text with dialogue.last_bot_text, pending_question, user_updates and next_step. "
                         "Substantive answers/corrections/follow-ups merit engagement without @; interleaved chatter "
                         "does not end the exchange. Verify recipient/topic despite continuation_kind. Bare laughs/acks "
                         "need no reply and need not end it. New substance can reopen a close, never bypass a stop. "
                         "check_current_answer and answered user_updates mean evaluate the answer, not re-ask.",
            },
            "criteria": {
                "answer": "The intended member answers a question the bot actually asked; respond to the answer",
                "continue": "The member adds, corrects, asks or explains something in the ongoing exchange",
                "reopen": "New substantive content resumes the prior exchange after a conversational closing",
                "end": "Explicit ending, or a bare reaction with nothing new requiring a response",
                "unrelated": "A new unrelated topic, another recipient or third-person discussion",
                "unclear": "The link or addressee cannot be established from the evidence",
            },
        }
    if narrative:
        questions["impact"] = {"type": "choice", "instructions": {
            "question": "Does this actual new message warrant revising the character's continuing situation before answering?",
            "focus": "Classify an explicit activity interruption, changed promise, or expressed relationship boundary. "
                     "Ordinary chatter has no continuing impact. This is routing, not permission to assert facts or invent feelings."},
            "criteria": {"none": "No important ongoing change", "activity": "Explicit interruption or activity change",
                         "commitment": "Explicit promise, cancellation or changed condition",
                         "relationship": "Explicit discomfort, personal boundary or substantive relationship event"}}
    if context_completion:
        criteria = {
            "ready": "Enough context to decide the appropriate reply or silence; no lookup needed",
            "clarify": "A necessary fact is still missing or only the user can provide it; ask one focused question if addressed",
        }
        if not context_completed:
            criteria.update(
                {
                    "recent": "Missing this speaker's recent exchange in this session",
                    "topic": "Missing earlier statements or references from this same confirmed topic",
                    "memory": "Missing an older shared experience or stated preference that scoped long-term memory may contain",
                }
            )
        questions["context_need"] = {
            "type": "choice",
            "instructions": {
                "question": "Is a concrete missing piece of context preventing an accurate response?",
                "focus": "Default ready; no reason to speak is not missing context. Find a real gap in this message/"
                         "dialogue/evidence, never retrieve for others or bypass boundaries. After context_completion."
                         "attempted: use evidence or clarify one fact, no more lookup or invention. Empty/failed "
                         "lookup does not disprove an event.",
            },
            "criteria": criteria,
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
    if narrative:
        from .decision_compaction import compact_question
        return {key: compact_question(key, value) for key, value in questions.items()}
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


def primary_target(turn: TurnContext, answers: Mapping[str, Any], *, floor: float = DEFAULT_MIN_CONFIDENCE):
    """One authoritative current fragment for admission, generation and receipts."""
    if not turn.messages:
        return None
    current = {message.message_id: message for message in turn.messages}
    target = _choice(answers, "target", tuple(current))
    return current[target[0]] if target is not None and target[1] >= floor else turn.messages[-1]


def _targets(turn: TurnContext, answers: Mapping[str, Any], *, needed: bool,
             floor: float = DEFAULT_MIN_CONFIDENCE) -> tuple[str, ...]:
    focus = primary_target(turn, answers, floor=floor)
    ids = tuple(str(m.message_id) for m in turn.messages or ())
    return tuple(dict.fromkeys((focus.message_id, *ids))) if focus is not None else ids


def _goal(action: str, reason: str) -> str:
    return (_ACTION_GOALS.get(action, _ACTION_GOALS["ignore"]) + _REASON_GOALS.get(reason, ""))[:600]


def _unit_interval(value: Any) -> float | None:
    if type(value) not in (int, float) or value < 0 or value > 1:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


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


def _aimed_only_at_others(turn: TurnContext) -> bool:
    """True when the turn @s or quotes someone else and nothing targets the bot.

    Uses only platform At ids (``mentioned_users``) and the quoted sender id;
    text names are ignored here.
    """
    bot = str(getattr(turn, "bot_id", "") or "")
    if not bot:
        return False
    aimed_elsewhere = False
    for message in getattr(turn, "messages", ()) or ():
        mentions = {str(item) for item in (getattr(message, "mentioned_users", ()) or ()) if str(item)}
        quoted = str(getattr(getattr(message, "semantics", None), "quoted_author_id", "") or "")
        if bot in mentions or quoted == bot:
            return False
        if mentions - {bot, "all"} or (quoted and quoted != bot):
            aimed_elsewhere = True
    return aimed_elsewhere


def _low_confidence_ack_is_supported(
    turn: TurnContext,
    answers: Mapping[str, Any],
    action: tuple[str, float],
    *,
    floor: float,
    join_floor: float,
    allow_soft_wake: bool = False,
) -> bool:
    """Conservatively preserve a clear invitation when action labels are split.

    This never admits ignore/close/clarify, an incomplete turn, a declined
    participation vote, a known recipient/boundary veto, or an invalid action
    probability distribution. It only selects acknowledge when the model's
    own probabilities favor responding labels over all nonresponse labels.
    """
    if action[0] not in {"acknowledge", "reply"} or action[1] >= floor:
        return False
    if (turn.mandatory_reply or (turn.soft_wake and not allow_soft_wake)
            or (turn.explicit and turn.wake_kind == "legacy")):
        return False
    if _aimed_only_at_others(turn):
        return False
    completion = _choice(answers, "completion", ("complete", "wait"))
    if completion is None or completion[0] != "complete" or completion[1] < floor:
        return False
    if turn.soft_wake:
        # The caller has already checked the configured bot-recipient floor.
        # An additional reliable addressed purpose is required without a join vote.
        purpose = _choice(answers, "reason", REASONS)
        if purpose is None or purpose[1] < floor or purpose[0] not in {
            "addressed_request", "addressed_question", "ongoing_thread", "social_signal",
        }:
            return False
    else:
        join = answers.get("join")
        if not isinstance(join, Mapping) or join.get("type") != "noul":
            return False
        probability = _unit_interval(join.get("noul"))
        if probability is None or probability < join_floor:
            return False

    answer = answers.get("action")
    probabilities = answer.get("probabilities") if isinstance(answer, Mapping) else None
    if not isinstance(probabilities, Mapping) or set(probabilities) != set(ACTIONS):
        return False
    values = {}
    for key in ACTIONS:
        value = _unit_interval(probabilities.get(key))
        if value is None:
            return False
        values[key] = value
    if abs(sum(values.values()) - 1.0) > 0.02:
        return False
    responding = values["acknowledge"] + values["reply"]
    nonresponding = values["ignore"] + values["clarify"] + values["close"]
    if responding <= nonresponding:
        return False

    # A typed veto wins even if another choice is uncertain. The native prompt
    # and JSON vocabulary remain unchanged; no text parsing or score thresholds
    # are introduced here.
    reason = answers.get("reason")
    if isinstance(reason, Mapping) and reason.get("type") == "choice" and reason.get("choice") in {
        "other_recipient", "boundary_or_sensitive",
    }:
        return False
    if (isinstance(reason, Mapping) and reason.get("type") == "choice"
            and reason.get("choice") == "low_value_chatter"):
        confidence = _unit_interval(reason.get("confidence"))
        if confidence is not None and confidence >= floor:
            return False
    state = _choice(answers, "state", STATES)
    if state is not None and state[0] == "disengaging" and state[1] >= floor:
        return False
    # `impact=relationship` also covers benign relationship questions and is
    # not itself a denial. Explicit boundaries are represented by the reason
    # choice above and stay protected.
    return True


def recipient_veto(
    turn: TurnContext,
    answers: Mapping[str, Any],
    addressees: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    floor: float = DEFAULT_MIN_CONFIDENCE,
) -> str:
    """Reconcile current-message recipient evidence before admitting speech.

    A platform quote is reference material, not a hard recipient veto. Actual
    third-party mentions, reliable model vetoes and the selected current
    message's member attribution cannot be bypassed by a positive action/join.
    Background estimates never veto a current invitation; real @bot keeps its
    separate mandatory path.
    """
    if turn.mandatory_reply or not turn.messages:
        return ""
    recipient = _choice(answers, "recipient", ("bot", "other", "unclear"))
    if recipient is not None and recipient[0] == "other" and recipient[1] >= floor:
        return "wake_other" if turn.soft_wake else "recipient_other"
    reason = _choice(answers, "reason", REASONS)
    if reason is not None and reason[0] == "other_recipient" and reason[1] >= floor:
        return "other_recipient"

    focus = primary_target(turn, answers, floor=floor)
    bot = str(turn.bot_id or "")
    mentions = {str(uid) for uid in focus.mentioned_users if uid}
    # A direct name call can explicitly ask the bot about a mentioned member.
    # The semantic recipient checks still apply to that path.
    if (bot and mentions - {bot, "all"} and bot not in mentions and "all" not in mentions
            and turn.wake_kind != "name"):
        return "platform_other_recipient"
    estimate = (addressees or {}).get(focus.message_id)
    if isinstance(estimate, Mapping) and estimate.get("kind") == "member":
        confidence = _unit_interval(estimate.get("confidence"))
        uid = str(estimate.get("user_id") or "")
        if uid and uid not in {bot, str(focus.author)} and confidence is not None and confidence >= max(floor, .7):
            return "addressee_other"
    return ""


def decision_from_answers(
    turn: TurnContext,
    answers: Mapping[str, Any],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    join_floor: float = JOIN_FLOOR,
    prefix: str = REASON_PREFIX,
    narrative: bool = False,
    quote_recipient_floor: float = WAKE_RECIPIENT_FLOOR,
    name_recipient_floor: float = 0.7,
    addressees: Mapping[str, Mapping[str, Any]] | None = None,
) -> TurnDecision:
    """Map validated System One answers onto the plugin's own decision contract.

    Invalid answers or an uncertain action return the local conservative plan.
    A valid, uncertain state/length/reason uses neutral presentation defaults;
    only the action confidence and join probability admit an ambient response.

    `prefix` identifies the source of the diagnostic reason in a decision trace.
    """
    if turn.mandatory_reply and not narrative:
        return TurnDecision.fallback(turn, "at_mandatory")
    current_target = any(
        message.reply_to or message.mentioned_users
        or (message.semantics and message.semantics.quoted_author_id)
        for message in turn.messages
    )
    if (turn.peer_continuation is not None and turn.peer_continuation.strong
            and is_short_peer_gesture(turn.text)
            and not turn.mandatory_reply and turn.wake_kind not in {"at", "quote", "name", "poke"}
            and not current_target):
        return TurnDecision.abstain("peer_continuation")
    if turn.mandatory_reply:
        action = _choice(answers, "action", ACTIONS)
        state = _choice(answers, "state", STATES)
        length = _choice(answers, "length", LENGTHS)
        reason = _choice(answers, "reason", REASONS)
        chosen = action[0] if action and action[0] != "ignore" and action[1] >= min_confidence else "acknowledge"
        chosen_state, chosen_length, chosen_reason = _reply_style(chosen, state, length, reason, min_confidence)
        return TurnDecision(chosen, chosen_state, _targets(turn, answers, needed=True, floor=min_confidence),
                            _goal(chosen, chosen_reason), chosen_length, prefix + "at_required")
    veto = recipient_veto(turn, answers, addressees, floor=min_confidence)
    if veto:
        return TurnDecision("ignore", "observing", _targets(turn, answers, needed=False, floor=min_confidence), "",
                            "brief", prefix + veto)
    completion = _choice(answers, "completion", ("complete", "wait"))
    if completion is not None and completion[0] == "wait" and completion[1] >= min_confidence:
        return TurnDecision("ignore", "observing", _targets(turn, answers, needed=False, floor=min_confidence), "",
                            "brief", prefix + "waiting_for_completion")
    dialogue = turn.dialogue
    continuity = _choice(answers, "continuity", ("answer", "continue", "reopen", "end", "unrelated", "unclear"))
    recipient = _choice(answers, "recipient", ("bot", "other", "unclear"))
    reason = _choice(answers, "reason", REASONS)
    boundary = reason is not None and reason[0] in {"other_recipient", "boundary_or_sensitive"}
    peer_conflict = (
        turn.peer_continuation is not None
        and turn.peer_continuation.strong
        and (dialogue is None or dialogue.continuation_kind != "quoted_reply")
    )
    features = analyze_text(turn.text)
    bare_reaction = features.is_filler or features.is_ack
    if (
        dialogue is not None
        and dialogue.user_id == turn.author
        and dialogue.continuation_kind
        and dialogue.last_bot_message_id in dialogue.source_message_ids
        and 0 < dialogue.age_seconds <= (900 if dialogue.interaction_state == "focused" else 600)
        and continuity is not None
        and continuity[0] in {"answer", "continue", "reopen"}
        and continuity[1] >= max(min_confidence, 0.7)
        and (not dialogue.closed or continuity[0] == "reopen")
        and recipient is not None
        and recipient[0] == "bot"
        and recipient[1] >= max(min_confidence, 0.7)
        and completion is not None
        and completion[0] == "complete"
        and completion[1] >= min_confidence
        and _choice(answers, "action", ACTIONS) is not None
        and reason is not None
        and (not bare_reaction or (continuity[0] == "answer" and dialogue.question_pending))
        and not _aimed_only_at_others(turn)
        and not boundary
        and not peer_conflict
    ):
        state = _choice(answers, "state", STATES)
        length = _choice(answers, "length", LENGTHS)
        chosen_state, chosen_length, _ = _reply_style("reply", state, length, reason, min_confidence)
        if chosen_state in {"observing", "disengaging"}:
            chosen_state = "casual"
        return TurnDecision(
            "reply",
            chosen_state,
            _targets(turn, answers, needed=True, floor=min_confidence),
            "接住这轮对上一段对话的回答、补充或纠正，先回应新增内容；问题已经答过就继续讨论，不重复询问。"
            "根据内容补充一个相关看法、细节或必要问题，自然推进，不机械追问或擅自结束话题。",
            chosen_length,
            prefix + "dialogue_" + continuity[0],
        )
    if turn.soft_wake:
        recipient = _choice(answers, "recipient", ("bot", "other", "unclear"))
        recipient_floor = (quote_recipient_floor if turn.wake_kind == "quote" else
                           name_recipient_floor if turn.wake_kind == "name" else max(min_confidence, 0.7))
        if recipient is None or recipient[1] < recipient_floor:
            return TurnDecision.fallback(turn, prefix + "wake_uncertain")
        if recipient[0] != "bot":
            return TurnDecision.fallback(turn, prefix + "wake_" + recipient[0])
        # Every soft wake needs a usable action. A quote is not permission to
        # replace an ignore or an uncertain action with a fabricated reply.
        action = _choice(answers, "action", ACTIONS)
        if action is not None and action[1] < min_confidence and _low_confidence_ack_is_supported(
                turn, answers, action, floor=min_confidence, join_floor=join_floor, allow_soft_wake=True):
            state = _choice(answers, "state", STATES)
            length = _choice(answers, "length", LENGTHS)
            reason = _choice(answers, "reason", REASONS)
            chosen_state, _, chosen_reason = _reply_style("acknowledge", state, length, reason, min_confidence)
            return TurnDecision("acknowledge", chosen_state, _targets(turn, answers, needed=True, floor=min_confidence),
                                _goal("acknowledge", chosen_reason), "brief",
                                prefix + "wake_low_confidence_acknowledged")
        if action is None or action[1] < min_confidence:
            return TurnDecision.abstain(prefix + "wake_action_uncertain")
        if action[0] == "ignore":
            return TurnDecision.abstain(prefix + "wake_action_ignore")
        state = _choice(answers, "state", STATES)
        length = _choice(answers, "length", LENGTHS)
        reason = _choice(answers, "reason", REASONS)
        chosen = action[0]
        chosen_state, chosen_length, chosen_reason = _reply_style(chosen, state, length, reason, min_confidence)
        if chosen_reason == "boundary_or_sensitive" and chosen == "reply":
            chosen = "close"
            chosen_state = "disengaging"
        return TurnDecision(
            chosen, chosen_state,
            _targets(turn, answers, needed=True, floor=min_confidence),
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
        if _low_confidence_ack_is_supported(turn, answers, action, floor=floor, join_floor=join_floor):
            chosen_state, chosen_length, chosen_reason = _reply_style(
                "acknowledge", state, length, reason, floor)
            return TurnDecision(
                "acknowledge", chosen_state, _targets(turn, answers, needed=True, floor=min_confidence),
                _goal("acknowledge", chosen_reason), chosen_length,
                prefix + "low_confidence_acknowledged",
            )
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
        _targets(turn, answers, needed=chosen != "ignore", floor=min_confidence),
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
    for key in (
        "completion",
        "join",
        "action",
        "state",
        "length",
        "reason",
        "target",
        "recipient",
        "topic",
        "impact",
        "continuity",
        "context_need",
        "communication_intent",
        "expression_mode",
        "turn_update",
        *sorted(key for key in answers if isinstance(key, str) and key.startswith(ADDRESSEE_PREFIX)),
    ):
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
    action = answers.get("action")
    probabilities = action.get("probabilities") if isinstance(action, Mapping) else None
    if (isinstance(probabilities, Mapping) and set(probabilities) == set(ACTIONS)
            and all(_unit_interval(value) is not None for value in probabilities.values())
            and abs(sum(probabilities.values()) - 1) <= .02):
        described["action_probabilities"] = {key: float(probabilities[key]) for key in ACTIONS}
    return described


def decision_confidence(answers: Mapping[str, Any]) -> float:
    """Action confidence; 0.0 when that answer is invalid or unavailable."""
    action = _choice(answers, "action", ACTIONS)
    return action[1] if action is not None else 0.0
