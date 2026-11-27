"""Unified cross-plugin privacy mute registry with atomic persistence and audit log."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_PRIVACY_SCOPES = frozenset(
    {
        "global",
        "memory",
        "learning",
        "chronicle",
        "horizons",
        "world",
        "games",
        "visual_media",
    }
)
CORE_PRIVACY_SCOPES = ("memory", "learning", "chronicle")
_MAX_AUDIT_ENTRIES = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_scopes(raw: Any, *, default_global: bool = False) -> list[str]:
    if raw is None:
        return ["global"] if default_global else []
    if isinstance(raw, str):
        items = [part.strip().lower() for part in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(part or "").strip().lower() for part in raw]
    else:
        items = []
    cleaned = [scope for scope in dict.fromkeys(items) if scope in VALID_PRIVACY_SCOPES]
    if "global" in cleaned:
        return ["global"]
    if not cleaned and default_global:
        return ["global"]
    return cleaned


class SeriesPrivacyRegistry:
    """Thread-safe persisted registry of per-(umo, member_id) cross-plugin privacy mutes."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []
        self._load()

    @staticmethod
    def _key(umo: str, member_id: str) -> str:
        return f"{str(umo or '').strip()}\x1f{str(member_id or '').strip()}"

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            for k, val in entries.items():
                if not isinstance(k, str) or not isinstance(val, dict):
                    continue
                scopes = _normalize_scopes(val.get("scopes"))
                if not scopes:
                    continue
                self._entries[k] = {
                    "umo": str(val.get("umo") or "").strip(),
                    "member_id": str(val.get("member_id") or "").strip(),
                    "scopes": scopes,
                    "updated_at": str(val.get("updated_at") or ""),
                    "by": str(val.get("by") or "")[:128],
                    "reason": str(val.get("reason") or "")[:200],
                }
        audit = raw.get("audit")
        if isinstance(audit, list):
            self._audit = [dict(row) for row in audit[-_MAX_AUDIT_ENTRIES:] if isinstance(row, dict)]

    def _save_locked(self) -> None:
        if self._path is None:
            return
        payload = {
            "schema_version": 1,
            "entries": self._entries,
            "audit": self._audit[-_MAX_AUDIT_ENTRIES:],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    def get_muted(self, umo: str, member_id: str, scope: str = "") -> dict[str, Any]:
        clean_umo = str(umo or "").strip()
        clean_mid = str(member_id or "").strip()
        clean_scope = str(scope or "").strip().lower()
        if not clean_umo or not clean_mid:
            return {"muted": False, "scopes": [], "updated_at": "", "by": ""}
        with self._lock:
            entry = self._entries.get(self._key(clean_umo, clean_mid))
            if not entry:
                return {"muted": False, "scopes": [], "updated_at": "", "by": ""}
            scopes = list(entry.get("scopes") or [])
            if "global" in scopes:
                is_muted = True
            elif not clean_scope:
                is_muted = bool(scopes)
            else:
                is_muted = clean_scope in scopes
            return {
                "muted": is_muted,
                "scopes": scopes,
                "updated_at": str(entry.get("updated_at") or ""),
                "by": str(entry.get("by") or ""),
            }

    def set_muted(
        self,
        umo: str,
        member_id: str,
        muted: bool,
        *,
        scopes: list[str] | None = None,
        actor: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        clean_umo = str(umo or "").strip()
        clean_mid = str(member_id or "").strip()
        if not clean_umo or not clean_mid:
            raise ValueError("umo and member_id are required")
        clean_actor = str(actor or "").strip()[:128]
        clean_reason = str(reason or "").strip()[:200]
        now = _now_iso()
        key = self._key(clean_umo, clean_mid)

        with self._lock:
            existing = self._entries.get(key)
            current_scopes = list(existing.get("scopes") or []) if existing else []
            req_scopes = _normalize_scopes(scopes, default_global=bool(muted and scopes is None))

            if muted:
                if "global" in req_scopes or "global" in current_scopes or not req_scopes:
                    next_scopes = ["global"]
                else:
                    next_scopes = sorted(set(current_scopes) | set(req_scopes))
                self._entries[key] = {
                    "umo": clean_umo,
                    "member_id": clean_mid,
                    "scopes": next_scopes,
                    "updated_at": now,
                    "by": clean_actor,
                    "reason": clean_reason,
                }
            else:
                if not req_scopes or "global" in req_scopes:
                    next_scopes = []
                else:
                    base = set(CORE_PRIVACY_SCOPES) if "global" in current_scopes else set(current_scopes)
                    next_scopes = sorted(base - set(req_scopes))
                if next_scopes:
                    self._entries[key] = {
                        "umo": clean_umo,
                        "member_id": clean_mid,
                        "scopes": next_scopes,
                        "updated_at": now,
                        "by": clean_actor,
                        "reason": clean_reason,
                    }
                else:
                    self._entries.pop(key, None)

            self._audit.append(
                {
                    "ts": now,
                    "umo": clean_umo,
                    "member_id": clean_mid,
                    "muted": bool(muted),
                    "requested_scopes": req_scopes or (["global"] if not muted else []),
                    "effective_scopes": next_scopes,
                    "actor": clean_actor,
                    "reason": clean_reason,
                }
            )
            if len(self._audit) > _MAX_AUDIT_ENTRIES:
                self._audit = self._audit[-_MAX_AUDIT_ENTRIES:]
            self._save_locked()

        return {
            "ok": True,
            "umo": clean_umo,
            "member_id": clean_mid,
            "muted": bool(next_scopes),
            "scopes": next_scopes,
            "updated_at": now,
            "by": clean_actor,
        }

    def list_muted(self, umo: str = "") -> list[dict[str, Any]]:
        clean_umo = str(umo or "").strip()
        with self._lock:
            rows = [
                dict(entry, scopes=list(entry.get("scopes") or []))
                for entry in self._entries.values()
                if not clean_umo or entry.get("umo") == clean_umo
            ]
        rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        return rows

    def audit_log(self, *, umo: str = "", limit: int = 50) -> list[dict[str, Any]]:
        clean_umo = str(umo or "").strip()
        cap = max(1, min(int(limit or 50), _MAX_AUDIT_ENTRIES))
        with self._lock:
            rows = [
                dict(row)
                for row in reversed(self._audit)
                if not clean_umo or row.get("umo") == clean_umo
            ]
        return rows[:cap]
