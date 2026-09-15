"""Privacy-bounded, immutable-by-copy routing decision diagnostics.

Three schemas have existed, and the differences are the reason the learning
plugin has two readers:

    1   the raw routing mapping, before traces existed
    2   the allowlisted snapshot: recipient, topic, participation evidence, state
    3   the same, plus the candidate set with the host's own per-candidate
        evidence, the topic that was selected, and where the turn finally ended
        up (`core/outcome_recorder.py`)

The schema number travels as `trace_schema_version`. The older key name,
`routing_schema_version`, described the routing section alone while the number
described the whole trace; it is gone from what this module writes.

The trace is frozen at decision time, so the outcome does not exist yet when it
is built. It is written into the snapshot afterwards by the recorder, and
re-attached here when the annotation record rebuilds a fresh one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from copy import deepcopy
from enum import Enum
from collections.abc import Mapping
from typing import Any
from .evidence import routing_ledger, sanitize_ledger, finite_number
from .outcome_recorder import OUTCOME_KEY
from .participation_policy import EVIDENCE_CODES, EVIDENCE_FAMILIES, EVIDENCE_SOURCES
from .routing_contract import addressee_is_ambiguous

# The schema 3 section carrying the shadow policy's decision. Named here rather
# than imported from the consumer: the trace describes what happened, and it
# should not depend on the module that decided whether to read a policy.
SHADOW_KEY = "shadow"


def _mapping(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return vars(value) if hasattr(value, "__dict__") else {}


def _safe(value):
    if isinstance(value, Enum):
        return _safe(value.value)
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return value if float('-inf') < value < float('inf') else None
    return None


def _participation_evidence(part):
    """Allow known diagnostic identifiers and numeric contributions only."""
    def number(value):
        return _safe(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    evidence = []
    items = part.get("evidence", ())
    for item in items if isinstance(items, (list, tuple)) else ():
        fields = _mapping(item) if is_dataclass(item) or isinstance(item, Mapping) else {}
        code, family, source = (fields.get(key) for key in ("code", "family", "source"))
        strength = number(fields.get("strength"))
        if (isinstance(code, str) and code in EVIDENCE_CODES
                and isinstance(family, str) and family in EVIDENCE_FAMILIES
                and isinstance(source, str) and source in EVIDENCE_SOURCES and strength is not None):
            # "strength" is the contribution the policy applied; "raw_value" is the
            # fact behind it. A caller that predates the split has only one number,
            # so it is used for both rather than recording a zero.
            raw = number(fields.get("raw_value"))
            evidence.append(dict(code=code, family=family, strength=strength,
                                 raw_value=strength if raw is None else raw, source=source))
    families = part.get("family_contributions", {})
    if isinstance(families, (tuple, list)):
        families = {item[0]: item[1] for item in families if isinstance(item, (tuple, list))
                    and len(item) == 2 and isinstance(item[0], str)}
    families = families if isinstance(families, Mapping) else {}
    return {"evidence": evidence,
            "family_contributions": {key: number(value) for key, value in families.items()
                                     if key in EVIDENCE_FAMILIES and number(value) is not None},
            "contribution_total": number(part.get("contribution_total"))}


def _candidates(value):
    result = []
    for item in value[:3] if isinstance(value, (list, tuple)) else ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        score, identifier = item
        if finite_number(score) is not None and isinstance(identifier, str):
            result.append([score, identifier[:160]])
    return result


def _topic_candidates(route, evidence_by_topic):
    """The candidate set in the schema 3 shape: ranked, scored, with evidence.

    Two older shapes arrive here — `[[score, id], ...]` pairs and
    `{topic_id, final_score, rank}` rows — and both become one structured list.
    The per-candidate evidence comes from `route["topic_candidate_evidence"]`,
    which the resolver fills in for **every** candidate it scored, not only the
    winner: "which scoring term put the wrong one first" cannot be asked of a
    snapshot that only kept the winner breakdown.

    A candidate whose score was never recorded keeps `final_score` out rather
    than writing 0.0. A reader that saw a zero would treat a missing number as a
    decisive one.
    """
    evidence_by_topic = evidence_by_topic if isinstance(evidence_by_topic, Mapping) else {}
    entries = []
    raw = route.get("topic_candidates")
    if not isinstance(raw, (list, tuple)):
        raw = route.get("candidates")
    for index, item in enumerate(raw if isinstance(raw, (list, tuple)) else ()):
        if index >= 8:
            break
        if isinstance(item, Mapping):
            topic_id = item.get("topic_id")
            score = item.get("final_score")
            if score is None:
                score = item.get("score")
            rank = item.get("rank")
            rank = rank if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else None
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            score, topic_id = item[0], item[1]
            rank = None
        else:
            continue
        if not isinstance(topic_id, str) or not topic_id:
            continue
        row = {"topic_id": topic_id[:160]}
        if finite_number(score) is not None:
            row["final_score"] = float(score)
        if rank is not None:
            row["rank"] = rank
        evidence = evidence_by_topic.get(topic_id)
        if isinstance(evidence, Mapping) and evidence:
            row["evidence"] = {str(key)[:64]: float(value)
                               for key, value in list(evidence.items())[:32]
                               if finite_number(value) is not None}
        entries.append(row)
    entries.sort(key=lambda row: (row.get("rank") is None, row.get("rank") or 0,
                                  -(row.get("final_score") or 0.0)))
    for position, row in enumerate(entries, 1):
        row.setdefault("rank", position)
    return entries


def _outcome_block(outcome):
    """The recorder's block, narrowed to the four fields a reader consumes."""
    block = _mapping(outcome) if outcome is not None else {}
    if not block:
        return None
    value = block.get("final_outcome")
    if not isinstance(value, str) or not value:
        return None
    delivered = block.get("delivered")
    return {
        "final_outcome": value[:32],
        "delivered": delivered if isinstance(delivered, bool) else None,
        "suppression_reason": str(block.get("suppression_reason") or "")[:96],
        "stage": str(block.get("stage") or "")[:32],
    }


def _shadow_block(shadow):
    """The shadow policy's decision, narrowed to what a reader consumes.

    Recorded only while the policy is being *observed*: it is the other half of
    a comparison whose first half is what the runtime actually did, and a
    comparison against itself would only add rows to the disagreement subset
    that are not disagreements.
    """
    block = _mapping(shadow) if shadow is not None else {}
    if not block or not block.get("policy_id"):
        return None
    outcome = {
        "policy_id": str(block.get("policy_id"))[:64],
        "baseline_threshold": finite_number(block.get("baseline_threshold")),
        "shadow_threshold": finite_number(block.get("shadow_threshold")),
        "baseline_reply": bool(block.get("baseline_reply")),
        "shadow_reply": bool(block.get("shadow_reply")),
        "changed": bool(block.get("changed")),
        "reason": str(block.get("reason") or "")[:32],
    }
    for name in ("score", "baseline_margin", "shadow_margin"):
        value = finite_number(block.get(name))
        if value is not None:
            outcome[name] = value
    return outcome


def redact_trace_identifiers(trace):
    """Redact identifiers in a fresh trace, including parent candidates."""
    from copy import deepcopy
    result = deepcopy(trace)
    result.get("topic", {})["topic_id"] = ""
    result.get("parent", {})["message_id"] = ""
    result.get("parent", {})["candidates"] = []
    result.get("recipient", {})["ids"] = []
    # Schema 3 carries the same identifiers a second time inside the candidate
    # set: the selected topic and every offered topic id. Redacting only the
    # `topic` section would leave them in the "redacted" copy.
    routing = result.get("routing")
    if isinstance(routing, dict):
        routing["selected_topic"] = ""
        routing["topic_candidates"] = []
    turn = result.get("turn")
    if isinstance(turn, dict):
        turn["session_id"] = ""
        turn["message_id"] = ""
        turn["turn_id"] = ""
    state = result.get("state", {})
    state["active_interlocutor"] = None
    state["last_bot_message_id"] = None
    if isinstance(state.get("intervening_users"), list):
        state["intervening_users"] = len(state["intervening_users"])
    result["identifiers_redacted"] = True
    return result


def build_routing_trace(*, routing, identity=None, participation=None,
                        state=None, mode="legacy", weights_version="default",
                        outcome=None, shadow=None) -> dict:
    """Return schema 3 using a field allowlist; never copy message text/payloads.

    `outcome` is optional because the trace is normally built *before* the turn
    reaches the gate. When the annotation record rebuilds a trace it passes the
    outcome recorded on the node, so the frozen snapshot ends up describing the
    whole turn rather than stopping at the admission decision.
    """
    route, ident, part, status = map(_mapping, (routing, identity, participation, state))
    result = {
        "trace_schema_version": 3,
        "trace_version": 1,
        "calibrated": False,
        "parent": {"message_id": route.get("parent_message_id", ""),
                   "confidence": finite_number(route.get("parent_confidence", 0.0)),
                   "margin": finite_number(route.get("parent_margin", 0.0)),
                   "threshold": finite_number(route.get("parent_threshold")),
                   "margin_threshold": finite_number(route.get("parent_margin_threshold")),
                   "candidate_score": finite_number(route.get("parent_candidate_score")),
                   "ambiguous": bool(route.get("parent_ambiguous", not route.get("parent_message_id"))),
                   "candidates": _candidates(route.get("parent_candidates"))},
        "topic": {"topic_id": route.get("topic_id", ""),
                  "confidence": finite_number(route.get("topic_confidence", 0.0)),
                  "threshold": finite_number(route.get("topic_threshold")),
                  "margin_threshold": finite_number(route.get("topic_margin_threshold")),
                  "ambiguous": route.get("topic_ambiguous", False)},
        "recipient": {"ids": route.get("addressee_ids", []),
                      "bot_targeted": route.get("bot_is_addressee", False),
                      "confidence": finite_number(route.get("addressee_confidence", 0.0)),
                      "threshold": finite_number(route.get("recipient_threshold")),
                      "ambiguous": addressee_is_ambiguous(route, True)},
        "identity": {"bot_reference": ident.get("bot_reference", "none"),
                     "mention": ident.get("mention", False),
                     "vocative": ident.get("vocative", False),
                     "subject": ident.get("subject", False)},
        "participation": {"score": finite_number(part.get("score")),
                          "level": part.get("level") if part.get("level") in ("strong", "hover", "weak") else None,
                          "should_reply": part.get("should_reply") if isinstance(part.get("should_reply"), bool) else None},
        "state": {key: status.get(key) for key in ("pending_hover", "active_interlocutor", "intervening_users", "waiting_for_answer", "last_bot_was_question", "last_bot_message_id")},
        "mode": mode,
        "weights_version": weights_version,
    }
    snapshot = json.loads(json.dumps({key: {k: _safe(v) for k, v in value.items()}
                                 if isinstance(value, dict) else _safe(value)
                                 for key, value in result.items()}, allow_nan=False))
    if any(key in part for key in ("evidence", "family_contributions", "contribution_total")):
        snapshot["participation"].update(_participation_evidence(part))
    # Schema 3 sections, added after the JSON round trip: `_safe` narrows a
    # mapping to None, so a nested structure has to be built already-clean.
    snapshot["routing"] = {
        "selected_topic": str(route.get("topic_id") or "")[:160],
        "topic_candidates": _topic_candidates(route, route.get("topic_candidate_evidence")),
    }
    block = _outcome_block(outcome)
    if block is not None:
        snapshot[OUTCOME_KEY] = block
    shadow_block = _shadow_block(shadow)
    if shadow_block is not None:
        snapshot[SHADOW_KEY] = shadow_block
    ledger = routing_ledger(route)
    for item in _participation_evidence(part)["evidence"]:
        ledger["entries"].append(dict(domain="participation", code=item["code"],
                                     source=item["source"], raw_value=item["raw_value"],
                                     contribution=item["strength"]))
    snapshot["ledger"] = sanitize_ledger(ledger)
    return snapshot


def compact_trace_inputs(trace: Any) -> dict:
    """Persist the complete historical decision, detached from mutable node state.

    The name is retained for compatibility. The trace itself is already
    allowlisted and bounded; the global persistence budget controls retention.
    """
    # The complete allowlisted decision is required: rebuilding from live routing
    # changes history after a reroute. Global persistence budgets bound retention.
    return deepcopy(dict(trace)) if isinstance(trace, Mapping) else {}


def finalize_decision_trace(trace, *, should_reply: bool, branch: str = "") -> dict:
    """Finalize admission once, preserving the actual short-circuit branch."""
    result = deepcopy(dict(trace))
    if result.get("decision_finalized"):
        return result
    result.setdefault("participation", {})["should_reply"] = bool(should_reply)
    result["decision_branch"] = str(branch)[:96]
    result["decision_finalized"] = True
    return result


def trace_with_updates(trace, *, outcome=None, shadow=None) -> dict:
    """Attach later observations without rebuilding the historical decision."""
    result = deepcopy(dict(trace))
    block = _outcome_block(outcome)
    if block is not None:
        result[OUTCOME_KEY] = block
    block = _shadow_block(shadow)
    if block is not None:
        result[SHADOW_KEY] = block
    return result
