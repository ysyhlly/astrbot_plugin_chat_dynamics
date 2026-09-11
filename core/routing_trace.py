"""Privacy-bounded, immutable-by-copy routing decision diagnostics."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from collections.abc import Mapping
from .routing_contract import addressee_is_ambiguous
from .participation_policy import EVIDENCE_CODES, EVIDENCE_FAMILIES, EVIDENCE_SOURCES


def _mapping(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return vars(value) if value is not None else {}


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
            evidence.append(dict(code=code, family=family, strength=strength, source=source))
    families = part.get("family_contributions", {})
    if isinstance(families, (tuple, list)):
        families = {item[0]: item[1] for item in families if isinstance(item, (tuple, list))
                    and len(item) == 2 and isinstance(item[0], str)}
    families = families if isinstance(families, Mapping) else {}
    return {"evidence": evidence,
            "family_contributions": {key: number(value) for key, value in families.items()
                                     if key in EVIDENCE_FAMILIES and number(value) is not None},
            "contribution_total": number(part.get("contribution_total"))}


def build_routing_trace(*, routing, identity=None, participation=None,
                        state=None, mode="legacy", weights_version="default") -> dict:
    """Return schema 2 using a field allowlist; never copy message text/payloads."""
    route, ident, part, status = map(_mapping, (routing, identity, participation, state))
    result = {
        "routing_schema_version": 2,
        "topic": {"topic_id": route.get("topic_id", ""),
                  "confidence": route.get("topic_confidence", 0.0),
                  "ambiguous": route.get("topic_ambiguous", False)},
        "recipient": {"ids": route.get("addressee_ids", []),
                      "bot_targeted": route.get("bot_is_addressee", False),
                      "confidence": route.get("addressee_confidence", 0.0),
                      "ambiguous": addressee_is_ambiguous(route, True)},
        "identity": {"bot_reference": ident.get("bot_reference", "none"),
                     "mention": ident.get("mention", False),
                     "vocative": ident.get("vocative", False),
                     "subject": ident.get("subject", False)},
        "participation": {key: part.get(key) for key in ("score", "level", "should_reply")},
        "state": {key: status.get(key) for key in ("pending_hover", "active_interlocutor", "intervening_users", "waiting_for_answer", "last_bot_was_question", "last_bot_message_id")},
        "mode": mode,
        "weights_version": weights_version,
    }
    snapshot = json.loads(json.dumps({key: {k: _safe(v) for k, v in value.items()}
                                 if isinstance(value, dict) else _safe(value)
                                 for key, value in result.items()}, allow_nan=False))
    if any(key in part for key in ("evidence", "family_contributions", "contribution_total")):
        snapshot["participation"].update(_participation_evidence(part))
    return snapshot
