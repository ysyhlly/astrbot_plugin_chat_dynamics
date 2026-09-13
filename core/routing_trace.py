"""Privacy-bounded, immutable-by-copy routing decision diagnostics."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from collections.abc import Mapping
from .evidence import routing_ledger, sanitize_ledger, finite_number
from .routing_contract import addressee_is_ambiguous
from .participation_policy import EVIDENCE_CODES, EVIDENCE_FAMILIES, EVIDENCE_SOURCES


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


def redact_trace_identifiers(trace):
    """Redact identifiers in a fresh trace, including parent candidates."""
    from copy import deepcopy
    result = deepcopy(trace)
    result.get("topic", {})["topic_id"] = ""
    result.get("parent", {})["message_id"] = ""
    result.get("parent", {})["candidates"] = []
    result.get("recipient", {})["ids"] = []
    state = result.get("state", {})
    state["active_interlocutor"] = None
    state["last_bot_message_id"] = None
    if isinstance(state.get("intervening_users"), list):
        state["intervening_users"] = len(state["intervening_users"])
    result["identifiers_redacted"] = True
    return result


def build_routing_trace(*, routing, identity=None, participation=None,
                        state=None, mode="legacy", weights_version="default") -> dict:
    """Return schema 2 using a field allowlist; never copy message text/payloads."""
    route, ident, part, status = map(_mapping, (routing, identity, participation, state))
    result = {
        "routing_schema_version": 2,
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
    ledger = routing_ledger(route)
    for item in _participation_evidence(part)["evidence"]:
        ledger["entries"].append(dict(domain="participation", code=item["code"],
                                     source=item["source"], raw_value=item["raw_value"],
                                     contribution=item["strength"]))
    snapshot["ledger"] = sanitize_ledger(ledger)
    return snapshot
