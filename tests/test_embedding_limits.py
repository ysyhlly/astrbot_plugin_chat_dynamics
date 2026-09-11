"""Embedding provenance and burst/reconfiguration regressions."""
import asyncio
from dataclasses import replace

import pytest

from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter


def test_match_backend_survives_interleaved_matches():
    adapter = EmbeddingAdapter(enabled=True)
    adapter.remember("left", [1, 0])
    adapter.remember("right", [1, 0])
    neural = adapter.match("left", "right")
    hashed = adapter.match("uncached", "other")
    assert neural.backend == "neural"
    assert hashed.backend == "hashed"
    # Isolate the adapter's backend-specific compatibility rule.
    evidence = replace(neural, score=0, lexical_ratio=0, overlap_count=0,
                       concept_affinity=0, shared_scenes=())
    assert adapter.last_backend == "hashed"
    assert adapter.should_link(evidence)
    adapter.match("left", "right")
    assert not adapter.should_link(replace(evidence, backend="hashed"))


@pytest.mark.asyncio
async def test_distinct_burst_is_bounded_and_duplicates_still_join():
    release = asyncio.Event()
    started = asyncio.Event()
    calls = []

    class Context:
        def get_all_embedding_providers(self):
            return [self]

        async def get_embedding(self, text):
            calls.append(text)
            if len(calls) == 2:
                started.set()
            await release.wait()
            return [1, 0]

    adapter = EmbeddingAdapter(Context(), enabled=True, max_inflight=2)
    initial = [asyncio.create_task(adapter.embed(str(i))) for i in range(2)]
    await asyncio.wait_for(started.wait(), 1)
    duplicate = asyncio.create_task(adapter.embed("0"))
    assert await asyncio.gather(*(adapter.embed(str(i)) for i in range(2, 100))) == [None] * 98
    assert len(adapter._pending_tasks) == 2
    release.set()
    assert await asyncio.gather(*initial, duplicate) == [(1., 0.)] * 3
    assert calls == ["0", "1"]
    assert not adapter._pending_tasks


@pytest.mark.asyncio
async def test_reconfigure_rejects_old_result_even_if_provider_swallows_cancel():
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Context:
        def get_all_embedding_providers(self):
            return [self]

        async def get_embedding(self, text):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return [1, 0]

    adapter = EmbeddingAdapter(Context(), enabled=True)
    waiter = asyncio.create_task(adapter.embed("old"))
    await asyncio.wait_for(started.wait(), 1)
    adapter.configure(enabled=False)
    await asyncio.wait_for(cancelled.wait(), 1)
    release.set()
    await asyncio.gather(waiter, return_exceptions=True)
    assert adapter.cached("old") is None
    assert adapter.last_backend == "hashed"
    assert not adapter._pending_tasks


@pytest.mark.asyncio
async def test_terminate_awaits_embedding_task_no_longer_in_text_map():
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin

    plugin = _plugin({})
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def old_request():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()

    old = asyncio.create_task(old_request())
    # A replacement of the same text can leave the old generation only here.
    plugin.embeddings._pending_tasks.add(old)
    await asyncio.wait_for(started.wait(), 1)
    shutdown = asyncio.create_task(plugin.terminate())
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        assert old not in plugin.embeddings._inflight.values()
        assert not shutdown.done()
        release.set()
        await asyncio.wait_for(shutdown, 1)
        assert old.done()
    finally:
        release.set()
        if not old.done():
            old.cancel()
        await asyncio.gather(old, shutdown, return_exceptions=True)
