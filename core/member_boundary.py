"""Public, reload-safe integration with Autonomous Blocking.

This module deliberately discovers the companion on every operation. It keeps
only plain owner/policy snapshots, never a plugin instance or private store.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


PLUGIN_NAME = "autonomous_blocking"


class BoundaryCancelled(Exception):
    """Internal cancellation for a reply invalidated by a boundary policy."""


@dataclass(frozen=True)
class BoundaryState:
    service: Any = None
    context: Any = None
    available: bool = False
    disabled: bool = False
    required: bool = False


def _required(plugin: Any) -> bool:
    config = getattr(plugin, "_runtime_config", None)
    return bool(getattr(config, "autonomous_blocking_required", False))


def resolve_state(plugin: Any, event: Any = None, scope: Any = None) -> BoundaryState:
    """Resolve the currently activated public service and owner context."""
    required = _required(plugin)
    registry = getattr(plugin, "integrations", None)
    discover = getattr(registry, "narrative_provider", None)
    if not callable(discover):
        return BoundaryState(required=required)
    try:
        service = discover(PLUGIN_NAME)
        if service is None:
            return BoundaryState(required=required)
        if getattr(service, "enabled", True) is False:
            return BoundaryState(service=service, available=True, disabled=True, required=required)
        resolve = getattr(service, "resolve_context", None)
        if not callable(resolve):
            return BoundaryState(service=service, required=required)
        context = resolve(event=event, scope=scope)
        if not isinstance(context, dict):
            return BoundaryState(service=service, required=required)
        ready = bool(context.get("ready")) and bool((context.get("owner") or {}).get("self_id"))
        disabled = bool(context.get("disabled"))
        return BoundaryState(service, context, ready or disabled, disabled, required)
    except Exception:
        return BoundaryState(required=required)


def _fail_closed(state: BoundaryState) -> bool:
    return state.required and not state.available


def peek(plugin: Any, event: Any, uid: str, *, scope: Any = None) -> tuple[dict[str, Any] | None, dict[str, int] | None, BoundaryState]:
    state = resolve_state(plugin, event, scope)
    if not state.available or state.disabled:
        return None, None, state
    try:
        value = state.service.peek_policy(state.context, str(uid or ""))
        if not isinstance(value, dict):
            raise TypeError("policy snapshot is not a mapping")
        token = {
            "target_revision": int(value.get("target_revision", 0)),
            "shield_epoch": int(value.get("shield_epoch", 0)),
        }
        return value, token, state
    except Exception:
        return None, None, BoundaryState(service=state.service, required=state.required)


def blocked(policy: dict[str, Any] | None, *, purpose: str = "reply") -> bool:
    if not policy or not policy.get("active", True):
        return False
    mode = str(policy.get("mode") or "").lower()
    if mode == "shield":
        return True
    if purpose in {"reply", "send", "tool"} and mode == "silence":
        return True
    return False


def policy_snapshot(plugin: Any, event: Any, *, scope: Any = None) -> tuple[dict[str, Any] | None, BoundaryState]:
    state = resolve_state(plugin, event, scope)
    if not state.available or state.disabled:
        return None, state
    try:
        value = state.service.policy_snapshot(state.context)
        return (value if isinstance(value, dict) else None), state
    except Exception:
        return None, BoundaryState(service=state.service, required=state.required)


def capture_policy_snapshot(plugin: Any, event: Any) -> tuple[dict[str, Any] | None, BoundaryState]:
    """Capture plain target revisions and owner shield epoch for a draft."""
    snapshot, state = policy_snapshot(plugin, event)
    if not state.available or state.disabled or snapshot is None:
        return snapshot, state
    return {
        "owner": snapshot.get("owner"),
        "owner_revision": int(snapshot.get("owner_revision", 0)),
        "shield_epoch": int(snapshot.get("shield_epoch", 0)),
        "targets": dict(snapshot.get("targets") or {}),
    }, state


def dispatch_targets_current(plugin: Any, event: Any, targets: Any, captured: Any) -> bool:
    """Reject stale drafts after target rule changes or any owner shield change."""
    current, state = policy_snapshot(plugin, event)
    if not state.available:
        return not (state.required or state.service is not None)
    if state.disabled:
        return True
    if current is None:
        return False
    if not isinstance(captured, dict):
        return not state.required and not current.get("active")
    if captured.get("owner") != current.get("owner"):
        return False
    if int(captured.get("shield_epoch", 0)) != int(current.get("shield_epoch", 0)):
        return False
    before_targets = captured.get("targets") if isinstance(captured.get("targets"), dict) else {}
    after_targets = current.get("targets") if isinstance(current.get("targets"), dict) else {}
    for value in targets or ():
        uid = str(value or "").strip()
        if not uid:
            continue
        before = before_targets.get(uid) or {"target_revision": 0, "shield_epoch": 0}
        after = after_targets.get(uid) or {"target_revision": 0, "shield_epoch": 0}
        if any(int(before.get(field, 0)) != int(after.get(field, 0))
               for field in ("target_revision", "shield_epoch")):
            return False
        policy, _, policy_state = peek(plugin, event, uid)
        if not policy_state.available and (policy_state.required or policy_state.service is not None):
            return False
        if blocked(policy, purpose="send"):
            return False
    return True


def filter_frames(plugin: Any, event: Any, frames: Any, *, purpose: str, scope: Any = None) -> tuple[Any, BoundaryState]:
    state = resolve_state(plugin, event, scope)
    if not state.available or state.disabled:
        return frames, state
    try:
        filtered = state.service.filter_frames(state.context, frames, purpose)
        return filtered, state
    except Exception:
        return [], BoundaryState(service=state.service, required=state.required)


def filter_material(plugin: Any, event: Any, bundle: Any, *, purpose: str, scope: Any = None) -> tuple[Any, BoundaryState]:
    state = resolve_state(plugin, event, scope)
    if not state.available or state.disabled:
        return bundle, state
    try:
        filtered = state.service.filter_material(state.context, bundle, purpose)
        return filtered, state
    except Exception:
        return {}, BoundaryState(service=state.service, required=state.required)


@asynccontextmanager
async def send_lease(plugin: Any, event: Any, uid: str, token: dict[str, int] | None = None):
    """Hold the companion owner lock through the platform send await."""
    state = resolve_state(plugin, event)
    if not str(uid or "") and state.required:
        raise BoundaryCancelled("boundary_recipient_unknown")
    if not state.available:
        if _fail_closed(state):
            raise BoundaryCancelled("boundary_service_unavailable")
        yield
        return
    if state.disabled:
        yield
        return
    lease = getattr(state.service, "send_lease", None)
    if not callable(lease):
        if state.required:
            raise BoundaryCancelled("boundary_send_lease_unavailable")
        yield
        return
    try:
        async with lease(state.context, str(uid or ""), token):
            yield
    except Exception as exc:
        # Do not import a companion private exception class. Its public denial
        # is identified by contract name, then translated to CD's own type.
        if type(exc).__name__ == "PolicyDenied":
            raise BoundaryCancelled("boundary_policy_changed") from None
        raise


def fail_closed(state: BoundaryState) -> bool:
    return _fail_closed(state)
