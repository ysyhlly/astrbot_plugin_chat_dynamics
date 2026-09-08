"""Short mood/topic tags per (umo, peer) with decay. No long raw text, no cross-group."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .persist import atomic_write_json, safe_umo

logger = logging.getLogger("astrbot_plugin_chat_dynamics.mood_memory")

_BLOCKED_TAGS = {"conflict", "romance", "恋爱", "冲突", "吵架", "暧昧"}
_TAG_RE = re.compile(r"^[\w\u4e00-\u9fff\-·]{1,24}$")


def _safe_umo(umo: str) -> str:
    return safe_umo(umo)


class MoodMemoryStore:
    """Local short-tag mood memory; prefers selflearning approved memories when connected."""

    def __init__(self, data_dir: Path, *, bridge: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.enabled = False
        self._cache: Dict[str, Dict[str, Any]] = {}

    def configure(self, *, enabled: bool, bridge: Any = None) -> None:
        self.enabled = bool(enabled)
        if bridge is not None:
            self.bridge = bridge

    def _path(self, umo: str) -> Path:
        return self.data_dir / f"mood_{_safe_umo(umo)}.json"

    def _load(self, umo: str) -> Dict[str, Any]:
        key = _safe_umo(umo)
        if key in self._cache:
            return self._cache[key]
        path = self._path(umo)
        data: Dict[str, Any] = {"peers": {}, "forgotten": {}, "mute_until": 0.0}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update(raw)
            except Exception as exc:  # noqa: BLE001
                logger.debug("mood load failed type=%s", type(exc).__name__)
        self._cache[key] = data
        return data

    def _save(self, umo: str, data: Dict[str, Any]) -> None:
        key = _safe_umo(umo)
        self._cache[key] = data
        path = self._path(umo)
        try:
            # Keep storage small: cap peers.
            peers = data.get("peers") if isinstance(data.get("peers"), dict) else {}
            if len(peers) > 200:
                # Drop oldest by last_seen.
                ordered = sorted(
                    peers.items(),
                    key=lambda kv: float((kv[1] or {}).get("last_seen") or 0.0),
                )
                data["peers"] = dict(ordered[-200:])
            atomic_write_json(path, data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mood save failed type=%s", type(exc).__name__)

    def mute_tonight(self, umo: str, *, hours: float = 10.0, now: Optional[float] = None) -> float:
        stamp = time.time() if now is None else float(now)
        data = self._load(umo)
        until = stamp + max(1.0, float(hours) * 3600.0)
        data["mute_until"] = until
        self._save(umo, data)
        return until

    def forget(self, umo: str, peer_id: str = "", *, tag: str = "") -> bool:
        data = self._load(umo)
        peers = data.setdefault("peers", {})
        forgotten = data.setdefault("forgotten", {})
        if peer_id and tag:
            key = f"{peer_id}::{tag}"
            forgotten[key] = time.time()
            peer = peers.get(peer_id) or {}
            tags = [t for t in peer.get("tags", []) if t.get("tag") != tag]
            peer["tags"] = tags
            peers[peer_id] = peer
            self._save(umo, data)
            return True
        if peer_id:
            peers.pop(peer_id, None)
            # Mark all tags for peer forgotten.
            forgotten[f"{peer_id}::*"] = time.time()
            self._save(umo, data)
            return True
        data["peers"] = {}
        data["forgotten"] = {"*": time.time()}
        self._save(umo, data)
        return True

    def remember(
        self,
        umo: str,
        peer_id: str,
        tags: Sequence[str],
        *,
        now: Optional[float] = None,
        half_life_hours: float = 72.0,
    ) -> None:
        if not self.enabled or not peer_id:
            return
        stamp = time.time() if now is None else float(now)
        data = self._load(umo)
        if float(data.get("mute_until") or 0) > stamp:
            return
        forgotten = data.get("forgotten") or {}
        if forgotten.get(f"{peer_id}::*") or forgotten.get("*"):
            return
        peer = data.setdefault("peers", {}).setdefault(peer_id, {"tags": [], "last_seen": stamp})
        existing = {t["tag"]: t for t in peer.get("tags", []) if isinstance(t, dict) and "tag" in t}
        for raw in tags:
            tag = self._normalize_tag(raw)
            if not tag or tag in _BLOCKED_TAGS:
                continue
            if forgotten.get(f"{peer_id}::{tag}"):
                continue
            existing[tag] = {
                "tag": tag,
                "weight": 1.0,
                "updated_at": stamp,
                "half_life_hours": half_life_hours,
            }
        peer["tags"] = list(existing.values())[-12:]
        peer["last_seen"] = stamp
        self._save(umo, data)

    def recall(
        self,
        umo: str,
        peer_id: str,
        *,
        now: Optional[float] = None,
        limit: int = 3,
        remote_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Return decayed local tags; prefer selflearning approved memories when present."""
        stamp = time.time() if now is None else float(now)
        if not self.enabled:
            return []
        data = self._load(umo)
        if float(data.get("mute_until") or 0) > stamp:
            return []
        forgotten = data.get("forgotten") or {}
        if forgotten.get(f"{peer_id}::*") or forgotten.get("*"):
            return []

        remote: List[Dict[str, Any]] = remote_rows or []
        bridge = self.bridge
        if remote_rows is None and bridge is not None:
            try:
                remote = bridge.fetch_approved_memories(umo=umo, peer_id=peer_id, limit=limit) or []
            except Exception:
                remote = []
        cleaned_remote: List[Dict[str, Any]] = []
        for item in remote:
            tag = self._normalize_tag(str(item.get("tag") or item.get("label") or item.get("text") or ""))
            if not tag or tag in _BLOCKED_TAGS:
                continue
            if forgotten.get(f"{peer_id}::{tag}"):
                continue
            # Never pull long raw text into recall payload.
            try:
                weight = float(item.get("weight", 0.8))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(weight) or weight <= 0:
                continue
            cleaned_remote.append({"tag": tag, "weight": min(weight, 1.0), "source": "selflearning"})
            if len(cleaned_remote) >= limit:
                break
        if cleaned_remote:
            return cleaned_remote

        peer = (data.get("peers") or {}).get(peer_id) or {}
        rows: List[Dict[str, Any]] = []
        for item in peer.get("tags") or []:
            if not isinstance(item, dict):
                continue
            tag = self._normalize_tag(str(item.get("tag") or ""))
            if not tag or tag in _BLOCKED_TAGS:
                continue
            if forgotten.get(f"{peer_id}::{tag}"):
                continue
            updated = float(item.get("updated_at") or stamp)
            half = max(1.0, float(item.get("half_life_hours") or 72.0))
            age_h = max(0.0, (stamp - updated) / 3600.0)
            weight = float(item.get("weight") or 1.0) * (0.5 ** (age_h / half))
            if weight < 0.15:
                continue
            rows.append({"tag": tag, "weight": round(weight, 3), "source": "local"})
        rows.sort(key=lambda r: r["weight"], reverse=True)
        return rows[:limit]

    async def recall_async(self, umo: str, peer_id: str, *, now: Optional[float] = None, limit: int = 3) -> List[Dict[str, Any]]:
        """Await companion IO without caching remote data or bypassing forget/mute."""
        stamp = time.time() if now is None else float(now)
        if not self.enabled:
            return []
        data = self._load(umo)
        forgotten = data.get("forgotten") or {}
        if float(data.get("mute_until") or 0) > stamp or forgotten.get(f"{peer_id}::*") or forgotten.get("*"):
            return []
        remote = []
        if self.bridge is not None:
            remote = await self.bridge.fetch_approved_memories_async(umo=umo, peer_id=peer_id, limit=limit)
        # Re-read local policy after the await; a concurrent forget must win.
        return self.recall(umo, peer_id, now=now, limit=limit, remote_rows=remote)

    def remote_context_allowed(self, umo: str, peer_id: str) -> bool:
        """Honor local forgetting even for general companion memory context.

        A forgotten tag cannot reliably be matched to arbitrary prose, so
        conservatively omit remote memories for that member while it exists.
        """
        data = self._load(umo)
        forgotten = data.get("forgotten") or {}
        return not (float(data.get("mute_until") or 0) > time.time()
                    or forgotten.get("*")
                    or any(value and key.startswith(f"{peer_id}::") for key, value in forgotten.items()))

    @staticmethod
    def _normalize_tag(raw: str) -> str:
        tag = (raw or "").strip().replace("\n", " ")
        if len(tag) > 24:
            tag = tag[:24]
        if not tag or not _TAG_RE.match(tag):
            # Soft cleanup: keep CJK/alnum only.
            tag = re.sub(r"[^\w\u4e00-\u9fff\-·]", "", tag)[:24]
        if tag.lower() in _BLOCKED_TAGS or tag in _BLOCKED_TAGS:
            return ""
        return tag
