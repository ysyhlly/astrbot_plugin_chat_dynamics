"""AstrBot-independent policy runtime helpers and owner-scoped coordination."""

from __future__ import annotations

import asyncio
import copy
import contextvars
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import MODES, SCHEMA_VERSION
from .store import PolicyStore


_DURATION = re.compile(r"^(?:(\d+)([smhdw]))$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class PolicyDenied(RuntimeError):
    """A protected operation was denied by the current policy or stale token."""


def parse_duration(value: Any) -> tuple[bool, int | None]:
    if isinstance(value, str) and value.strip().lower() in {"permanent", "forever"}:
        return True, None
    match = _DURATION.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError("duration must be a positive value such as 30m, 2h, 1d, or permanent")
    count = int(match.group(1))
    seconds = count * _UNIT_SECONDS[match.group(2).lower()]
    if count <= 0 or seconds > 10 * 365 * 24 * 60 * 60:
        raise ValueError("duration must be positive and at most 10 years")
    return False, seconds


def resolve_context(event: Any = None, scope: str | None = None) -> dict[str, Any]:
    """Resolve an owner only from a concrete event; never guess missing identity."""
    if event is None:
        return {"schema_version": SCHEMA_VERSION, "owner": None, "umo": "", "ready": False}
    try:
        platform_id = str(event.get_platform_id() or "").strip()
        self_id = str(event.get_self_id() or "").strip()
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            umo = str(event.get_session_id() or "").strip()
    except Exception:
        platform_id = self_id = umo = ""
    owner = {"platform_instance_id": platform_id, "self_id": self_id} if platform_id and self_id else None
    # `scope` is a caller-provided narrative/context object in integrations, not
    # the persistence rule scope. Rule scope is validated only by mutations.
    return {"schema_version": SCHEMA_VERSION, "owner": owner, "umo": umo, "ready": bool(owner and umo)}


class BlockingRuntime:
    """Store adapter with per-bot asyncio serialization and safe public snapshots."""

    def __init__(self, store: PolicyStore):
        self.store = store
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._lease_marker = contextvars.ContextVar(f"ablock_lease_{id(self)}", default=None)

    @staticmethod
    def _valid_context(context: dict[str, Any]) -> tuple[dict[str, str], str]:
        if not isinstance(context, dict) or not context.get("ready") or not isinstance(context.get("owner"), dict):
            raise PolicyDenied("policy context is unavailable")
        owner = context["owner"]
        platform = str(owner.get("platform_instance_id") or "").strip()
        self_id = str(owner.get("self_id") or "").strip()
        umo = str(context.get("umo") or "").strip()
        if not platform or not self_id or not umo:
            raise PolicyDenied("policy context is unavailable")
        return {"platform_instance_id": platform, "self_id": self_id}, umo

    def _owner_lock(self, owner: dict[str, str]) -> asyncio.Lock:
        key = (owner["platform_instance_id"], owner["self_id"])
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def peek_policy(self, context: dict[str, Any], target_uid: Any) -> dict[str, Any]:
        owner, umo = self._valid_context(context)
        policy = self.store.peek_policy(owner, target_uid, umo)
        return {**policy, "active": policy["mode"] is not None}

    async def get_policy(self, context: dict[str, Any], target_uid: Any) -> dict[str, Any]:
        # Store reads are synchronous SQLite calls; this API stays async for callers
        # that share an async interface with mutation/lease operations.
        return self.peek_policy(context, target_uid)

    def policy_snapshot(self, context: dict[str, Any]) -> dict[str, Any]:
        owner, umo = self._valid_context(context)
        snapshot = self.store.snapshot(owner, umo)
        return {
            "schema_version": SCHEMA_VERSION,
            "owner": copy.deepcopy(snapshot["owner"]),
            "umo": snapshot["umo"],
            "owner_revision": snapshot["owner_revision"],
            "shield_epoch": snapshot.get("shield_epoch", 0),
            "rules": copy.deepcopy(snapshot["rules"]),
            "targets": copy.deepcopy(snapshot["targets"]),
            "active": bool(snapshot["rules"]),
        }

    def capture_token(self, context: dict[str, Any], target_uid: Any) -> dict[str, int]:
        policy = self.peek_policy(context, target_uid)
        return {"owner_revision": policy["owner_revision"],
                "target_revision": policy["target_revision"], "shield_epoch": policy["shield_epoch"]}

    def _check_mutation_token(self, context: dict[str, Any], target_uid: str,
                              expected_token: dict[str, Any] | None) -> None:
        if expected_token is None:
            return
        current = self.peek_policy(context, target_uid)
        if current["mode"] is not None or any(
            expected_token.get(field) != current.get(field)
            for field in ("owner_revision", "target_revision", "shield_epoch")
        ):
            raise PolicyDenied("interaction decision token is stale or no longer allowed")

    @asynccontextmanager
    async def send_lease(self, context: dict[str, Any], target_uid: Any, token: dict[str, Any] | None = None):
        owner, _ = self._valid_context(context)
        owner_key = (owner["platform_instance_id"], owner["self_id"])
        task = asyncio.current_task()
        marker = self._lease_marker.get()
        nested = marker == (owner_key, task)
        lock = self._owner_lock(owner)
        if not nested:
            await lock.acquire()
        marker_token = None
        lock = self._owner_lock(owner)
        try:
            if not nested:
                marker_token = self._lease_marker.set((owner_key, task))
            policy = self.peek_policy(context, target_uid)
            if token is not None:
                if not isinstance(token, dict) or any(
                    token.get(field) != policy[field] for field in ("target_revision", "shield_epoch")
                ):
                    raise PolicyDenied("draft policy token is stale")
            if not policy["reply_allowed"]:
                raise PolicyDenied("reply is blocked")
            yield True
        finally:
            if marker_token is not None:
                self._lease_marker.reset(marker_token)
            if not nested:
                lock.release()

    async def set_interaction_boundary(
        self, event: Any, mode: str, duration: Any, scope: str = "bot",
        target_user_id: Any = "", reason: str = "", *, expected_token: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = resolve_context(event)
        owner, umo = self._valid_context(context)
        author = str(event.get_sender_id() or "").strip()
        requested = str(target_user_id or "").strip()
        if not author or (requested and requested != author):
            raise PolicyDenied("only the current event author can be targeted")
        if author == owner["self_id"]:
            raise PolicyDenied("self-targeting is forbidden")
        if mode not in MODES or scope not in {"bot", "session"}:
            raise ValueError("invalid mode or scope")
        permanent, seconds = parse_duration(duration)
        async with self._owner_lock(owner):
            self._check_mutation_token(context, author, expected_token)
            return self.store.set_rule(
                owner, author, mode, scope_type=scope, umo=umo, duration_seconds=seconds,
                permanent=permanent, reason=reason, actor_kind="bot", actor_uid=author,
                event_id=self._event_id(event), action_id=uuid.uuid4().hex,
            )

    async def clear_interaction_boundary(
        self, event: Any, target_user_id: Any, scope: str = "all", reason: str = "",
        *, expected_token: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = resolve_context(event)
        owner, umo = self._valid_context(context)
        target = str(target_user_id or "").strip()
        if not target or target == owner["self_id"]:
            raise ValueError("valid non-self target UID required")
        if scope not in {"all", "bot", "session"}:
            raise ValueError("invalid scope")
        async with self._owner_lock(owner):
            author = str(event.get_sender_id() or "").strip()
            if not author:
                raise PolicyDenied("current event author is unavailable")
            self._check_mutation_token(context, author, expected_token)
            return self.store.clear_rule(
                owner, target, scope_type=scope, umo=umo, reason=reason,
                actor_kind="bot", actor_uid=str(event.get_sender_id() or ""),
                event_id=self._event_id(event), action_id=uuid.uuid4().hex,
            )

    @staticmethod
    def _event_id(event: Any) -> str:
        return str(getattr(event, "event_id", "") or getattr(event, "id", "") or uuid.uuid4().hex)
