"""Per-group override profiles (presence, threshold, shadow_mode, natural rhythm) with atomic persistence."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_PRESENCE_KNOBS = frozenset({"ghost", "sensible", "lively"})
PROFILE_FIELDS = (
    "presence_knob",
    "reply_probability_threshold",
    "shadow_mode",
    "natural_reply_rhythm_enabled",
)
_MAX_PROFILES = 256


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sanitize_group_profile(raw: Any) -> dict[str, Any]:
    """Validate and normalize a per-group override dict; unset/None keys are omitted (inherit global)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("group profile must be an object")
    unknown = sorted(k for k in raw if k not in set(PROFILE_FIELDS) | {"updated_at", "label"})
    if unknown:
        raise ValueError("unsupported group profile field(s): " + ", ".join(unknown))

    out: dict[str, Any] = {}
    if "presence_knob" in raw and raw["presence_knob"] not in (None, ""):
        val = str(raw["presence_knob"]).strip().lower()
        if val not in VALID_PRESENCE_KNOBS:
            raise ValueError("presence_knob must be one of ghost, sensible, lively")
        out["presence_knob"] = val

    if "reply_probability_threshold" in raw and raw["reply_probability_threshold"] not in (None, ""):
        raw_thr = raw["reply_probability_threshold"]
        if isinstance(raw_thr, bool):
            raise ValueError("reply_probability_threshold must be a number between 0 and 100")
        try:
            thr = float(raw_thr)
        except (TypeError, ValueError) as exc:
            raise ValueError("reply_probability_threshold must be a number between 0 and 100") from exc
        if not math.isfinite(thr) or not 0.0 <= thr <= 100.0:
            raise ValueError("reply_probability_threshold must be between 0 and 100")
        out["reply_probability_threshold"] = round(thr, 1)

    if "shadow_mode" in raw and raw["shadow_mode"] is not None:
        val = raw["shadow_mode"]
        if not isinstance(val, bool):
            raise ValueError("shadow_mode must be a boolean or null")
        out["shadow_mode"] = val

    if "natural_reply_rhythm_enabled" in raw and raw["natural_reply_rhythm_enabled"] is not None:
        val = raw["natural_reply_rhythm_enabled"]
        if not isinstance(val, bool):
            raise ValueError("natural_reply_rhythm_enabled must be a boolean or null")
        out["natural_reply_rhythm_enabled"] = val

    return out


class GroupProfileStore:
    """Thread-safe persisted store for per-group scheduling override profiles."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        self.file_path = Path(file_path) if file_path is not None else None
        self._lock = threading.RLock()
        self._profiles: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _norm_key(umo_or_group: str) -> str:
        return str(umo_or_group or "").strip()[:160]

    def _load(self) -> None:
        if self.file_path is None or not self.file_path.is_file():
            return
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        source = raw.get("profiles") if isinstance(raw, dict) else raw
        if not isinstance(source, dict):
            return
        loaded: dict[str, dict[str, Any]] = {}
        for k, v in source.items():
            key = self._norm_key(k)
            if not key or not isinstance(v, dict):
                continue
            try:
                clean = sanitize_group_profile(v)
            except ValueError:
                continue
            if clean:
                if isinstance(v.get("updated_at"), str) and v["updated_at"].strip():
                    clean["updated_at"] = v["updated_at"].strip()[:64]
                loaded[key] = clean
            if len(loaded) >= _MAX_PROFILES:
                break
        self._profiles = loaded

    def _save_locked(self) -> None:
        if self.file_path is None:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "profiles": {k: dict(v) for k, v in sorted(self._profiles.items())},
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.file_path.parent,
                prefix=".group-profiles-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.file_path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def all_profiles(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._profiles.items()}

    def get(self, session_or_umo: str, *, group_id: str = "") -> dict[str, Any]:
        """Return override dict for exact UMO/session_key, falling back to bare group_id."""
        with self._lock:
            key = self._norm_key(session_or_umo)
            if key and key in self._profiles:
                return dict(self._profiles[key])
            gid = self._norm_key(group_id)
            if not gid and ":" in key:
                gid = key.rsplit(":", 1)[-1].strip()
            if gid and gid in self._profiles:
                return dict(self._profiles[gid])
            return {}

    def set(self, session_or_umo: str, overrides: dict[str, Any] | None) -> dict[str, Any]:
        key = self._norm_key(session_or_umo)
        if not key:
            raise ValueError("session_key / umo is required")
        clean = sanitize_group_profile(overrides)
        with self._lock:
            if not clean:
                self._profiles.pop(key, None)
                self._save_locked()
                return {}
            if key not in self._profiles and len(self._profiles) >= _MAX_PROFILES:
                raise ValueError(f"群画像配置已达上限 ({_MAX_PROFILES})")
            record = {**clean, "updated_at": _now_iso()}
            self._profiles[key] = record
            self._save_locked()
            return dict(record)

    def delete(self, session_or_umo: str) -> bool:
        key = self._norm_key(session_or_umo)
        if not key:
            return False
        with self._lock:
            existed = self._profiles.pop(key, None) is not None
            if existed:
                self._save_locked()
            return existed


def resolve_effective_group_profile(
    plugin: Any,
    session_or_umo: str = "",
    *,
    group_id: str = "",
) -> dict[str, Any]:
    """Merge global RuntimeConfig defaults with per-group overrides for one session."""
    cfg = getattr(plugin, "_runtime_config", None)
    global_defaults = {
        "presence_knob": str(getattr(cfg, "presence_knob", "sensible") or "sensible"),
        "reply_probability_threshold": float(getattr(cfg, "reply_probability_threshold", 70.0)),
        "shadow_mode": bool(getattr(plugin, "shadow_mode", getattr(cfg, "shadow_mode", False))),
        "natural_reply_rhythm_enabled": bool(getattr(cfg, "natural_reply_rhythm_enabled", True)),
    }
    store = getattr(plugin, "group_profiles_store", None)
    overrides: dict[str, Any] = {}
    if store is not None and callable(getattr(store, "get", None)):
        overrides = store.get(session_or_umo, group_id=group_id)
    elif isinstance(getattr(plugin, "_group_profiles", None), dict):
        raw_map = plugin._group_profiles
        key = str(session_or_umo or "").strip()
        gid = str(group_id or "").strip() or (key.rsplit(":", 1)[-1] if ":" in key else "")
        cand = raw_map.get(key) or (raw_map.get(gid) if gid else None)
        if isinstance(cand, dict):
            overrides = dict(cand)

    effective = dict(global_defaults)
    for field in PROFILE_FIELDS:
        if field in overrides and overrides[field] is not None:
            effective[field] = overrides[field]
    return {
        "effective": effective,
        "overrides": {k: overrides[k] for k in PROFILE_FIELDS if k in overrides},
        "defaults": global_defaults,
        "has_overrides": any(k in overrides for k in PROFILE_FIELDS),
        "updated_at": str(overrides.get("updated_at") or ""),
    }
