"""Durable, synchronous SQLite policy store.

No platform or AstrBot imports belong here.  Callers pass trusted runtime
identity, and must never call an async function while a store transaction is
open.  Writes commit rule, audit, and revision state together before publishing
the new RAM mirror.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .contracts import MODES, SCHEMA_VERSION, SCOPES


MAX_DURATION_SECONDS = 10 * 365 * 24 * 60 * 60
MAX_REASON_LENGTH = 500
MAX_INSPECTION_ROWS = 1000


class PolicyStoreError(RuntimeError):
    """Base class for store errors."""


class ValidationError(PolicyStoreError, ValueError):
    """Raised when an input violates the policy contract."""


def _text(value: Any, name: str, *, required: bool = True) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        result = str(value).strip()
    if required and not result:
        raise ValidationError(f"{name} must be non-empty")
    return result


def _owner(owner: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(owner, dict):
        raise ValidationError("owner must be a mapping")
    return (
        _text(owner.get("platform_instance_id"), "owner.platform_instance_id"),
        _text(owner.get("self_id"), "owner.self_id"),
    )


def _utc_now(clock: Callable[[], Any]) -> datetime:
    value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("clock datetime must include a UTC timezone")
        value = value.astimezone(timezone.utc)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValidationError("clock timestamp must be finite")
        value = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        raise ValidationError("clock must return an aware datetime or epoch seconds")
    return value.replace(microsecond=0)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    # Persisted values are always normalized UTC; reject malformed or naive data.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted expiry is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class PolicyStore:
    """Owns one WAL SQLite database and an immutable-by-copy RAM policy mirror."""

    def __init__(self, path: str | os.PathLike[str], clock: Callable[[], Any] | None = None):
        self.path = str(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._closed = False
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._create_schema()
            self._cache: dict[tuple[str, str, str], tuple[dict[str, Any], ...]] = {}
            self._reload_cache_locked()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rules (
                rule_id TEXT PRIMARY KEY,
                platform_instance_id TEXT NOT NULL,
                self_id TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('silence','shield')),
                scope_type TEXT NOT NULL CHECK(scope_type IN ('bot','session')),
                umo TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                reason TEXT NOT NULL,
                fact_kind TEXT NOT NULL CHECK(fact_kind='subjective'),
                actor_kind TEXT NOT NULL CHECK(actor_kind IN ('bot','admin')),
                actor_uid TEXT NOT NULL,
                event_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS rules_lookup
                ON rules(platform_instance_id,self_id,target_uid,active,scope_type,umo);
            CREATE TABLE IF NOT EXISTS owner_meta (
                platform_instance_id TEXT NOT NULL,
                self_id TEXT NOT NULL,
                owner_revision INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(platform_instance_id,self_id)
            );
            CREATE TABLE IF NOT EXISTS target_meta (
                platform_instance_id TEXT NOT NULL,
                self_id TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                target_revision INTEGER NOT NULL DEFAULT 0,
                shield_epoch INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(platform_instance_id,self_id,target_uid)
            );
            CREATE TABLE IF NOT EXISTS audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_instance_id TEXT NOT NULL,
                self_id TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                action TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                umo TEXT NOT NULL,
                mode TEXT,
                reason TEXT NOT NULL,
                fact_kind TEXT NOT NULL CHECK(fact_kind='subjective'),
                actor_kind TEXT NOT NULL,
                actor_uid TEXT NOT NULL,
                event_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                changed INTEGER NOT NULL CHECK(changed IN (0,1)),
                owner_revision INTEGER NOT NULL,
                target_revision INTEGER NOT NULL,
                shield_epoch INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS audit_lookup
                ON audit(platform_instance_id,self_id,audit_id DESC);
            CREATE TABLE IF NOT EXISTS actions (
                platform_instance_id TEXT NOT NULL,
                self_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(platform_instance_id,self_id,action_id)
            );
            """
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise PolicyStoreError("store is closed")

    def _reload_cache_locked(self) -> None:
        rows = self._conn.execute(
            "SELECT * FROM rules WHERE active=1 ORDER BY created_at,rule_id"
        ).fetchall()
        cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (row["platform_instance_id"], row["self_id"], row["target_uid"])
            cache.setdefault(key, []).append(self._row_rule(row))
        owner_revisions = {
            (row["platform_instance_id"], row["self_id"]): int(row["owner_revision"])
            for row in self._conn.execute("SELECT * FROM owner_meta").fetchall()
        }
        target_revisions = {
            (row["platform_instance_id"], row["self_id"], row["target_uid"]):
                (int(row["target_revision"]), int(row["shield_epoch"]))
            for row in self._conn.execute("SELECT * FROM target_meta").fetchall()
        }
        self._cache = {key: tuple(value) for key, value in cache.items()}
        self._owner_revisions = owner_revisions
        self._target_revisions = target_revisions

    @staticmethod
    def _row_rule(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "target_uid": row["target_uid"],
            "mode": row["mode"],
            "scope_type": row["scope_type"],
            "umo": row["umo"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "reason": row["reason"],
            "fact_kind": row["fact_kind"],
            "actor_kind": row["actor_kind"],
            "actor_uid": row["actor_uid"],
            "event_id": row["event_id"],
            "active": bool(row["active"]),
        }

    @staticmethod
    def _public_rule(rule: dict[str, Any]) -> dict[str, Any]:
        return {
            "rule_id": rule["rule_id"],
            "target_uid": rule["target_uid"],
            "mode": rule["mode"],
            "scope_type": rule["scope_type"],
            "umo": rule["umo"],
            "created_at": rule["created_at"],
            "expires_at": rule["expires_at"],
        }

    @staticmethod
    def _reason(reason: Any) -> str:
        value = _text(reason, "reason", required=False)
        value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")
        if len(value) > MAX_REASON_LENGTH:
            raise ValidationError(f"reason must be at most {MAX_REASON_LENGTH} characters")
        return value

    def _validate_target(self, owner: tuple[str, str], target_uid: Any) -> str:
        target = _text(target_uid, "target_uid")
        if target == owner[1]:
            raise ValidationError("self-targeting is forbidden")
        return target

    def _active_rules(self, owner: tuple[str, str], target_uid: str, umo: str) -> list[dict[str, Any]]:
        now = _utc_now(self._clock)
        key = (*owner, target_uid)
        candidates = self._cache.get(key, ())
        active = []
        for rule in candidates:
            if rule["scope_type"] == "session" and rule["umo"] != umo:
                continue
            expiry = rule["expires_at"]
            if expiry is not None and _parse_utc(expiry) <= now:
                continue
            active.append(dict(rule))
        return active

    def _active_rules_sql(self, owner: tuple[str, str], target_uid: str, umo: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM rules WHERE platform_instance_id=? AND self_id=? AND target_uid=? AND active=1 "
            "ORDER BY created_at,rule_id",
            (*owner, target_uid),
        ).fetchall()
        now = _utc_now(self._clock)
        active = []
        for row in rows:
            rule = self._row_rule(row)
            if rule["scope_type"] == "session" and rule["umo"] != umo:
                continue
            if rule["expires_at"] is not None and _parse_utc(rule["expires_at"]) <= now:
                continue
            active.append(rule)
        return active

    def _revisions(self, owner: tuple[str, str], target_uid: str) -> tuple[int, int, int]:
        owner_row = self._conn.execute(
            "SELECT owner_revision FROM owner_meta WHERE platform_instance_id=? AND self_id=?", owner
        ).fetchone()
        target_row = self._conn.execute(
            "SELECT target_revision FROM target_meta "
            "WHERE platform_instance_id=? AND self_id=? AND target_uid=?",
            (*owner, target_uid),
        ).fetchone()
        owner_shield_epoch = self._conn.execute(
            "SELECT COALESCE(SUM(shield_epoch),0) FROM target_meta "
            "WHERE platform_instance_id=? AND self_id=?",
            owner,
        ).fetchone()[0]
        return (
            int(owner_row[0]) if owner_row else 0,
            int(target_row[0]) if target_row else 0,
            int(owner_shield_epoch or 0),
        )

    def _policy(self, owner: tuple[str, str], target_uid: str, umo: str,
                active: list[dict[str, Any]] | None = None,
                revisions: tuple[int, int, int] | None = None) -> dict[str, Any]:
        if active is None:
            active = self._active_rules(owner, target_uid, umo)
        modes = {rule["mode"] for rule in active}
        mode = "shield" if "shield" in modes else "silence" if "silence" in modes else None
        owner_revision, target_revision, shield_epoch = (
            revisions if revisions is not None else self._revisions(owner, target_uid)
        )
        expires = [rule["expires_at"] for rule in active if rule["mode"] == mode and rule["expires_at"]]
        return {
            "schema_version": SCHEMA_VERSION,
            "reply_allowed": mode is None,
            "read_allowed": mode != "shield",
            "mode": mode,
            "expires_at": min(expires, key=_parse_utc) if expires else None,
            "rules": [self._public_rule(rule) for rule in active],
            "owner_revision": owner_revision,
            "target_revision": target_revision,
            "shield_epoch": shield_epoch,
        }

    def peek_policy(self, owner: dict[str, Any], target_uid: Any, umo: Any = "") -> dict[str, Any]:
        """Return effective policy from the RAM mirror without touching SQLite."""
        with self._lock:
            self._ensure_open()
            normalized_owner = _owner(owner)
            target = self._validate_target(normalized_owner, target_uid)
            cached_revisions = (
                self._owner_revisions.get(normalized_owner, 0),
                self._target_revisions.get((*normalized_owner, target), (0, 0))[0],
                sum(
                    shield_epoch
                    for (platform, self_id, _target), (_target_revision, shield_epoch)
                    in self._target_revisions.items()
                    if (platform, self_id) == normalized_owner
                ),
            )
            return self._policy(normalized_owner, target, _text(umo, "umo", required=False),
                                revisions=cached_revisions)

    def _mutation_args(
        self,
        owner: dict[str, Any],
        target_uid: Any,
        *,
        actor_kind: str,
        actor_uid: Any,
        event_id: Any,
        action_id: Any,
        reason: Any,
    ) -> tuple[tuple[str, str], str, str, str, str, str]:
        normalized_owner = _owner(owner)
        target = self._validate_target(normalized_owner, target_uid)
        if actor_kind not in {"bot", "admin"}:
            raise ValidationError("actor_kind must be 'bot' or 'admin'")
        return (
            normalized_owner,
            target,
            _text(actor_uid, "actor_uid", required=False),
            _text(event_id, "event_id", required=False) or uuid.uuid4().hex,
            _text(action_id, "action_id", required=False) or uuid.uuid4().hex,
            self._reason(reason),
        )

    def _idempotent_receipt(self, owner: tuple[str, str], action_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT receipt_json FROM actions WHERE platform_instance_id=? AND self_id=? AND action_id=?",
            (*owner, action_id),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_action(self, owner: dict[str, Any], action_id: Any) -> dict[str, Any] | None:
        """Return an idempotency receipt and its audit row for admin API replay checks."""
        with self._lock:
            self._ensure_open()
            normalized_owner = _owner(owner)
            key = _text(action_id, "action_id")
            receipt = self._idempotent_receipt(normalized_owner, key)
            if receipt is None:
                return None
            row = self._conn.execute(
                "SELECT * FROM audit WHERE platform_instance_id=? AND self_id=? AND action_id=? "
                "ORDER BY audit_id DESC LIMIT 1",
                (*normalized_owner, key),
            ).fetchone()
            return {"receipt": receipt, "audit": dict(row) if row else None}

    def _revision_tx(self, owner: tuple[str, str], target_uid: str, *, changed: bool, shield_changed: bool) -> tuple[int, int, int]:
        if changed:
            self._conn.execute(
                "INSERT INTO owner_meta(platform_instance_id,self_id,owner_revision) VALUES(?,?,1) "
                "ON CONFLICT(platform_instance_id,self_id) DO UPDATE SET owner_revision=owner_revision+1",
                owner,
            )
            self._conn.execute(
                "INSERT INTO target_meta(platform_instance_id,self_id,target_uid,target_revision,shield_epoch) "
                "VALUES(?,?,?,1,?) ON CONFLICT(platform_instance_id,self_id,target_uid) DO UPDATE SET "
                "target_revision=target_revision+1, shield_epoch=shield_epoch+excluded.shield_epoch",
                (*owner, target_uid, 1 if shield_changed else 0),
            )
        return self._revisions(owner, target_uid)

    def _finish_mutation(
        self,
        *,
        owner: tuple[str, str],
        target_uid: str,
        umo: str,
        action: str,
        scope_type: str,
        mode: str | None,
        reason: str,
        actor_kind: str,
        actor_uid: str,
        event_id: str,
        action_id: str,
        changed: bool,
        shield_changed: bool,
    ) -> dict[str, Any]:
        revisions = self._revision_tx(owner, target_uid, changed=changed, shield_changed=shield_changed)
        created_at = _iso_utc(_utc_now(self._clock))
        self._conn.execute(
            "INSERT INTO audit(platform_instance_id,self_id,target_uid,action,scope_type,umo,mode,reason,fact_kind,"
            "actor_kind,actor_uid,event_id,action_id,created_at,changed,owner_revision,target_revision,shield_epoch) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*owner, target_uid, action, scope_type, umo, mode, reason, "subjective", actor_kind,
             actor_uid, event_id, action_id, created_at, int(changed), *revisions),
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "action_id": action_id,
            "event_id": event_id,
            "changed": changed,
            "owner_revision": revisions[0],
            "target_revision": revisions[1],
            "shield_epoch": revisions[2],
            "_owner": list(owner),
            "_target_uid": target_uid,
            "_umo": umo,
        }

    def _mutate(self, operation: Callable[[], dict[str, Any]], owner: tuple[str, str], action_id: str) -> dict[str, Any]:
        self._ensure_open()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            previous = self._idempotent_receipt(owner, action_id)
            if previous is not None:
                self._conn.execute("COMMIT")
                return previous
            raw = operation()
            target = raw["_target_uid"]
            umo = raw["_umo"]
            receipt = dict(raw)
            receipt.pop("_owner", None)
            receipt.pop("_target_uid", None)
            receipt.pop("_umo", None)
            active = self._active_rules_sql(owner, target, umo)
            receipt["active_rules"] = [self._public_rule(rule) for rule in active]
            receipt["effective"] = self._policy(owner, target, umo, active)
            self._conn.execute(
                "INSERT INTO actions(platform_instance_id,self_id,action_id,receipt_json) VALUES(?,?,?,?)",
                (*owner, action_id, _json(receipt)),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._reload_cache_locked()
        return json.loads(_json(receipt))

    def set_rule(
        self,
        owner: dict[str, Any],
        target_uid: Any,
        mode: str,
        scope_type: str = "bot",
        umo: Any = "",
        duration_seconds: float | None = None,
        permanent: bool = False,
        reason: Any = "",
        actor_kind: str = "bot",
        actor_uid: Any = "",
        event_id: Any = "",
        action_id: Any = "",
    ) -> dict[str, Any]:
        with self._lock:
            if mode not in MODES:
                raise ValidationError("mode must be 'silence' or 'shield'")
            if scope_type not in SCOPES:
                raise ValidationError("scope_type must be 'bot' or 'session'")
            session = _text(umo, "umo", required=scope_type == "session")
            if scope_type == "bot":
                session = ""
            if isinstance(permanent, bool) is False:
                raise ValidationError("permanent must be a bool")
            if permanent and duration_seconds is not None:
                raise ValidationError("permanent and duration_seconds are mutually exclusive")
            if not permanent and duration_seconds is None:
                raise ValidationError("provide duration_seconds or set permanent=True")
            expires_at = None
            if not permanent:
                if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
                    raise ValidationError("duration_seconds must be a finite positive number")
                duration = float(duration_seconds)
                if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION_SECONDS:
                    raise ValidationError("duration_seconds must be > 0 and at most 10 years")
                expires_at = _iso_utc(_utc_now(self._clock) + timedelta(seconds=math.ceil(duration)))
            args = self._mutation_args(owner, target_uid, actor_kind=actor_kind, actor_uid=actor_uid,
                                       event_id=event_id, action_id=action_id, reason=reason)
            normalized_owner, target, actor, event, action_key, clean_reason = args

            def operation() -> dict[str, Any]:
                prior = self._conn.execute(
                    "SELECT mode FROM rules WHERE platform_instance_id=? AND self_id=? AND target_uid=? "
                    "AND active=1 AND scope_type=? AND umo=?",
                    (*normalized_owner, target, scope_type, session),
                ).fetchall()
                changed = True
                self._conn.execute(
                    "UPDATE rules SET active=0 WHERE platform_instance_id=? AND self_id=? AND target_uid=? "
                    "AND active=1 AND scope_type=? AND umo=?",
                    (*normalized_owner, target, scope_type, session),
                )
                self._conn.execute(
                    "INSERT INTO rules(rule_id,platform_instance_id,self_id,target_uid,mode,scope_type,umo,created_at,"
                    "expires_at,active,reason,fact_kind,actor_kind,actor_uid,event_id) VALUES(?,?,?,?,?,?,?,?,?,1,?,'subjective',?,?,?)",
                    (uuid.uuid4().hex, *normalized_owner, target, mode, scope_type, session,
                     _iso_utc(_utc_now(self._clock)), expires_at, clean_reason, actor_kind, actor, event),
                )
                shield_changed = mode == "shield" or any(row[0] == "shield" for row in prior)
                return self._finish_mutation(
                    owner=normalized_owner, target_uid=target, umo=session, action="set", scope_type=scope_type,
                    mode=mode, reason=clean_reason, actor_kind=actor_kind, actor_uid=actor, event_id=event,
                    action_id=action_key, changed=changed, shield_changed=shield_changed,
                )

            return self._mutate(operation, normalized_owner, action_key)

    def clear_rule(
        self,
        owner: dict[str, Any],
        target_uid: Any,
        scope_type: str = "all",
        umo: Any = "",
        reason: Any = "",
        actor_kind: str = "admin",
        actor_uid: Any = "",
        event_id: Any = "",
        action_id: Any = "",
    ) -> dict[str, Any]:
        with self._lock:
            if scope_type not in {"all", "bot", "session"}:
                raise ValidationError("scope_type must be 'all', 'bot', or 'session'")
            session = _text(umo, "umo", required=scope_type == "session")
            args = self._mutation_args(owner, target_uid, actor_kind=actor_kind, actor_uid=actor_uid,
                                       event_id=event_id, action_id=action_id, reason=reason)
            normalized_owner, target, actor, event, action_key, clean_reason = args
            if scope_type == "all":
                scope, session = "all", ""
            elif scope_type == "bot":
                session = ""
                scope = "bot"
            else:
                scope = "session"

            def operation() -> dict[str, Any]:
                where = "platform_instance_id=? AND self_id=? AND target_uid=? AND active=1"
                params: list[Any] = [*normalized_owner, target]
                if scope == "bot":
                    where += " AND scope_type='bot'"
                elif scope == "session":
                    where += " AND scope_type='session' AND umo=?"
                    params.append(session)
                rows = self._conn.execute(f"SELECT mode FROM rules WHERE {where}", params).fetchall()
                changed = bool(rows)
                if changed:
                    self._conn.execute(f"UPDATE rules SET active=0 WHERE {where}", params)
                shield_changed = any(row[0] == "shield" for row in rows)
                return self._finish_mutation(
                    owner=normalized_owner, target_uid=target, umo=session, action="clear", scope_type=scope,
                    mode=None, reason=clean_reason, actor_kind=actor_kind, actor_uid=actor, event_id=event,
                    action_id=action_key, changed=changed, shield_changed=shield_changed,
                )

            return self._mutate(operation, normalized_owner, action_key)

    def snapshot(self, owner: dict[str, Any], umo: Any = "") -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            normalized_owner = _owner(owner)
            session = _text(umo, "umo", required=False)
            now = _utc_now(self._clock)
            rules = []
            for (platform, self_id, target), candidates in self._cache.items():
                if (platform, self_id) != normalized_owner:
                    continue
                for rule in candidates:
                    if rule["scope_type"] == "session" and rule["umo"] != session:
                        continue
                    if rule["expires_at"] and _parse_utc(rule["expires_at"]) <= now:
                        continue
                    rules.append(self._public_rule(rule))
            # Include metadata for targets whose rules were cleared or expired.
            # Senders use this as an arrival-time draft token; omitting inactive
            # targets would make a cleared UID look like revision zero forever.
            target_rows = self._conn.execute(
                "SELECT target_uid,target_revision,shield_epoch FROM target_meta "
                "WHERE platform_instance_id=? AND self_id=?",
                normalized_owner,
            ).fetchall()
            owner_shield_epoch = sum(int(row["shield_epoch"]) for row in target_rows)
            revisions = {
                row["target_uid"]: {
                    "target_revision": int(row["target_revision"]),
                    "shield_epoch": owner_shield_epoch,
                }
                for row in target_rows
            }
            owner_row = self._conn.execute(
                "SELECT owner_revision FROM owner_meta WHERE platform_instance_id=? AND self_id=?", normalized_owner
            ).fetchone()
            return {
                "schema_version": SCHEMA_VERSION,
                "owner": {"platform_instance_id": normalized_owner[0], "self_id": normalized_owner[1]},
                "umo": session,
                "owner_revision": int(owner_row[0]) if owner_row else 0,
                "shield_epoch": owner_shield_epoch,
                "rules": rules,
                "targets": revisions,
            }

    def list_rules(self, owner: dict[str, Any], limit: int = 100, include_inactive: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            normalized_owner = _owner(owner)
            cap = self._limit(limit)
            sql = "SELECT * FROM rules WHERE platform_instance_id=? AND self_id=?"
            params: list[Any] = list(normalized_owner)
            if not include_inactive:
                sql += " AND active=1"
            sql += " ORDER BY created_at DESC,rule_id DESC LIMIT ?"
            params.append(cap)
            return [self._row_rule(row) for row in self._conn.execute(sql, params).fetchall()]

    def audit(self, owner: dict[str, Any], limit: int = 100, target_uid: Any | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            normalized_owner = _owner(owner)
            cap = self._limit(limit)
            sql = "SELECT * FROM audit WHERE platform_instance_id=? AND self_id=?"
            params: list[Any] = list(normalized_owner)
            if target_uid is not None:
                sql += " AND target_uid=?"
                params.append(self._validate_target(normalized_owner, target_uid))
            sql += " ORDER BY audit_id DESC LIMIT ?"
            params.append(cap)
            result = []
            for row in self._conn.execute(sql, params).fetchall():
                result.append({key: row[key] for key in row.keys()})
            return result

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError("limit must be a positive integer")
        return min(value, MAX_INSPECTION_ROWS)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "PolicyStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
