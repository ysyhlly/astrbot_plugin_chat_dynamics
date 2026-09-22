"""Bounded historical stage facts, separate from final reply admission."""
from copy import deepcopy

from .persona_axes import AXIS_NAMES as PERSONA_AXIS_NAMES

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


def stage_trace(trace, *, persona, interaction_state, presence, turn, proposed, final, gate,
                axes=None):
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
