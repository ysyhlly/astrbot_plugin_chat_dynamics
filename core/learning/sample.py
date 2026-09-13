"""One labelled decision with the features that produced it.

Storing only predicted versus expected tells a later learner that a decision was
wrong. Storing the feature values tells it which evidence carried the error, so
this record keeps them separate: raw input facts, and the codes the router
actually applied.

No message text and no arbitrary strings are accepted. Feature keys are drawn
from the routing and participation evidence whitelists plus a fixed set of
message facts, so a corrupt store cannot inject new identifiers.

Schema 2 adds provenance. Once weights start moving, a corpus is only
interpretable if every row says which policy produced its prediction, which
routing and feature schemas it was read under, and which plugin build wrote it.
Mixing v1 and v2 rows without that is unfixable after the fact.

A feature that was never observed is **absent from the map**, not zero. The two
are different facts and callers must not collapse them: "present" exists so a
learner can build a presence column instead of reading an unobserved
fact.is_question as False.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

from ..evidence import DIALOGUE_FACTORS, ROUTING_CODES
from ..participation_policy import EVIDENCE_CODES

SAMPLE_SCHEMA_VERSION = 2
FEATURE_SCHEMA_VERSION = 1
TASKS = ("topic", "parent", "recipient", "participation")

# Input facts, not evidence: the router did not weigh these, a later learner may.
# Every code here is reachable from a saved annotation: text shapes when the
# annotation kept the message text, and the bot-reference facts from the trace.
MESSAGE_FACTS = frozenset({
    "fact.is_short", "fact.is_elliptical", "fact.is_ack", "fact.is_question",
    "fact.is_answer_like", "fact.is_topic_boundary", "fact.is_filler",
    "fact.can_start_topic", "fact.question_ending", "fact.answer_boundary",
    "fact.reaction_like", "fact.information_density", "fact.lexical_terms",
    "fact.bot_mentioned", "fact.bot_vocative", "fact.bot_subject", "fact.has_reply",
})

FEATURE_CODES = frozenset(ROUTING_CODES | EVIDENCE_CODES | DIALOGUE_FACTORS | MESSAGE_FACTS)

MAX_IDENTIFIER = 160
MAX_FEATURES = 128
MAX_ERROR_TYPE = 64
MAX_VERSION = 64
MAX_SESSION_HASH = 64


class InvalidSample(ValueError):
    """A sample that could not be trusted enough to store."""


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER:
        raise InvalidSample(f"invalid {name}")
    return value


def _optional_text(value: object, name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise InvalidSample(f"invalid {name}")
    return value


def _optional_int(value: object, name: str) -> int:
    """A schema version: a non-negative int, absent, or refused."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
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
    session_hash: str = ""
    policy_version: str = ""
    plugin_version: str = ""
    routing_schema_version: int = 0
    feature_schema_version: int = FEATURE_SCHEMA_VERSION
    schema_version: int = field(default=SAMPLE_SCHEMA_VERSION)

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
        self._check_provenance()

    def _check_provenance(self) -> None:
        for value, name, limit in (
            (self.session_hash, "session_hash", MAX_SESSION_HASH),
            (self.policy_version, "policy_version", MAX_VERSION),
            (self.plugin_version, "plugin_version", MAX_VERSION),
        ):
            if not isinstance(value, str) or len(value) > limit:
                raise InvalidSample(f"invalid {name}")
        if self.session_hash and not all(
                character in "0123456789abcdef" for character in self.session_hash):
            raise InvalidSample("invalid session_hash")
        for value, name in ((self.routing_schema_version, "routing_schema_version"),
                            (self.feature_schema_version, "feature_schema_version"),
                            (self.schema_version, "schema_version")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InvalidSample(f"invalid {name}")

    @property
    def correct(self) -> bool:
        return self.predicted == self.expected

    @property
    def present(self) -> frozenset:
        """Codes that were actually observed.

        Absence is information: a message fact is missing because the annotation
        kept no text, which is not the same as the fact being false.
        """
        return frozenset(code for code, _ in self.features)

    @property
    def group_key(self) -> str:
        """What validation must split on; empty when the row cannot be grouped."""
        if self.session_hash:
            return self.session_hash
        return "" if self.session_id in ("", "unknown") else self.session_id

    def feature_map(self) -> dict:
        return dict(self.features)

    def to_dict(self) -> dict:
        return {"schema": SAMPLE_SCHEMA_VERSION, "session_id": self.session_id,
                "session_hash": self.session_hash, "message_id": self.message_id,
                "timestamp": self.timestamp, "task": self.task, "predicted": self.predicted,
                "expected": self.expected, "confidence": self.confidence,
                "source": self.source, "error_type": self.error_type,
                "policy_version": self.policy_version, "plugin_version": self.plugin_version,
                "routing_schema_version": self.routing_schema_version,
                "feature_schema_version": self.feature_schema_version,
                "features": [[code, value] for code, value in self.features]}

    @classmethod
    def from_dict(cls, payload: object) -> "LearningSample":
        if not isinstance(payload, dict):
            raise InvalidSample("unsupported sample schema")
        schema = payload.get("schema")
        if schema not in (1, SAMPLE_SCHEMA_VERSION):
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
        # Schema 1 rows predate provenance; they keep empty versions rather than
        # inheriting a guess, so a mixed corpus stays honest about its gaps.
        return cls(session_id=payload.get("session_id"), message_id=payload.get("message_id"),
                   timestamp=float(payload["timestamp"]), task=payload.get("task"),
                   predicted=payload.get("predicted"), expected=payload.get("expected"),
                   confidence=float(payload["confidence"]), source=payload.get("source"),
                   features=tuple(features), error_type=payload.get("error_type", ""),
                   session_hash=_optional_text(payload.get("session_hash"),
                                               "session_hash", MAX_SESSION_HASH),
                   policy_version=_optional_text(payload.get("policy_version"),
                                                 "policy_version", MAX_VERSION),
                   plugin_version=_optional_text(payload.get("plugin_version"),
                                                 "plugin_version", MAX_VERSION),
                   routing_schema_version=_optional_int(
                       payload.get("routing_schema_version"), "routing_schema_version"),
                   feature_schema_version=(_optional_int(
                       payload.get("feature_schema_version"), "feature_schema_version")
                       or FEATURE_SCHEMA_VERSION),
                   schema_version=SAMPLE_SCHEMA_VERSION)
