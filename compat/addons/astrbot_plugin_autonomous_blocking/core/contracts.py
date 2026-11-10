"""Public data contract for the autonomous blocking rule store.

This module intentionally has no AstrBot dependency.  The runtime adapter is
responsible for deriving owner identity and for deciding who may call admin
inspection methods.
"""

from __future__ import annotations

from typing import Any, TypedDict


SCHEMA_VERSION = 1
MODES = frozenset({"silence", "shield"})
SCOPES = frozenset({"bot", "session"})


class Owner(TypedDict):
    platform_instance_id: str
    self_id: str


class Policy(TypedDict):
    reply_allowed: bool
    read_allowed: bool
    mode: str | None
    expires_at: str | None
    rules: list[dict[str, Any]]
    owner_revision: int
    target_revision: int
    shield_epoch: int


class MutationReceipt(TypedDict):
    schema_version: int
    action_id: str
    event_id: str
    changed: bool
    owner_revision: int
    target_revision: int
    shield_epoch: int
    active_rules: list[dict[str, Any]]
    effective: Policy

