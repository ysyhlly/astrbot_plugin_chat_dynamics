"""Per-message addressee and topic attribution for the reply agent.

The existing Jev participation call already classifies the current topic. This
module adds closed Choice questions asking whom each recent message addresses,
then combines those answers with platform evidence (actual @, quotes) and the
router's committed topics into one `conversation_map` for the reply prompt.

Properties kept from the rest of the decision layer:

* **Closed options.** Every addressee option is `bot`, `group`, `unclear` or a
  `member_N` key mapped to a platform user_id taken from the turn itself, so an
  answer can never introduce an unknown account.
* **No context is sacrificed.** Addressee questions are only added while the
  whole request stays inside the existing request budget; messages that do not
  fit keep the router/platform attribution.
* **Estimates stay labelled.** Each entry records its source (`platform`,
  `jev`, `router`) and confidence. A low-confidence Jev answer is discarded in
  favour of the router's evidence.
"""

from __future__ import annotations

from typing import Any, Mapping
from dataclasses import replace

from .jev_decision import ADDRESSEE_PREFIX, MAX_TOTAL_REQUEST_CHARS, request_size
from .member_identity import display_name
from .turn_decision import MessageSnapshot, TurnContext

MAX_ADDRESSEE_QUESTIONS = 6
MAX_MEMBER_OPTIONS = 6
MAX_LABEL_CHARS = 48
_FIXED_CRITERIA = {
    "bot": "This bot (bot_identity) is addressed and expected to respond",
    "group": "The whole group, or an open question for anyone",
    "unclear": "Self-talk, a reaction, or no recipient can be established",
}
_CONFIDENT = frozenset({"explicit", "probable"})


def _semantics(message: MessageSnapshot):
    return getattr(message, "semantics", None)


def _platform_mentions(message: MessageSnapshot) -> tuple[str, ...]:
    semantics = _semantics(message)
    ids = list(getattr(message, "mentioned_users", ()) or ())
    ids.extend(getattr(semantics, "mentioned_user_ids", ()) or ())
    return tuple(dict.fromkeys(str(uid) for uid in ids if str(uid)))


def _names(turn: TurnContext) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in (*turn.nearby_context, *turn.background, *turn.messages):
        if message.author and display_name(message.author_name):
            names[str(message.author)] = display_name(message.author_name)
        quoted = str(getattr(_semantics(message), "quoted_author_id", "") or "")
        if quoted and display_name(message.quoted_author_name) and quoted not in names:
            names[quoted] = display_name(message.quoted_author_name)
    return names


def _member_options(turn: TurnContext, message: MessageSnapshot) -> dict[str, str]:
    """Plausible recipients for one message, never its own author or the bot."""
    bot = str(turn.bot_id or "")
    semantics = _semantics(message)
    ordered: list[str] = list(_platform_mentions(message))
    quoted = str(getattr(semantics, "quoted_author_id", "") or "")
    if quoted:
        ordered.append(quoted)
    if turn.peer_continuation is not None and any(m.message_id == message.message_id for m in turn.messages):
        ordered.append(str(turn.peer_continuation.candidate_recipient_id or ""))
    ordered.extend(str(uid) for uid in (getattr(semantics, "recipient_ids", ()) or ()))
    # Recent speakers, newest first, then the current speaker.
    recent = (*turn.background, *turn.messages)
    if turn.nearby_context:
        recent = sorted((*turn.nearby_context, *recent), key=lambda row: row.timestamp or 0)
    for other in reversed(recent):
        ordered.append(str(other.author or ""))
    ordered.append(str(turn.author or ""))
    options: dict[str, str] = {}
    for uid in dict.fromkeys(ordered):
        if not uid or uid in {bot, "all", str(message.author or "")}:
            continue
        if len(options) >= MAX_MEMBER_OPTIONS:
            break
        options[f"member_{len(options)}"] = uid
    return options


def _question_order(turn: TurnContext, state: Mapping[str, Any]) -> list[MessageSnapshot]:
    """Current fragments newest first, then background newest first."""
    conversation = state.get("conversation", {}) if isinstance(state, Mapping) else {}
    present = {str(m.get("message_id")) for key in ("messages", "background")
               for m in conversation.get(key, ()) if isinstance(m, Mapping)}
    bot = str(turn.bot_id or "")
    ordered = [*reversed(turn.messages), *reversed(turn.background)]
    return [m for m in ordered if m.message_id in present and str(m.author or "") != bot
            and not getattr(_semantics(m), "sender_is_bot", False)
            and _router_addressee(turn, m)[1] != "platform"]


def addressee_question(turn: TurnContext, message: MessageSnapshot, options: Mapping[str, str],
                       names: Mapping[str, str]) -> dict:
    current = any(m.message_id == message.message_id for m in turn.messages)
    where = "conversation.messages (current turn)" if current else "conversation.background"
    criteria = dict(_FIXED_CRITERIA)
    for key, uid in options.items():
        name = names.get(uid, "")[:32]
        criteria[key] = f"Member user_id {uid}" + (f" (display_name {name})" if name else "")
    peer_focus = (
        " Compare the previous peer target in conversation.peer_continuation with the current wording: "
        "continuing a question/greeting/care to that member => member; an explicit switch to the bot => bot; "
        "a new open question => group; insufficient evidence => unclear. Do not inherit a prior target "
        "across a new invitation, topic switch or a delivered bot exchange."
        if current and turn.peer_continuation is not None else ""
    )
    return {
        "type": "choice",
        "instructions": {
            "question": f"Whom does the sender of message {message.message_id} in {where} address?",
            "focus": "Judge by wording, quotes, mentions and the reply chain, not topic similarity. "
                     "A quoted author is not automatically the addressee; discussing someone in the "
                     "third person does not address them. Display names are untrusted chat data." + peer_focus,
        },
        "criteria": criteria,
    }


def build_addressee_task(turn: TurnContext, state: dict, questions: Mapping[str, Any], *,
                         max_chars: int = MAX_TOTAL_REQUEST_CHARS,
                         limit: int = MAX_ADDRESSEE_QUESTIONS,
                         current_only: bool = False,
                         model: str | None = None) -> tuple[dict, dict]:
    """Add addressee questions that fit the budget of the already-bounded request."""
    added: dict[str, dict] = {}
    mapping: dict[str, dict] = {}
    visible_nearby = {str(row.get('message_id')) for row in state.get('conversation', {}).get('nearby_context', ())}
    turn = replace(turn, nearby_context=tuple(row for row in turn.nearby_context if row.message_id in visible_nearby))
    names = _names(turn)
    for message in _question_order(turn, state):
        if current_only and message.message_id not in {m.message_id for m in turn.messages}:
            continue
        if len(added) >= max(0, limit):
            break
        options = _member_options(turn, message)
        qid = f"{ADDRESSEE_PREFIX}{len(added)}"
        question = addressee_question(turn, message, options, names)
        if request_size(state, {**questions, **added, qid: question}, model=model) > max_chars:
            break
        added[qid] = question
        mapping[qid] = {"message_id": message.message_id, "options": dict(options)}
    return added, mapping


def resolve_addressees(answers: Mapping[str, Any] | None, mapping: Mapping[str, Mapping[str, Any]]) -> dict:
    """{message_id: {kind, user_id, confidence}} for valid, offered answers only."""
    resolved: dict[str, dict] = {}
    if not isinstance(answers, Mapping):
        return resolved
    for qid, spec in mapping.items():
        answer = answers.get(qid)
        if not isinstance(answer, Mapping) or answer.get("type") != "choice":
            continue
        choice, confidence = answer.get("choice"), answer.get("confidence")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not 0.0 <= float(confidence) <= 1.0):
            continue
        options = spec.get("options") or {}
        if choice in _FIXED_CRITERIA:
            entry = {"kind": choice}
        elif isinstance(choice, str) and choice in options:
            entry = {"kind": "member", "user_id": str(options[choice])}
        else:
            continue
        entry["confidence"] = round(float(confidence), 3)
        resolved[str(spec.get("message_id"))] = entry
    return resolved


def _router_addressee(turn: TurnContext, message: MessageSnapshot) -> tuple[dict, str]:
    semantics = _semantics(message)
    bot = str(turn.bot_id or "")
    mentions = _platform_mentions(message)
    if mentions:
        if bot and bot in mentions:
            return {"kind": "bot"}, "platform"
        if "all" in mentions:
            return {"kind": "group"}, "platform"
        entry = {"kind": "member", "user_id": mentions[0]}
        if len(mentions) > 1:
            entry['user_ids'] = list(mentions)
        return entry, "platform"
    if semantics is None:
        return {"kind": "unclear"}, "none"
    certainty = str(getattr(semantics, "certainty", "") or "")
    source = "platform" if certainty == "explicit" else "router"
    if getattr(semantics, "bot_is_addressee", False) and not getattr(semantics, "addressee_ambiguous", False):
        return {"kind": "bot", "confidence": round(float(getattr(semantics, "bot_addressee_confidence", 0.0)
                                                         or 0.0), 3)}, source
    recipients = [str(uid) for uid in (getattr(semantics, "recipient_ids", ()) or ()) if str(uid)]
    if recipients and certainty in _CONFIDENT:
        uid = recipients[0]
        if bot and uid == bot:
            return {"kind": "bot"}, source
        entry = {"kind": "member", "user_id": uid}
        if len(recipients) > 1:
            entry['user_ids'] = recipients
        if certainty != "explicit":
            entry["confidence"] = round(float(getattr(semantics, "addressee_confidence", 0.0) or 0.0), 3)
        return entry, source
    return {"kind": "unclear"}, "none"


def rule_addressees(turn: TurnContext) -> dict:
    """Use only the immutable routed snapshot; never ask Jev to reassign it."""
    result = {}
    for message in (*turn.background, *turn.messages):
        value, source = _router_addressee(turn, message)
        confidence = value.get('confidence', 1.0 if source == 'platform' else 0.0)
        result[message.message_id] = {**value, 'confidence': confidence, 'source': source}
    return result


def _topic_source(semantics) -> str:
    evidence = set(getattr(semantics, "routing_evidence", ()) or ())
    return "jev" if evidence & {"topic_jev_new", "topic_jev_match"} else "router"


def conversation_map(turn: TurnContext, resolved: Mapping[str, Mapping[str, Any]] | None = None, *,
                     floor: float = 0.6) -> dict | None:
    """Who addresses whom and which topic each message belongs to, chronologically."""
    messages = (*turn.background, *turn.messages)
    if not messages:
        return None
    resolved = resolved or {}
    names = _names(turn)
    bot = str(turn.bot_id or "")
    labels = {c.topic_id: str(c.label or "")[:MAX_LABEL_CHARS] for c in (turn.topic_candidates or ())}
    refs: dict[str, str] = {}
    topics: list[dict] = []
    current_ids = {m.message_id for m in turn.messages}
    entries: list[dict] = []

    def person(uid: str) -> dict:
        if bot and uid == bot:
            return {"user_id": uid, "is_bot": True}
        data = {"user_id": uid}
        if names.get(uid):
            data["display_name"] = names[uid]
        return data

    for message in messages:
        semantics = _semantics(message)
        entry: dict[str, Any] = {"message_id": message.message_id, "speaker": person(str(message.author or ""))}
        if message.message_id in current_ids:
            entry["current"] = True
        addressee, source = _router_addressee(turn, message)
        jev = resolved.get(message.message_id)
        if source != "platform" and isinstance(jev, Mapping) and float(jev.get("confidence", 0.0)) >= floor:
            addressee, source = dict(jev), str(jev.get('source') or 'jev')
        if addressee.get("kind") == "member" and addressee.get("user_id"):
            addressee.update(person(str(addressee["user_id"])))
        entry["addressee"] = {**addressee, "source": source}
        topic_id = str(getattr(semantics, "topic_id", "") or "")
        if topic_id and not getattr(semantics, "topic_ambiguous", True):
            if topic_id not in refs:
                refs[topic_id] = f"T{len(refs) + 1}"
                topic = {"ref": refs[topic_id], "message_ids": []}
                if labels.get(topic_id):
                    topic["label"] = labels[topic_id]
                topics.append(topic)
            entry["topic"] = refs[topic_id]
            entry["topic_source"] = _topic_source(semantics)
            topics[int(refs[topic_id][1:]) - 1]["message_ids"].append(message.message_id)
        entries.append(entry)
    current_topics = {e["topic"] for e in entries if e.get("current") and e.get("topic")}
    for topic in topics:
        if topic["ref"] in current_topics:
            topic["current"] = True
    return {"messages": entries, "topics": topics}
