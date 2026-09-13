"""Bounded operational comparisons, deliberately separate from labelled samples."""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from typing import Any

KV_KEY = "shadow_telemetry_v1"
SALT_KEY = "shadow_telemetry_salt_v1"


class ShadowTelemetry:
    def __init__(self, *, retention_seconds: int = 30 * 86400, max_records: int = 20000) -> None:
        self.retention_seconds = max(1, retention_seconds)
        self.max_records = max(1, max_records)
        self.salt = secrets.token_hex(32)
        self.rows: dict[str, dict[str, Any]] = {}

    def _hash(self, value: str) -> str:
        return hmac.new(self.salt.encode(), value.encode(), hashlib.sha256).hexdigest()

    def prune(self, now: float) -> None:
        self.rows = {key: row for key, row in self.rows.items()
                     if now - self.retention_seconds <= row["recorded_at"] <= now}
        if len(self.rows) > self.max_records:
            recent = sorted(self.rows.values(), key=lambda row: row["recorded_at"])[-self.max_records:]
            self.rows = {row["observation_id"]: row for row in recent}

    def record(self, comparison: dict[str, Any], *, session: str, message_id: str,
               host_version: str) -> bool:
        now = float(comparison["recorded_at"])
        if not math.isfinite(now) or now <= 0:
            return False
        self.prune(now)
        policy_id = str(comparison["policy_id"])
        identity = self._hash(repr((session, message_id, policy_id, host_version)))
        if identity in self.rows:
            return False
        row = {key: comparison[key] for key in (
            "recorded_at", "policy_id", "reason", "baseline_reply", "shadow_reply",
            "score", "baseline_threshold", "shadow_threshold", "changed")}
        row.update(observation_id=identity, session_hash=self._hash(session), host_version=host_version)
        self.rows[identity] = row
        self.prune(now)
        return True

    def export(self, now: float) -> dict[str, Any]:
        self.prune(now)
        return {"schema_version": 1, "updated_at": now,
                "retention_seconds": self.retention_seconds, "max_records": self.max_records,
                "observations": [dict(row) for row in self.rows.values()]}

    def restore(self, payload: Any, now: float) -> None:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return
        allowed = {"observation_id", "recorded_at", "policy_id", "host_version", "session_hash",
                   "reason", "baseline_reply", "shadow_reply", "score", "baseline_threshold",
                   "shadow_threshold", "changed"}
        rows = payload.get("observations", [])
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict) or not allowed.issubset(row):
                continue
            stamp = row["recorded_at"]
            if not isinstance(stamp, (float, int)) or isinstance(stamp, bool) or not math.isfinite(stamp):
                continue
            if not isinstance(row["observation_id"], str):
                continue
            self.rows[row["observation_id"]] = {key: row[key] for key in allowed}
        self.prune(now)
