"""Bounded historical stage facts, separate from final reply admission."""
from copy import deepcopy
from dataclasses import replace
import math

from . import outcome_recorder as outcomes
from .persona_axes import AXIS_NAMES as PERSONA_AXIS_NAMES
from .jev_decision import ACTIONS, STATES, LENGTHS, REASONS

# `decision_trace` is a bounded-facts record and holds no free text at all -- not
# conversation, not the response goal, not persona prose, not the model's own
# reasoning. `test_persona_review_boundaries` asserts exactly this by planting the
# word "private" in all three and requiring it never reach the serialized trace,
# and the learning store is documented as samples "without message text".
#
# The turn decision does carry free text now -- `rationale`, and `why_rejected` on
# each alternative -- because a decision someone cannot follow is not auditable.
# That text stays in memory for the reply stage and is *not* persisted here. What
# crosses this boundary is only what has a closed vocabulary or a fixed meaning:
# the coarse reason category, per-field confidences, message ids with their cue
# tags and weights, the rejected actions without their prose, and the four
# assessment readings.
#
# `reason_category` is checked here rather than at parse time on purpose: the
# writer is the one choosing what leaves, so the writer is the one that must
# enforce it. Prose smuggled into that field degrades to empty instead of being
# stored -- the same rule as the rest of the record.
REASON_CATEGORIES = frozenset({
    "direct_address", "reply_to_bot", "other_addressee", "no_addressee",
    "ongoing_thread", "question_open", "information_request", "social_gesture",
    "media_share", "off_topic", "private_boundary", "insufficient_context",
    "meta_control",
})

CUE_TAGS = frozenset({
    "mention", "vocative", "direct_question", "quoted_bot", "reply_chain",
    "media_drop", "topic_shift", "negation", "boundary_signal", "name_drop",
    "subject_only", "filler",
})


_MODEL_CHOICES = {
    "action": ACTIONS, "state": STATES, "length": LENGTHS, "reason": REASONS,
    "recipient": ("bot", "other", "unclear"),
    "completion": ("complete", "wait"),
    "continuity": ("answer", "continue", "reopen", "end", "unrelated", "unclear"),
}
_REASON_CATEGORY = {
    "addressed_request": "direct_address", "addressed_question": "information_request",
    "ongoing_thread": "ongoing_thread", "open_group_topic": "question_open",
    "social_signal": "social_gesture", "other_recipient": "other_addressee",
    "boundary_or_sensitive": "private_boundary", "low_value_chatter": "no_addressee",
}


def _probability(value):
    return (type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value))


def _model_choices(answers):
    clean = {}
    for key, options in _MODEL_CHOICES.items():
        raw = answers.get(key) if isinstance(answers, dict) else None
        if (isinstance(raw, dict) and raw.get("type") == "choice"
                and raw.get("choice") in options and _probability(raw.get("confidence"))):
            clean[key] = {"choice": raw["choice"], "confidence": float(raw["confidence"])}
    return clean


def _current_addressees(turn, addressees):
    """Only offered, current-message member identities can leave in a trace."""
    current = {m.message_id for m in turn.messages}
    known = {m.author for m in (*turn.messages, *turn.background)}
    for message in (*turn.messages, *turn.background):
        known.update(message.mentioned_users)
        sem = message.semantics
        known.update(getattr(sem, "recipient_ids", ()) or ())
        known.add(getattr(sem, "quoted_author_id", ""))
    if turn.peer_continuation is not None:
        known.add(turn.peer_continuation.candidate_recipient_id)
    rows = []
    for message in turn.messages[-8:]:
        raw = (addressees or {}).get(message.message_id)
        if not isinstance(raw, dict) or not _probability(raw.get("confidence")):
            continue
        kind = raw.get("kind")
        if kind not in {"bot", "member", "group", "unclear"}:
            continue
        row = {"message_id": str(message.message_id)[:160], "kind": kind,
               "confidence": float(raw["confidence"])}
        if kind == "member":
            uid = raw.get("user_id")
            if not isinstance(uid, str) or uid not in known or uid in {"", turn.bot_id, message.author}:
                continue
            row["user_id"] = str(uid)[:160]
        if message.message_id in current:
            rows.append(row)
    return rows


def annotate_model_decision(decision, turn, answers, addressees):
    """Fill existing review fields from typed outputs, without saving model prose."""
    choices = _model_choices(answers)
    confidence = tuple((key, choices[key]["confidence"])
                       for key in ("action", "state", "length") if key in choices)
    category = _REASON_CATEGORY.get(choices.get("reason", {}).get("choice"), "")
    current = _current_addressees(turn, addressees)
    focus = next(iter(decision.target_message_ids), "")
    selected = next((row for row in current if row["message_id"] == focus), None)
    kind = selected["kind"] if selected else choices.get("recipient", {}).get("choice", "unclear")
    assessment = (("addressee", kind),
                  ("completeness", choices.get("completion", {}).get("choice", "unavailable")))
    return replace(decision, confidence=confidence, reason_category=category, assessment=assessment)


def model_judgement(turn, answers, addressees, *, focus_id=None):
    choices = _model_choices(answers)
    if not choices:
        return None
    result = {"schema_version": 1, **choices, "current_addressees": _current_addressees(turn, addressees)}
    if focus_id is not None:
        result["focus_message_id"] = str(focus_id)[:160]
        for row in result["current_addressees"]:
            row["selected"] = row["message_id"] == focus_id
    probabilities = answers.get("action_probabilities", answers.get("action", {}).get("probabilities", {}))
    if (isinstance(probabilities, dict) and set(probabilities) == set(ACTIONS)
            and all(_probability(v) for v in probabilities.values())
            and abs(sum(probabilities.values()) - 1) <= .02):
        result["action_probabilities"] = {key: float(probabilities[key]) for key in ACTIONS}
    join = answers.get("join", {})
    if isinstance(join, dict) and join.get("type") == "noul" and _probability(join.get("noul")):
        result["join"] = float(join["noul"])
    result["platform"] = [{
        "message_id": str(m.message_id)[:160],
        "mentioned_user_ids": [str(uid)[:160] for uid in m.mentioned_users[:8]],
        "at_bot": bool(turn.bot_id and turn.bot_id in m.mentioned_users),
        "quoted_message_id": str(m.reply_to or "")[:160],
        "quoted_author_id": str(getattr(m.semantics, "quoted_author_id", "") or "")[:160],
    } for m in turn.messages[-8:]]
    result["omitted_current_messages"] = max(0, len(turn.messages) - 8)
    return result


def _persona_record(proposed, allowed=()) -> dict:
    """The enumerable, non-text half of a turn decision.

    Everything here has a closed vocabulary or is a number, so a record of it
    says what was decided and how firmly without carrying a syllable of anyone's
    words. Message ids are the one list that crosses, and they are filtered
    against `allowed` rather than trusted: `TurnDecision.parse` already checks
    them, but the constructor does not, and a field that is safe only on one code
    path is not a safe field.
    """
    keep = frozenset(str(a) for a in (allowed or ()) if a)
    confidence = [{"field": str(field), "value": float(value)}
                  for field, value in proposed.confidence]
    evidence = [{"message_id": str(message_id),
                 "cues": [cue for cue in cues if cue in CUE_TAGS],
                 "weight": float(weight)}
                for message_id, cues, weight in proposed.evidence]
    return {
        "action": proposed.action,
        "state": proposed.state,
        "length": proposed.length,
        "reason_code": proposed.reason_code,
        # Who the reply is aimed at -- the answer to one of the few things the
        # judge decides that the record was quietly dropping on the floor. Only
        # ids from this turn survive.
        "target_message_ids": [str(m) for m in proposed.target_message_ids if str(m) in keep],
        "reason_category": proposed.reason_category if proposed.reason_category in REASON_CATEGORIES else "",
        "confidence": confidence,
        "evidence": evidence,
        # The prose half of each alternative is dropped; which options were set
        # aside is itself a fact worth keeping.
        "alternatives": [{"action": action} for action, _why in proposed.alternatives
                         if action in {"ignore", "acknowledge", "clarify", "reply", "close"}],
        "assessment": {key: value for key, value in proposed.assessment},
    }


def record_outcome(runtime, item, marker, *args, **kwargs):
    turn = item.context
    if (item.shadow or runtime.epoch != turn.epoch
            or runtime.user_revisions.get(turn.author, 0) != turn.revision):
        return
    for message, node in zip(turn.messages, item.outcome_nodes):
        if node is not None and runtime.dag.get_node(message.message_id) is node:
            marker(node, *args, **kwargs)


# A turn that stopped being current still ended: no one else will ever write it.
_OPEN_TERMINAL_OUTCOMES = frozenset({None, "", outcomes.VALUE_IN_FLIGHT, outcomes.VALUE_NOT_ATTEMPTED})


def record_terminal_outcome(runtime, item, marker, *args, only_in_flight: bool = False, **kwargs) -> bool:
    """Write one terminal ending for a turn the normal writer now refuses.

    `record_outcome` deliberately drops a turn whose epoch or author revision
    moved on. That is right while its generation may still finish, but it also
    meant the ending of an invalidated turn was never recorded: the node stayed
    `in_flight` forever and nothing downstream ever learned how the turn went.
    This writer exists for that ending only: the node instance must still be the
    current one in the DAG, an already terminal value is never overwritten, and
    shadow turns stay untouched. `only_in_flight=True` is the final sweep that
    closes exactly the turns nothing else finished.
    """
    turn = getattr(item, "context", None)
    if turn is None or getattr(item, "shadow", False):
        return False
    written = False
    for message, node in zip(turn.messages, item.outcome_nodes):
        if node is None or runtime.dag.get_node(message.message_id) is not node:
            continue
        current = outcomes.read_outcome(node).get("final_outcome")
        if only_in_flight:
            if current != outcomes.VALUE_IN_FLIGHT:
                continue
        elif current not in _OPEN_TERMINAL_OUTCOMES:
            continue
        marker(node, *args, **kwargs)
        written = True
    return written


def stage_trace(trace, *, persona, interaction_state, presence, turn, proposed, final, gate,
                axes=None, model_answers=None, addressees=None):
    result = deepcopy(trace)
    rhythm = getattr(gate, "rhythm", None)
    constraints = {
        "evaluated": gate is not None,
        "allowed": bool(gate.should_speak) if gate is not None else None,
        "reason_code": str(getattr(gate, "reason_code", "") or
                           (final.reason_code if final != proposed else ""))[:96],
        "length_hint": final.length,
        "rhythm": {"state": str(getattr(rhythm, "state", ""))[:48],
                   "action": str(getattr(rhythm, "action", ""))[:48]},
    }
    if final.action == "ignore" and proposed.action != "ignore":
        constraints["allowed"] = False
    part = result.get("participation", {})
    record = _persona_record(proposed, getattr(turn, "allowed_ids", ()))
    # The sets a per-candidate question is asked over. Recording only what was
    # chosen says nothing about what was not, and the negatives are the hard half
    # of "who is this for" -- they cannot be reconstructed later, because by then
    # the turn is gone. Ids only, and only ids from this turn.
    candidates = sorted(str(i) for i in (getattr(turn, "allowed_ids", ()) or ()) if i)
    if candidates:
        record["candidate_message_ids"] = candidates

    # Who took part. Two sets on purpose: the window's speakers are the candidate
    # set a per-candidate question is asked over, and `participant_ids` is the
    # narrower "in *this* exchange" reading that serves as the label.
    #
    # That label is derived, not judged. `TurnDecision` has no participants field:
    # the judge works out who is who -- `semantics`, `subject_user_ids`,
    # `subject_is_bot` -- and then never reports it. So what is recorded here is
    # the routing layer's own answer (who spoke in the current fragment, who is the
    # active interlocutor, who was identified as a recipient), and a learner trained
    # on it is learning to reproduce those rules, not to reproduce the judge. When
    # the staged record section is switched on and the judge reports `participants`
    # directly, these two can be diffed -- the disagreement between the rules and
    # the judge is worth having even then.
    window = tuple(getattr(turn, "messages", ()) or ()) + tuple(getattr(turn, "background", ()) or ())
    everyone = {str(getattr(m, "author", "") or "") for m in window}
    everyone |= {str(getattr(turn, "author", "") or "")}
    everyone.discard("")
    if everyone:
        record["candidate_participant_ids"] = sorted(everyone)

    state_block = result.get("state") if isinstance(result.get("state"), dict) else {}
    recipients = result.get("recipient") if isinstance(result.get("recipient"), dict) else {}
    here = {str(getattr(m, "author", "") or "")
            for m in tuple(getattr(turn, "messages", ()) or ())}
    here |= {str(getattr(turn, "author", "") or "")}
    active = state_block.get("active_interlocutor")
    if isinstance(active, str) and active:
        here.add(active)
    here |= {str(i) for i in (recipients.get("ids") or ()) if i}
    here.discard("")
    if here:
        record["participant_ids"] = sorted(here)
    result["decision_stages"] = {
        "schema_version": 1,
        "rule": {"level": part.get("level"), "score": part.get("score")},
        "persona": record,
        "gate": constraints,
    }
    judgement = model_judgement(turn, model_answers, addressees,
                                focus_id=next(iter(proposed.target_message_ids), ""))
    if judgement is not None:
        result["decision_stages"]["judgement"] = judgement
    result["review_context"] = {
        "schema_version": 1, "decision_mode": "persona_model",
        "persona_fingerprint": str(persona.fingerprint)[:128],
        "presence_knob": str(presence)[:32],
        "interaction_state": str(interaction_state)[:48],
        "context_truncated": bool(turn.truncated),
    }
    # Which persona was judging, as six bounded integers. Presence-gated: an
    # unprojected persona leaves no field at all rather than a zeroed one, because
    # "never read" and "reads as neutral" are different facts and a learner must
    # not be taught they are the same.
    #
    # Values are clamped rather than dropped here even though the parser drops
    # them. The two jobs differ: the parser is telling the model its protocol was
    # broken, and rejecting is the message; the writer is deciding what may leave,
    # and its invariant is simply that no out-of-range number is ever emitted --
    # whatever the caller passed in.
    if axes:
        bounded = {}
        for name in PERSONA_AXIS_NAMES:
            value = axes.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                bounded[name] = max(0, min(4, int(round(float(value)))))
        if bounded:
            result["review_context"]["persona_axes"] = bounded
    return result
