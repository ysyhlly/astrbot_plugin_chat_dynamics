"""Bounded historical stage facts, separate from final reply admission."""
from copy import deepcopy

def record_outcome(runtime, item, marker, *args, **kwargs):
    turn = item.context
    if (item.shadow or runtime.epoch != turn.epoch
            or runtime.user_revisions.get(turn.author, 0) != turn.revision):
        return
    for message, node in zip(turn.messages, item.outcome_nodes):
        if node is not None and runtime.dag.get_node(message.message_id) is node:
            marker(node, *args, **kwargs)


def stage_trace(trace, *, persona, interaction_state, presence, turn, proposed, final, gate):
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
    result["decision_stages"] = {
        "schema_version": 1,
        "rule": {"level": part.get("level"), "score": part.get("score")},
        "persona": {key: getattr(proposed, key) for key in ("action", "state", "length", "reason_code")},
        "gate": constraints,
    }
    result["review_context"] = {
        "schema_version": 1, "decision_mode": "persona_model",
        "persona_fingerprint": str(persona.fingerprint)[:128],
        "presence_knob": str(presence)[:32],
        "interaction_state": str(interaction_state)[:48],
        "context_truncated": bool(turn.truncated),
    }
    return result
