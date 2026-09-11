"""Resource counters reflect work without retaining message content."""
import asyncio
from copy import deepcopy
import json

import pytest

from astrbot_plugin_chat_dynamics.core.conversation_context import context_statistics
from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter


@pytest.mark.asyncio
async def test_embedding_stats_count_admission_sharing_and_cache():
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        def get_all_embedding_providers(self):
            return [self]

        async def get_embedding(self, text):
            started.set()
            await release.wait()
            return [1, 0]

    adapter = EmbeddingAdapter(Provider(), enabled=True, max_inflight=1)
    first = asyncio.create_task(adapter.embed("private text"))
    await asyncio.wait_for(started.wait(), 1)
    duplicate = asyncio.create_task(adapter.embed("private text"))
    await asyncio.sleep(0)
    assert await adapter.embed("over capacity") is None
    stats = adapter.snapshot_stats()
    assert stats["provider_calls"] == stats["singleflight_joins"] == 1
    assert stats["inflight"] == stats["capacity_fallbacks"] == 1
    stats["provider_calls"] = 999
    assert adapter.snapshot_stats()["provider_calls"] == 1
    release.set()
    await asyncio.gather(first, duplicate)
    await adapter.embed("private text")
    stats = adapter.snapshot_stats()
    assert stats["cache_hits"] == stats["cache_entries"] == 1
    assert stats["inflight"] == 0
    assert all(isinstance(value, int) for value in stats.values())
    adapter.configure(enabled=False)
    assert adapter.snapshot_stats()["provider_calls"] == 1
    assert adapter.snapshot_stats()["cache_entries"] == 0


@pytest.mark.asyncio
async def test_embedding_stats_separate_timeout_failure_and_missing_provider():
    class Provider:
        def get_all_embedding_providers(self):
            return [self]

        async def get_embedding(self, text):
            if text == "timeout":
                await asyncio.Event().wait()
            if text == "error":
                raise ValueError("private provider error")
            return []

    adapter = EmbeddingAdapter(Provider(), enabled=True)
    adapter.timeout = .01
    for text in ("timeout", "error", "empty"):
        assert await adapter.embed(text) is None
    stats = adapter.snapshot_stats()
    assert stats["provider_calls"] == 3
    assert stats["timeouts"] == 1
    assert stats["failures"] == 2
    adapter.configure(context=object())
    assert await adapter.embed("missing") is None
    assert adapter.snapshot_stats()["provider_unavailable"] == 1
    assert adapter.snapshot_stats()["provider_calls"] == 3


@pytest.mark.asyncio
async def test_embedding_cancellation_is_not_recorded_as_failure():
    started = asyncio.Event()

    class Provider:
        def get_all_embedding_providers(self):
            return [self]

        async def get_embedding(self, text):
            started.set()
            await asyncio.Event().wait()

    adapter = EmbeddingAdapter(Provider(), enabled=True)
    waiter = asyncio.create_task(adapter.embed("cancel"))
    await asyncio.wait_for(started.wait(), 1)
    adapter.configure(enabled=False)
    await asyncio.gather(waiter, return_exceptions=True)
    stats = adapter.snapshot_stats()
    assert stats["provider_calls"] == 1
    assert stats["failures"] == stats["timeouts"] == stats["inflight"] == 0


def test_context_stats_count_characters_without_mutation_or_token_claims():
    payload = {
        "current_turn": "你好\n猫",
        "message_semantics": [{"message_id": "a"}, {"message_id": "b"}],
        "background_conversation_data": [{"text": "previous"}, {"text": "猫"}],
        "attribution_note": "untrusted",
        "social_hint": "",
    }
    before = deepcopy(payload)
    assert context_statistics(payload) == {
        "current_characters": 4,
        "background_characters": 9,
        "current_messages": 2,
        "background_messages": 2,
        "total_messages": 4,
        "serialized_characters": len(json.dumps(payload, ensure_ascii=False)),
    }
    assert payload == before
    assert context_statistics({})["serialized_characters"] == 2
