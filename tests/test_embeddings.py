"""Neural embedding adapter tests."""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter, _extract_vector
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG


class FakeEmbeddingProvider:
    def __init__(self, provider_id: str = "emb-1"):
        self.id = provider_id
        self.calls: list[str] = []

    async def get_embedding(self, text: str):
        self.calls.append(text)
        if any(token in text for token in ("超时", "502", "网关", "代码", "接口", "报错", "挂了")):
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0, 0.0]


class FakeContext:
    def __init__(self, provider: FakeEmbeddingProvider):
        self.provider = provider

    def get_all_embedding_providers(self):
        return [self.provider]

    def get_provider_by_id(self, provider_id: str):
        if provider_id == self.provider.id:
            return self.provider
        return None


@pytest.mark.asyncio
async def test_duplicate_waiter_cancellation_does_not_cancel_shared_embedding():
    started, release = asyncio.Event(), asyncio.Event()

    class Provider(FakeEmbeddingProvider):
        async def get_embedding(self, text):
            started.set()
            await release.wait()
            return [1.0, 0.0]

    adapter = EmbeddingAdapter(FakeContext(Provider()), enabled=True)
    first = asyncio.create_task(adapter.embed("shared"))
    await started.wait()
    second = asyncio.create_task(adapter.embed("shared"))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    assert await first == (1.0, 0.0)
    await asyncio.sleep(0)
    assert not adapter._inflight


def test_reconfigure_invalidates_neural_cache_and_disabled_match():
    adapter = EmbeddingAdapter(enabled=True, provider_id="old")
    adapter.remember("left", [1.0, 0.0])
    adapter.configure(provider_id="new")
    assert adapter.cached("left") is None
    adapter.remember("left", [1.0, 0.0])
    adapter.remember("right", [1.0, 0.0])
    adapter.configure(enabled=False)
    adapter.match("left", "right")
    assert adapter.last_backend == "hashed"


@pytest.mark.asyncio
async def test_neural_vectors_link_paraphrases_without_surface_overlap():
    provider = FakeEmbeddingProvider()
    adapter = EmbeddingAdapter(FakeContext(provider), enabled=True, link_threshold=0.78)
    left = "服务刚才超时了"
    right = "网关返回 502"
    hashed = adapter.match(left, right)
    assert hashed.should_link() is False

    await adapter.embed(left)
    await adapter.embed(right)
    neural = adapter.match(left, right)
    assert neural.embedding_cosine == pytest.approx(1.0)
    assert neural.should_link() is True
    assert adapter.last_backend == "neural"
    assert provider.calls == [left, right]


@pytest.mark.asyncio
async def test_neural_cache_avoids_duplicate_provider_calls():
    provider = FakeEmbeddingProvider()
    adapter = EmbeddingAdapter(FakeContext(provider), enabled=True)
    await adapter.embed("代码报错")
    await adapter.embed("代码报错")
    assert provider.calls == ["代码报错"]


@pytest.mark.asyncio
async def test_disabled_or_failed_provider_stays_on_hashed_fallback():
    adapter = EmbeddingAdapter(FakeContext(FakeEmbeddingProvider()), enabled=False)
    assert await adapter.embed("代码报错") is None
    assert adapter.match("代码报错", "接口失败").embedding_cosine < 1.0

    class Broken:
        id = "x"

        async def get_embedding(self, text: str):
            raise RuntimeError("boom")

    class BrokenContext:
        def get_all_embedding_providers(self):
            return [Broken()]

    failing = EmbeddingAdapter(BrokenContext(), enabled=True)
    assert await failing.embed("代码报错") is None
    assert failing.last_backend == "hashed"


def test_extract_vector_accepts_provider_payload_shapes():
    assert _extract_vector([0.0, 1.0])[-1] == pytest.approx(1.0)
    nested = _extract_vector([[0.0, 3.0]])
    assert nested is not None and nested[1] == pytest.approx(1.0)
    obj = type("Resp", (), {"data": [type("Row", (), {"embedding": [1.0, 0.0]})()]})()
    assert _extract_vector(obj)[0] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_embed_cancel_keeps_shared_inflight_task():
    started = asyncio.Event()
    release = asyncio.Event()

    class Slow:
        id = "slow-emb"

        async def get_embedding(self, text: str):
            started.set()
            await release.wait()
            return [1.0, 0.0, 0.0, 0.0]

    class Ctx:
        def __init__(self, provider):
            self.provider = provider

        def get_all_embedding_providers(self):
            return [self.provider]

        def get_provider_by_id(self, provider_id: str):
            if provider_id == self.provider.id:
                return self.provider
            return None

    adapter = EmbeddingAdapter(Ctx(Slow()), enabled=True, timeout=5.0)
    task = asyncio.create_task(adapter.embed("hello-cancel"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    inflight = adapter._inflight.get("hello-cancel")
    assert inflight is not None
    assert inflight.done() is False
    release.set()
    await inflight
    assert inflight.done()


def test_extract_vector_rejects_non_finite_payloads():
    assert _extract_vector([float("nan"), 1.0]) is None
    assert _extract_vector([float("inf"), 1.0]) is None


@pytest.mark.asyncio
async def test_cached_neural_match_requires_router_before_inferred_edge():
    provider = FakeEmbeddingProvider()
    adapter = EmbeddingAdapter(FakeContext(provider), enabled=True)
    dag = ConversationDAG(session_id="room", semantic_match_fn=adapter.match)
    first = dag.add_message("a", "u1", "服务刚才超时了", timestamp=1.0)
    await adapter.embed(first.text)
    await adapter.embed("网关返回 502")
    second = dag.add_message("b", "u2", "网关返回 502", timestamp=2.0)
    assert "a" not in second.parent_ids
    from types import SimpleNamespace
    from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter, RoutingState
    runtime = SimpleNamespace(dag=dag, routing_state=RoutingState(), bot_id="bot", last_bot_node=None)
    router = ThreadRouter(require_intense_dialogue=False)
    router.route(runtime, first)
    routing = router.route(runtime, second)
    assert routing.topic_id == first.metadata["routing"]["topic_id"]
    assert routing.parent_message_id == "a"
    assert second.edge_kinds["a"] == "inferred_reply"
    assert routing.addressee_ids == ["u1"]


@pytest.mark.asyncio
async def test_plugin_warmup_does_not_force_topic_before_dialogue_forms():
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin

    plugin = _plugin({"neural_embedding_enabled": True, "takeover_all": True})
    provider = FakeEmbeddingProvider()
    plugin.embeddings.context = FakeContext(provider)
    plugin.embeddings.enabled = True
    key = "mock:GroupMessage:emb"
    runtime = plugin._get_or_create_runtime(key, group_id="emb", umo=key, bot_id="bot")
    first = runtime.dag.add_message("a", "u1", "服务刚才超时了", timestamp=1.0)
    second = runtime.dag.add_message("b", "u2", "网关返回 502", timestamp=2.0)
    assert "a" not in second.parent_ids

    plugin._route_message(runtime, first)
    plugin._route_message(runtime, second)
    await plugin._schedule_neural_embed(key, first)
    await plugin._schedule_neural_embed(key, second)
    assert plugin.embeddings.cached(first.text) is not None
    assert plugin.embeddings.cached(second.text) is not None
    assert second.metadata["routing"]["topic_id"] == ""
    assert first.metadata["routing"]["topic_id"] == ""
    assert second.metadata["routing"]["topic_status"] == "unformed"
    assert second.metadata["routing"]["parent_message_id"] == ""
    assert "a" not in second.edge_kinds
    assert runtime.last_bot_node is None
    assert plugin.context.sent_messages == []
    await plugin.terminate()
