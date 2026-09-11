"""Privacy-bounded, immutable-by-copy routing decision diagnostics."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from collections.abc import Mapping
from .routing_contract import addressee_is_ambiguous


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
        "state": {key: status.get(key) for key in ("pending_hover", "active_interlocutor", "intervening_users")},
        "mode": mode,
        "weights_version": weights_version,
    }
    return json.loads(json.dumps({key: {k: _safe(v) for k, v in value.items()}
                                 if isinstance(value, dict) else _safe(value)
                                 for key, value in result.items()}, allow_nan=False))
