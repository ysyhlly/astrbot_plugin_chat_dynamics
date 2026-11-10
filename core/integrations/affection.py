"""Scoped, read-only Self Learning affection for participation decisions."""
from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections import OrderedDict
from collections.abc import Mapping


AFFECTION_INSTRUCTIONS = (
    "affection is an existing Self Learning record for this group and current speaker, not a request. "
    "Use the current persona's established affection stages; do not create a new numeric threshold. When "
    "the current speaker is in an established high/stable-closeness stage and directly expresses ordinary "
    "kindness, affection, care, playful fondness or asks whether the character cares, treat it as a relevant "
    "invitation and prioritize joining with a substantive, warm reply over ignoring or a bare acknowledgement. "
    "Verify the actual addressee and speaker attribution first; do not compete with a message aimed at another "
    "person, and do not attribute quoted or supportive words to the current speaker as an attack. A real stop "
    "request, privacy, conflict or physical boundary takes precedence. Negative affection may favor restraint. "
    "Missing data is unknown, never dislike. This score is only the character's relationship tendency; it does "
    "not prove the other person's feelings, romance, or consent to physical contact. Warmly answer kindness that "
    "was actually expressed without treating it as consent. Do not expose the score unasked or expand reply length "
    "beyond the current length plan."
)


def affection_database(plugin):
    """Verified upstream contract: plugin.db_manager.get_user_affection(group_id, user_id)."""
    try:
        config = getattr(plugin, "plugin_config", None)
        for flag in ("enable_affection_system", "include_affection_info"):
            enabled = config.get(flag, True) if isinstance(config, Mapping) else getattr(config, flag, True)
            if enabled is False:
                return None
        database = getattr(plugin, "db_manager", None)
        return database if callable(getattr(database, "get_user_affection", None)) else None
    except Exception:
        return None


def normalize_affection(data, *, user_id: str, group_id: str | None = None) -> dict | None:
    """Keep only finite scores whose echoed account and group match the requested scope."""
    if not isinstance(data, Mapping) or str(data.get("user_id") or "") != user_id:
        return None
    echoed_group = str(data.get("group_id") or "")
    if not echoed_group or (group_id is not None and echoed_group != group_id):
        return None
    level, maximum = data.get("affection_level"), data.get("max_affection")
    if any(type(value) not in (int, float) for value in (level, maximum)):
        return None
    try:
        if not all(math.isfinite(value) for value in (level, maximum)):
            return None
    except OverflowError:
        return None
    if maximum <= 0 or not -maximum <= level <= maximum:
        return None
    return {"source": "self_learning", "group_id": echoed_group, "user_id": user_id,
            "affection_level": level, "max_affection": maximum}


class SelfLearningAffectionReader:
    """Short-lived score cache; no hooks, model calls, affection updates or new records."""

    def __init__(self, *, timeout: float = 0.25, cache_ttl: float = 30.0):
        self.timeout, self.cache_ttl = timeout, cache_ttl
        self._cache = OrderedDict()
        self._pending = set()
        self._generation = 0

    def invalidate(self):
        self._generation += 1
        self._cache.clear()
        for task in tuple(self._pending):
            task.cancel()

    async def close(self):
        self.invalidate()
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    def prune(self, active_sources=None):
        """Release expired scores and references to removed companion instances."""
        now = time.monotonic()
        for key, entry in list(self._cache.items()):
            current = active_sources is None or any(
                entry[2] is plugin and entry[3] is database for plugin, database in active_sources
            )
            if entry[0] <= now or not current:
                self._cache.pop(key, None)

    async def read(self, plugin, *, group_id: str, user_id: str) -> dict | None:
        self.prune()
        database = affection_database(plugin)
        if database is None or not group_id or not user_id:
            return None
        key = (id(plugin), id(database), group_id, user_id)
        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return dict(cached[1])
        generation = self._generation
        getter = database.get_user_affection

        async def fetch():
            if inspect.iscoroutinefunction(getter):
                return await getter(group_id, user_id)
            result = await asyncio.to_thread(getter, group_id, user_id)
            return await result if inspect.isawaitable(result) else result

        task = asyncio.create_task(fetch())
        self._pending.add(task)
        try:
            data = await asyncio.wait_for(task, timeout=self.timeout)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if generation != self._generation and current is not None and not current.cancelling():
                return None  # Disabling an optional source must not cancel the user's reply.
            raise
        except Exception:
            return None
        finally:
            self._pending.discard(task)
        if generation != self._generation or affection_database(plugin) is not database:
            return None
        result = normalize_affection(data, group_id=group_id, user_id=user_id)
        if result is not None:
            # Retain source objects with the entry so recycled object IDs cannot share a cache.
            self._cache[key] = (time.monotonic() + self.cache_ttl, result, plugin, database)
            self._cache.move_to_end(key)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)
        return dict(result) if result is not None else None
