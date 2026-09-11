"""Neural embedding access for Chat Dynamics.

Uses AstrBot ``EmbeddingProvider`` when configured. The DAG path stays
synchronous: ``match()`` reads a process-local cache and falls back to the
hashed n-gram embedding if a neural vector is not ready yet.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections import OrderedDict
from typing import Any, Optional, Sequence, Tuple

from .semantics import SemanticMatch, semantic_match

logger = logging.getLogger("astrbot_plugin_chat_dynamics.embedding")


def _normalize_vec(values: Sequence[float]) -> Tuple[float, ...]:
    try:
        nums = [float(item) for item in values]
    except (TypeError, ValueError):
        return tuple()
    if not nums or not all(math.isfinite(item) for item in nums):
        return tuple()
    norm = math.sqrt(sum(item * item for item in nums))
    if norm <= 0.0:
        return tuple(nums)
    return tuple(item / norm for item in nums)


def _provider_id(provider: Any) -> str:
    if provider is None:
        return ""
    meta = getattr(provider, "meta", None)
    if meta is not None:
        for attr in ("id", "provider_id"):
            value = getattr(meta, attr, None)
            if value:
                return str(value)
    cfg = getattr(provider, "provider_config", None)
    if isinstance(cfg, dict) and cfg.get("id"):
        return str(cfg["id"])
    for attr in ("id", "provider_id"):
        value = getattr(provider, attr, None)
        if value:
            return str(value)
    return ""


def _extract_vector(payload: Any) -> Optional[Tuple[float, ...]]:
    if payload is None:
        return None
    if isinstance(payload, (list, tuple)) and payload and isinstance(payload[0], (int, float)):
        vector = _normalize_vec(payload)
        return vector or None
    if isinstance(payload, (list, tuple)) and payload:
        first = payload[0]
        if isinstance(first, (list, tuple)):
            return _extract_vector(first)
        if isinstance(first, dict):
            return _extract_vector(first)
        embedding = getattr(first, "embedding", None)
        if embedding is not None:
            return _extract_vector(embedding)
    embedding = getattr(payload, "embedding", None)
    if embedding is not None:
        return _extract_vector(embedding)
    data = getattr(payload, "data", None)
    if data:
        return _extract_vector(data)
    if isinstance(payload, dict):
        for key in ("embedding", "vector", "data"):
            if payload.get(key) is not None:
                return _extract_vector(payload[key])
    return None


class EmbeddingAdapter:
    """Optional neural embedding client with hashed fallback."""

    def __init__(
        self,
        context: Any = None,
        *,
        enabled: bool = False,
        provider_id: str = "",
        cache_size: int = 512,
        link_threshold: float = 0.78,
        timeout: float = 8.0,
        max_inflight: int = 8,
    ) -> None:
        self.context = context
        self.enabled = bool(enabled)
        self.provider_id = str(provider_id or "").strip()
        self.cache_size = max(32, int(cache_size))
        self.link_threshold = float(link_threshold)
        self.timeout = max(1.0, float(timeout))
        self.max_inflight = max(1, int(max_inflight))
        self._cache: OrderedDict[str, Tuple[float, ...]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._pending_tasks: set[asyncio.Task] = set()
        self._generation = 0
        self.last_error: str = ""
        self.last_backend: str = "hashed"

    def configure(
        self,
        *,
        enabled: Optional[bool] = None,
        provider_id: Optional[str] = None,
        cache_size: Optional[int] = None,
        link_threshold: Optional[float] = None,
        context: Any = None,
    ) -> None:
        changed = (
            (provider_id is not None and str(provider_id or "").strip() != self.provider_id)
            or (context is not None and context is not self.context)
            or (enabled is not None and bool(enabled) != self.enabled)
        )
        if changed:
            self._generation += 1
            self._cache.clear()
            self.last_backend = "hashed"
            self.last_error = ""
            for task in self._pending_tasks:
                task.cancel()
        if enabled is not None:
            self.enabled = bool(enabled)
        if provider_id is not None:
            self.provider_id = str(provider_id or "").strip()
        if cache_size is not None:
            self.cache_size = max(32, int(cache_size))
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        if link_threshold is not None:
            self.link_threshold = float(link_threshold)
        if context is not None:
            self.context = context

    @property
    def cache_len(self) -> int:
        return len(self._cache)

    def cached(self, text: str) -> Optional[Tuple[float, ...]]:
        key = (text or "").strip()
        if not key:
            return None
        vec = self._cache.get(key)
        if vec is None:
            return None
        self._cache.move_to_end(key)
        return vec

    def remember(self, text: str, vector: Sequence[float]) -> Tuple[float, ...]:
        key = (text or "").strip()
        normed = _normalize_vec(vector)
        if not key or not normed:
            return normed
        self._cache[key] = normed
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return normed

    def match(self, left_text: str, right_text: str) -> SemanticMatch:
        """Sync matcher used by the DAG. Neural cosine wins when both sides are cached."""
        base = semantic_match(left_text, right_text)
        if not self.enabled:
            self.last_backend = "hashed"
            return base
        left_vec = self.cached(left_text)
        right_vec = self.cached(right_text)
        if left_vec is None or right_vec is None or len(left_vec) != len(right_vec):
            self.last_backend = "hashed"
            return base
        neural = max(0.0, min(1.0, float(sum(a * b for a, b in zip(left_vec, right_vec)))))
        score = round(0.55 * neural + 0.20 * min(1.0, base.lexical_ratio) + 0.25 * base.concept_affinity, 4)
        if neural >= self.link_threshold:
            score = max(score, 0.42)
        self.last_backend = "neural"
        return SemanticMatch(
            lexical_ratio=base.lexical_ratio,
            overlap_count=base.overlap_count,
            embedding_cosine=round(neural, 4),
            concept_affinity=base.concept_affinity,
            score=score,
            shared_scenes=base.shared_scenes,
            backend="neural",
        )

    def should_link(self, match: SemanticMatch) -> bool:
        if match.should_link():
            return True
        return match.backend == "neural" and match.embedding_cosine >= self.link_threshold

    def resolve_provider(self) -> Any:
        ctx = self.context
        if ctx is None:
            return None
        if self.provider_id:
            getter = getattr(ctx, "get_provider_by_id", None)
            if callable(getter):
                try:
                    found = getter(self.provider_id)
                except Exception:
                    found = None
                if found is not None:
                    return found
        listing = getattr(ctx, "get_all_embedding_providers", None)
        if not callable(listing):
            return None
        try:
            providers = list(listing() or [])
        except Exception:
            return None
        if self.provider_id:
            for provider in providers:
                if _provider_id(provider) == self.provider_id:
                    return provider
            return None
        return providers[0] if providers else None

    async def embed(self, text: str) -> Optional[Tuple[float, ...]]:
        """Fetch a neural vector, or None to keep using the hashed fallback."""
        key = (text or "").strip()
        if not key or not self.enabled:
            return None
        cached = self.cached(key)
        if cached is not None:
            self.last_backend = "neural"
            self.last_error = ""
            return cached
        inflight = self._inflight.get(key)
        if (inflight is not None and not inflight.done()
                and getattr(inflight, "_embedding_generation", None) == self._generation):
            try:
                return await asyncio.shield(inflight)
            except Exception:
                return None
        # Same-text waiters join above even at capacity. Distinct bursts fall
        # back immediately instead of accumulating an unbounded task queue.
        if sum(not task.done() for task in self._pending_tasks) >= self.max_inflight:
            return None
        task = asyncio.create_task(self._embed_uncached(key, self._generation))
        task._embedding_generation = self._generation
        self._inflight[key] = task
        self._pending_tasks.add(task)
        def release(completed: asyncio.Task) -> None:
            self._pending_tasks.discard(completed)
            if self._inflight.get(key) is completed:
                self._inflight.pop(key, None)

        task.add_done_callback(release)
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(key) is task and task.done():
                self._inflight.pop(key, None)

    async def _embed_uncached(self, text: str, generation: int) -> Optional[Tuple[float, ...]]:
        if generation != self._generation:
            return None
        provider = self.resolve_provider()
        if provider is None:
            self.last_backend = "hashed"
            self.last_error = "no embedding provider"
            return None
        try:
            raw = await asyncio.wait_for(self._call_provider(provider, text), timeout=self.timeout)
        except Exception as exc:
            if generation != self._generation:
                return None
            self.last_backend = "hashed"
            self.last_error = type(exc).__name__
            logger.debug("[ChatDynamics] Neural embedding failed code=CD_EMBED_FAILED type=%s", type(exc).__name__)
            return None
        if generation != self._generation:
            return None
        vector = _extract_vector(raw)
        if not vector:
            self.last_backend = "hashed"
            self.last_error = "empty embedding"
            return None
        self.last_backend = "neural"
        self.last_error = ""
        return self.remember(text, vector)

    async def _call_provider(self, provider: Any, text: str) -> Any:
        get_embedding = getattr(provider, "get_embedding", None)
        if callable(get_embedding):
            value = get_embedding(text)
            return await value if inspect.isawaitable(value) else value
        get_embeddings = getattr(provider, "get_embeddings", None)
        if callable(get_embeddings):
            value = get_embeddings([text])
            return await value if inspect.isawaitable(value) else value
        raise RuntimeError("Embedding provider has no get_embedding API")
