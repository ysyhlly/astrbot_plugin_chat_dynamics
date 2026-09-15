"""Group-scoped notebook: anniversaries, one-shot reminders, slang trials."""

from __future__ import annotations

import logging
import math
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .persist import atomic_write_json, read_umo_json, safe_umo

logger = logging.getLogger("astrbot_plugin_chat_dynamics.group_memory")

_SENSITIVE = ("黄", "赌", "毒", "裸", "色情", "政治", "习近")

# Bound the in-process view: the JSON file is the authority, so an evicted entry
# only costs one re-read. Without a cap the cache grows for every umo the web
# API is ever asked about.
_CACHE_LIMIT = 256
# Upper bound for a single mute, so a bad request can never silence a group
# forever (json parses Infinity, and stamp + inf stays inf through a round trip).
_MAX_MUTE_HOURS = 720.0


def _clears_mute(value: Any) -> bool:
    """True when the caller asked to end a mute rather than start one."""
    try:
        return float(value) <= 0.0
    except (TypeError, ValueError):
        return False


def _finite_hours(value: Any, default: float = 10.0) -> float:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(hours):
        logger.warning("non-finite mute duration rejected; using %.1fh", default)
        return default
    return min(_MAX_MUTE_HOURS, max(1.0, hours))


def _safe_umo(umo: str) -> str:
    return safe_umo(umo)


class GroupMemoryNotebook:
    def __init__(self, data_dir: Path, *, bridge: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.enabled = True
        self.slang_enabled = False
        self._cache: Dict[str, Dict[str, Any]] = {}

    def configure(self, *, enabled: bool = True, slang_enabled: bool = False, bridge: Any = None) -> None:
        self.enabled = bool(enabled)
        self.slang_enabled = bool(slang_enabled)
        if bridge is not None:
            self.bridge = bridge

    def _path(self, umo: str) -> Path:
        return self.data_dir / f"notebook_{_safe_umo(umo)}.json"

    def _cache_put(self, key: str, data: Dict[str, Any]) -> None:
        if key not in self._cache and len(self._cache) >= _CACHE_LIMIT:
            for oldest in list(self._cache)[: max(1, _CACHE_LIMIT // 4)]:
                self._cache.pop(oldest, None)
        self._cache[key] = data

    def _load(self, umo: str) -> Dict[str, Any]:
        key = _safe_umo(umo)
        if key in self._cache:
            return self._cache[key]
        data: Dict[str, Any] = {
            "anniversaries": [],
            "reminders": [],
            "slang_trials": [],
            "mute_until": 0.0,
        }
        try:
            data.update(read_umo_json(self.data_dir, "notebook", umo))
        except Exception as exc:  # noqa: BLE001
            logger.debug("notebook load failed type=%s", type(exc).__name__)
        self._cache_put(key, data)
        return data

    def _save(self, umo: str, data: Dict[str, Any]) -> None:
        data["umo"] = str(umo or "")
        self._cache_put(_safe_umo(umo), data)
        try:
            atomic_write_json(self._path(umo), data)
        except Exception as exc:  # noqa: BLE001
            # A lost write is user-visible state (anniversaries, reminders,
            # mute), so it must not be silent; DEBUG is below the default level.
            logger.warning("notebook save failed for one session type=%s", type(exc).__name__)

    def list_all(self, umo: str) -> Dict[str, Any]:
        data = self._load(umo)
        return {
            "anniversaries": list(data.get("anniversaries") or []),
            "reminders": list(data.get("reminders") or []),
            "slang_trials": list(data.get("slang_trials") or []),
            "mute_until": float(data.get("mute_until") or 0),
        }

    def mute_tonight(self, umo: str, *, hours: float = 10.0, now: Optional[float] = None) -> float:
        stamp = time.time() if now is None else float(now)
        data = self._load(umo)
        # A non-positive duration clears an active mute instead of clamping up
        # to an hour: moving the deadline to now is exactly what every reader
        # treats as "not muted", and the panel needs a way to end a ten-hour
        # silence early.
        until = stamp if _clears_mute(hours) else stamp + _finite_hours(hours) * 3600.0
        data["mute_until"] = until
        self._save(umo, data)
        return until

    # --- D1 anniversaries --------------------------------------------------

    def add_anniversary(
        self,
        umo: str,
        *,
        title: str,
        month: int,
        day: int,
        note: str = "",
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("group_memory_disabled")
        title = (title or "").strip()[:40]
        if not title:
            raise ValueError("title required")
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
            raise ValueError("invalid date")
        item = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "month": int(month),
            "day": int(day),
            "note": (note or "").strip()[:80],
            "enabled": True,
        }
        data = self._load(umo)
        rows = list(data.get("anniversaries") or [])
        rows.append(item)
        data["anniversaries"] = rows[-50:]
        self._save(umo, data)
        return item

    def remove_anniversary(self, umo: str, item_id: str) -> bool:
        data = self._load(umo)
        before = list(data.get("anniversaries") or [])
        after = [r for r in before if r.get("id") != item_id]
        data["anniversaries"] = after
        self._save(umo, data)
        return len(after) != len(before)

    def due_anniversaries(self, umo: str, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        stamp = time.time() if now is None else float(now)
        data = self._load(umo)
        if float(data.get("mute_until") or 0) > stamp:
            return []
        local = time.localtime(stamp)
        return [
            r
            for r in (data.get("anniversaries") or [])
            if r.get("enabled", True) and int(r.get("month") or 0) == local.tm_mon and int(r.get("day") or 0) == local.tm_mday
        ]

    # --- D2 reminders (one nudge then expire) ------------------------------

    def add_reminder(
        self,
        umo: str,
        *,
        text: str,
        due_at: float,
        created_by: str = "",
    ) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("group_memory_disabled")
        text = (text or "").strip()[:80]
        if not text:
            raise ValueError("text required")
        item = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "due_at": float(due_at),
            "created_by": str(created_by or "")[:64],
            "nudged": False,
            "expired": False,
        }
        data = self._load(umo)
        rows = list(data.get("reminders") or [])
        rows.append(item)
        data["reminders"] = rows[-80:]
        self._save(umo, data)
        return item

    def remove_reminder(self, umo: str, item_id: str) -> bool:
        data = self._load(umo)
        before = list(data.get("reminders") or [])
        after = [r for r in before if r.get("id") != item_id]
        data["reminders"] = after
        self._save(umo, data)
        return len(after) != len(before)

    def pop_due_reminders(self, umo: str, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Return due reminders once, then mark nudged/expired (one-shot)."""
        if not self.enabled:
            return []
        stamp = time.time() if now is None else float(now)
        data = self._load(umo)
        if float(data.get("mute_until") or 0) > stamp:
            return []
        due: List[Dict[str, Any]] = []
        changed = False
        rows = list(data.get("reminders") or [])
        for row in rows:
            if row.get("expired") or row.get("nudged"):
                # Expire old nudged items after they were shown once.
                if row.get("nudged") and not row.get("expired"):
                    row["expired"] = True
                    changed = True
                continue
            if float(row.get("due_at") or 0) <= stamp:
                row["nudged"] = True
                row["expired"] = True  # one nudge then expire
                due.append(dict(row))
                changed = True
        if changed:
            data["reminders"] = rows
            self._save(umo, data)
        return due

    # --- D3 slang trials ---------------------------------------------------

    async def add_slang_trial_async(self, umo: str, *, phrase: str, approved: bool = False) -> Dict[str, Any]:
        """Resolve external approval before the synchronous local write."""
        if not self.slang_enabled:
            raise RuntimeError("slang_trial_disabled")
        phrase = (phrase or "").strip()[:24]
        if not approved and self.bridge is not None:
            candidates = await self.bridge.fetch_slang_candidates_async(umo=umo, limit=20)
            approved = any(phrase == str(c.get("phrase") or c.get("text") or c.get("tag") or "")
                           for c in candidates)
        if not approved:
            raise ValueError("unapproved_slang")
        return self.add_slang_trial(umo, phrase=phrase, approved=True)

    def add_slang_trial(
        self,
        umo: str,
        *,
        phrase: str,
        approved: bool = False,
    ) -> Dict[str, Any]:
        if not self.slang_enabled:
            raise RuntimeError("slang_trial_disabled")
        phrase = (phrase or "").strip()[:24]
        if not phrase:
            raise ValueError("phrase required")
        if any(s in phrase for s in _SENSITIVE):
            raise ValueError("sensitive_blocked")
        # Prefer selflearning approval when connected.
        if not approved and self.bridge is not None:
            try:
                candidates = self.bridge.fetch_slang_candidates(umo=umo, limit=20) or []
                approved = any(
                    phrase == str(c.get("phrase") or c.get("text") or c.get("tag") or "")
                    for c in candidates
                )
            except Exception:
                approved = False
        if not approved:
            raise ValueError("unapproved_slang")
        item = {
            "id": uuid.uuid4().hex[:12],
            "phrase": phrase,
            "uses": 0,
            "cold_retract": False,
            "approved": True,
        }
        data = self._load(umo)
        rows = list(data.get("slang_trials") or [])
        rows.append(item)
        data["slang_trials"] = rows[-40:]
        self._save(umo, data)
        return item

    def remove_slang(self, umo: str, item_id: str) -> bool:
        data = self._load(umo)
        before = list(data.get("slang_trials") or [])
        after = [r for r in before if r.get("id") != item_id]
        data["slang_trials"] = after
        self._save(umo, data)
        return len(after) != len(before)

    def try_use_slang(self, umo: str, *, now: Optional[float] = None) -> Optional[str]:
        """Soft-use one approved slang once; cold retract afterwards."""
        if not self.slang_enabled:
            return None
        stamp = time.time() if now is None else float(now)
        data = self._load(umo)
        if float(data.get("mute_until") or 0) > stamp:
            return None
        rows = list(data.get("slang_trials") or [])
        for row in rows:
            if row.get("cold_retract") or int(row.get("uses") or 0) >= 1:
                continue
            if not row.get("approved"):
                continue
            phrase = str(row.get("phrase") or "")
            if not phrase or any(s in phrase for s in _SENSITIVE):
                row["cold_retract"] = True
                continue
            row["uses"] = 1
            data["slang_trials"] = rows
            self._save(umo, data)
            return phrase
        return None

    def retract_cold_slang(self, umo: str, phrase: str) -> None:
        data = self._load(umo)
        for row in data.get("slang_trials") or []:
            if row.get("phrase") == phrase:
                row["cold_retract"] = True
        self._save(umo, data)
