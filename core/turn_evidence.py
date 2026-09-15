"""Immutable envelope for existing participation inputs; no second policy engine."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from collections.abc import Mapping

from .participation_policy import ParticipationPolicy, ParticipationSnapshot


def _detach(value):
    if is_dataclass(value):
        return type(value)(**{f.name: _detach(getattr(value, f.name)) for f in fields(value)})
    if isinstance(value, (list, tuple)):
        return tuple(_detach(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_detach(item) for item in value)
    if isinstance(value, Mapping):
        raise TypeError("Policy snapshot mappings must use immutable typed fields")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError("Unsupported mutable policy input")


@dataclass(frozen=True)
class TurnEvidence:
    session_id: str
    message_id: str
    epoch: int
    config_id: str
    policy_id: str
    visible_before: float
    participation: ParticipationSnapshot
    policy: ParticipationPolicy
    routing_json: str
    turn_id: str = ''
    revision: int = 0
    owner_revision: int = 0

    def __post_init__(self):
        object.__setattr__(self, "participation", _detach(self.participation))
        object.__setattr__(self, "policy", _detach(self.policy))
        if not isinstance(self.routing_json, str):
            raise TypeError("routing_json must be an immutable JSON string")

    @classmethod
    def capture(cls, *, session_id, message_id, epoch, config_id, policy_id,
                visible_before, participation, policy, routing, turn_id='', revision=0, owner_revision=0):
        return cls(str(session_id), str(message_id), int(epoch), str(config_id),
                   str(policy_id), float(visible_before), _detach(participation),
                   _detach(policy), json.dumps(routing, ensure_ascii=False,
                       sort_keys=True, allow_nan=False, separators=(",", ":")),
                   str(turn_id), int(revision), int(owner_revision))

    def evaluate(self, *, explicit_only=False):
        """Repeat the existing pure policy using the captured configuration."""
        return (self.policy.explicit(self.participation) if explicit_only
                else self.policy.evaluate(self.participation))

    def routing(self):
        return json.loads(self.routing_json)

    def is_current(self, *, session_id, message_id, epoch, config_id, policy_id,
                   turn_id=None, revision=None, owner_revision=None):
        return ((self.session_id, self.message_id, self.epoch, self.config_id, self.policy_id) == (
            str(session_id), str(message_id), int(epoch), str(config_id), str(policy_id))
            and (turn_id is None or self.turn_id == str(turn_id))
            and (revision is None or self.revision == int(revision))
            and (owner_revision is None or self.owner_revision == int(owner_revision)))

    def identity(self):
        return {key: getattr(self, key) for key in (
            "session_id", "message_id", "turn_id", "epoch", "revision", "owner_revision",
            "config_id", "policy_id", "visible_before")}
