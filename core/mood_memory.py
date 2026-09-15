"""Short mood/topic tags per (umo, peer) with decay. No long raw text, no cross-group."""

from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .persist import atomic_write_json, read_umo_json, safe_umo

logger = logging.getLogger("astrbot_plugin_chat_dynamics.mood_memory")

_BLOCKED_TAGS = {"conflict", "romance", "恋爱", "冲突", "吵架", "暧昧"}
_TAG_RE = re.compile(r"^[\w\u4e00-\u9fff\-·]{1,24}$")

# The JSON file is the authority, so eviction only costs one re-read.
_CACHE_LIMIT = 256
# Same bound as the notebook: a mute must not be able to become permanent.
_MAX_MUTE_HOURS = 720.0


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


class MoodMemoryStore:
    """Local short-tag mood memory; prefers selflearning approved memories when connected."""

    def __init__(self, data_dir: Path, *, bridge: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge
        self.enabled = False
        self._cache: Dict[str, Dict[str, Any]] = {}
        # (umo, peer) -> (stamp, rows) recalled from local notes plus the companion.
        # The request hook reads this instead of awaiting a companion query in front
        # of the model call, so a slow partner delays nothing.
        self._recall_cache: Dict[tuple, tuple] = {}
        self.recall_ttl = 120.0

    def configure(self, *, enabled: bool, bridge: Any = None) -> None:
        self.enabled = bool(enabled)
        if bridge is not None:
            self.bridge = bridge

    def _path(self, umo: str) -> Path:
        return self.data_dir / f"mood_{_safe_umo(umo)}.json"

    def _cache_put(self, key: str, data: Dict[str, Any]) -> None:
        if key not in self._cache and len(self._cache) >= _CACHE_LIMIT:
            for oldest in list(self._cache)[: max(1, _CACHE_LIMIT // 4)]:
                self._cache.pop(oldest, None)
        self._cache[key] = data

    def _load(self, umo: str) -> Dict[str, Any]:
        key = _safe_umo(umo)
        if key in self._cache:
            return self._cache[key]
        data: Dict[str, Any] = {"peers": {}, "forgotten": {}, "mute_until": 0.0}
        try:
            data.update(read_umo_json(self.data_dir, "mood", umo))
        except Exception as exc:  # noqa: BLE001
            logger.debug("mood load failed type=%s", type(exc).__name__)
        self._cache_put(key, data)
        return data

    def _save(self, umo: str, data: Dict[str, Any]) -> None:
        data["umo"] = str(umo or "")
        key = _safe_umo(umo)
        self._cache_put(key, data)
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
            logger.warning("mood save failed for one session type=%s", type(exc).__name__)

    def mute_tonight(self, umo: str, *, hours: float = 10.0, now: Optional[float] = None) -> float:
        stamp = time.time() if now is None else float(now)
        data = self._load(umo)
        clear = False
        try:
            clear = float(hours) <= 0.0
        except (TypeError, ValueError):
            clear = False
        until = stamp if clear else stamp + _finite_hours(hours) * 3600.0
        data["mute_until"] = until
        self._save(umo, data)
        self._invalidate_recall(umo)
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
            self._invalidate_recall(umo, peer_id)
            return True
        if peer_id:
            peers.pop(peer_id, None)
            # Mark all tags for peer forgotten.
            forgotten[f"{peer_id}::*"] = time.time()
            self._save(umo, data)
            self._invalidate_recall(umo, peer_id)
            return True
        data["peers"] = {}
        data["forgotten"] = {"*": time.time()}
        self._save(umo, data)
        return True

    # Local tag writing. Nothing in this plugin calls this today: the runtime
    # only reads approved tags, either from this store or from the companion's
    # own approved memories (see recall()). It is kept as the documented write
    # seam for a companion or an operator tool — deleting it would remove the
    # only way to seed the local store, and wiring it to an LLM tag classifier is
    # a feature decision, not a fix.
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
        self._invalidate_recall(umo, peer_id)

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
        # A member who asked to forget one tag gets no companion memories at all: an
        # arbitrary prose tag cannot be matched against the forgotten word, so the
        # conservative answer is to omit remote rows while that request stands. Local
        # notes are unaffected and are filtered per tag below.
        if self.bridge is not None and self.remote_context_allowed(umo, peer_id):
            remote = await self.bridge.fetch_approved_memories_async(umo=umo, peer_id=peer_id, limit=limit)
        # Re-read local policy after the await; a concurrent forget must win.
        return self.recall(umo, peer_id, now=now, limit=limit, remote_rows=remote)

    async def refresh_async(self, umo: str, peer_id: str, *, limit: int = 3) -> List[Dict[str, Any]]:
        """Warm the tag cache for one member; called from the message path.

        This is where the selflearning companion is actually read (`recall_async` →
        `fetch_approved_memories_async`, bounded by the bridge's own timeout). The
        request hook then serves the result without touching disk or the companion.
        """
        rows = await self.recall_async(umo, peer_id, limit=limit)
        self._recall_cache[(str(umo), str(peer_id))] = (time.time(), [dict(row) for row in rows])
        self._prune_recall_cache()
        return rows

    def cached_recall(self, umo: str, peer_id: str, *, limit: int = 3) -> List[Dict[str, Any]]:
        """Tags warmed by `refresh_async`, or [] when there is nothing fresh."""
        entry = self._recall_cache.get((str(umo), str(peer_id)))
        if not entry:
            return []
        stamp, rows = entry
        if time.time() - float(stamp) > self.recall_ttl:
            return []
        return [dict(row) for row in list(rows)[: max(0, int(limit))]]

    def recall_is_fresh(self, umo: str, peer_id: str) -> bool:
        """Whether a warmed answer exists; an empty answer counts as an answer."""
        entry = self._recall_cache.get((str(umo), str(peer_id)))
        return bool(entry) and (time.time() - float(entry[0])) <= self.recall_ttl

    def _prune_recall_cache(self, *, keep: int = 512) -> None:
        stamp = time.time()
        expired = [key for key, (at, _rows) in self._recall_cache.items()
                   if stamp - float(at) > self.recall_ttl]
        for key in expired:
            self._recall_cache.pop(key, None)
        if len(self._recall_cache) > keep:
            oldest = sorted(self._recall_cache, key=lambda key: self._recall_cache[key][0])
            for key in oldest[: len(self._recall_cache) - keep]:
                self._recall_cache.pop(key, None)

    def _invalidate_recall(self, umo: str, peer_id: str = "") -> None:
        """A forget or a mute must not be answered from a warmed cache."""
        target = str(umo)
        if peer_id:
            self._recall_cache.pop((target, str(peer_id)), None)
            return
        for key in [key for key in self._recall_cache if key[0] == target]:
            self._recall_cache.pop(key, None)

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
