"""One labelled decision with the features that produced it.

Storing only predicted versus expected tells a later learner that a decision was
wrong. Storing the feature values tells it which evidence carried the error, so
this record keeps them separate: raw input facts, and the codes the router
actually applied.

No message text and no arbitrary strings are accepted. Feature keys are drawn
from the routing and participation evidence whitelists plus a fixed set of
message facts, so a corrupt store cannot inject new identifiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ..evidence import DIALOGUE_FACTORS, ROUTING_CODES
from ..participation_policy import EVIDENCE_CODES

TASKS = ("topic", "parent", "recipient", "participation")

# Input facts, not evidence: the router did not weigh these, a later learner may.
MESSAGE_FACTS = frozenset({
    "fact.is_short", "fact.is_elliptical", "fact.is_ack", "fact.is_question",
    "fact.is_answer_like", "fact.is_topic_boundary", "fact.is_filler",
    "fact.can_start_topic", "fact.question_ending", "fact.answer_boundary",
    "fact.has_mention", "fact.has_reply", "fact.reaction_like",
    "fact.information_density", "fact.lexical_terms",
})

FEATURE_CODES = frozenset(ROUTING_CODES | EVIDENCE_CODES | DIALOGUE_FACTORS | MESSAGE_FACTS)

MAX_IDENTIFIER = 160
MAX_FEATURES = 128
MAX_ERROR_TYPE = 64


class InvalidSample(ValueError):
    """A sample that could not be trusted enough to store."""


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER:
        raise InvalidSample(f"invalid {name}")
    return value


@dataclass(frozen=True)
class LearningSample:
    """Immutable training row; ordering of features is preserved for diffs."""

    session_id: str
    message_id: str
    timestamp: float
    task: str
    predicted: str
    expected: str
    confidence: float
    source: str
    features: tuple[tuple[str, float], ...] = ()
    error_type: str = ""

    def __post_init__(self) -> None:
        _identifier(self.session_id, "session_id")
        _identifier(self.message_id, "message_id")
        _identifier(self.source, "source")
        if self.task not in TASKS:
            raise InvalidSample("invalid task")
        for value, name in ((self.predicted, "predicted"), (self.expected, "expected")):
            if not isinstance(value, str) or len(value) > MAX_IDENTIFIER:
                raise InvalidSample(f"invalid {name}")
        if len(self.error_type) > MAX_ERROR_TYPE:
            raise InvalidSample("invalid error_type")
        if len(self.features) > MAX_FEATURES:
            raise InvalidSample("too many features")
        if not isfinite(self.timestamp) or not isfinite(self.confidence):
            raise InvalidSample("non finite numeric field")
        for code, value in self.features:
            if code not in FEATURE_CODES:
                raise InvalidSample(f"unknown feature code: {code!r}")
            if not isfinite(value):
                raise InvalidSample(f"non finite feature value: {code!r}")

    @property
    def correct(self) -> bool:
        return self.predicted == self.expected

    def feature_map(self) -> dict[str, float]:
        return dict(self.features)

    def to_dict(self) -> dict:
        return {"schema": 1, "session_id": self.session_id, "message_id": self.message_id,
                "timestamp": self.timestamp, "task": self.task, "predicted": self.predicted,
                "expected": self.expected, "confidence": self.confidence,
                "source": self.source, "error_type": self.error_type,
                "features": [[code, value] for code, value in self.features]}

    @classmethod
    def from_dict(cls, payload: object) -> "LearningSample":
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise InvalidSample("unsupported sample schema")
        raw = payload.get("features", [])
        if not isinstance(raw, (list, tuple)):
            raise InvalidSample("invalid features")
        features = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise InvalidSample("invalid feature row")
            code, value = item
            if not isinstance(code, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
                raise InvalidSample("invalid feature row")
            features.append((code, float(value)))
        for key in ("timestamp", "confidence"):
            if not isinstance(payload.get(key), (int, float)) or isinstance(payload.get(key), bool):
                raise InvalidSample(f"invalid {key}")
        return cls(session_id=payload.get("session_id"), message_id=payload.get("message_id"),
                   timestamp=float(payload["timestamp"]), task=payload.get("task"),
                   predicted=payload.get("predicted"), expected=payload.get("expected"),
                   confidence=float(payload["confidence"]), source=payload.get("source"),
                   features=tuple(features), error_type=payload.get("error_type", ""))
