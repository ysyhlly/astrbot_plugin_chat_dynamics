"""The vector cache bounds entry lifetime and makes replay order-independent.

match() answers with the neural backend only when both sides are cached, so an
LRU that quietly evicted one side changes routing results rather than only
timing. These tests pin the two properties that fix that: entries expire, and a
known corpus can be made fully cached before a replay.
"""
from __future__ import annotations

import pytest

from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
from .test_embeddings import FakeContext, FakeEmbeddingProvider


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_entries_expire_and_expiry_is_counted():
    clock = Clock()
    adapter = EmbeddingAdapter(enabled=True, cache_ttl=60.0, clock=clock)
    adapter.remember("超时了", [1.0, 0.0])
    assert adapter.cached("超时了") is not None
    clock.now += 59
    assert adapter.cached("超时了") is not None
    clock.now += 2
    assert adapter.cached("超时了") is None
    assert adapter.cache_len == 0
    assert adapter.snapshot_stats()["expired"] == 1


def test_zero_ttl_keeps_entries_until_evicted():
    clock = Clock()
    adapter = EmbeddingAdapter(enabled=True, cache_ttl=0.0, clock=clock)
    adapter.remember("x", [1.0, 0.0])
    clock.now += 10_000
    assert adapter.cached("x") is not None
    assert adapter.snapshot_stats()["expired"] == 0


def test_eviction_keeps_the_timestamp_map_in_step():
    adapter = EmbeddingAdapter(enabled=True, cache_size=32)
    for index in range(40):
        adapter.remember(f"t{index}", [1.0, 0.0])
    assert adapter.cache_len == 32
    assert adapter.cached("t0") is None and adapter.cached("t39") is not None
    # The two structures must not drift apart, or evicted keys would linger.
    assert len(adapter._stamps) == adapter.cache_len


def test_reconfigure_to_a_smaller_cache_drops_orphaned_stamps():
    adapter = EmbeddingAdapter(enabled=True, cache_size=64)
    for index in range(64):
        adapter.remember(f"t{index}", [1.0, 0.0])
    adapter.configure(cache_size=32)
    assert adapter.cache_len == 32 and len(adapter._stamps) == 32


@pytest.mark.asyncio
async def test_warm_removes_eviction_order_dependence():
    provider = FakeEmbeddingProvider()
    adapter = EmbeddingAdapter(FakeContext(provider), enabled=True)
    left, right = "启动报错", "启动时提示错误"
    # Cold: one side is missing, so the decision comes from the hashed backend.
    assert adapter.match(left, right).backend == "hashed"
    assert await adapter.warm([left, right]) == 2
    warm_match = adapter.match(left, right)
    assert warm_match.backend == "neural"
    assert warm_match.embedding_cosine is not None
    assert adapter.snapshot_stats()["warms"] == 1
    # Warming twice is free: everything is already cached.
    assert await adapter.warm([left, right]) == 2
    assert provider.calls.count(left) == 1


@pytest.mark.asyncio
async def test_warm_ignores_blank_and_duplicate_texts():
    provider = FakeEmbeddingProvider()
    adapter = EmbeddingAdapter(FakeContext(provider), enabled=True)
    assert await adapter.warm(["  ", "启动报错", "启动报错", ""]) == 1
    assert provider.calls == ["启动报错"]


@pytest.mark.asyncio
async def test_warm_is_a_noop_when_the_adapter_is_disabled():
    adapter = EmbeddingAdapter(FakeContext(FakeEmbeddingProvider()), enabled=False)
    assert await adapter.warm(["a", "b"]) == 0
    assert adapter.cache_len == 0
