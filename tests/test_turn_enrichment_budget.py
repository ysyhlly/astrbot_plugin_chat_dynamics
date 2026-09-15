"""Real turn preparation and async barriers exercise the shared enrichment budget."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceResult
from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
from astrbot_plugin_chat_dynamics.core.turn_latency import start_turn
from tests.test_plugin_lifecycle import _plugin, MockEvent


def prepared(plugin, *, explicit=False):
    event = MockEvent("ordinary unrelated conversation", is_at_or_wake_command=explicit)
    result = DebounceResult(event.unified_msg_origin, event.sender_id, event.message_str, [], [event])
    turn = plugin._prepare_turn_locked(result)
    assert turn is not None
    start_turn(turn.node)
    return turn


@pytest.mark.asyncio
async def test_explicit_turn_does_not_wait_for_optional_models(monkeypatch):
    plugin = _plugin()
    turn = prepared(plugin, explicit=True)
    turn.neural_ready = asyncio.Event()
    monkeypatch.setattr(plugin, "_topic_reranker", lambda: pytest.fail("explicit turn waited for reranker"))
    await asyncio.wait_for(plugin._enrich_turn(turn), timeout=.1)
    assert not turn.neural_ready.is_set()


@pytest.mark.asyncio
async def test_embedding_exhaustion_does_not_grant_fresh_reranker_budget(monkeypatch):
    plugin = _plugin()
    plugin._runtime_config = replace(plugin._runtime_config, routing_neural_timeout=.02,
                                    topic_reranker_timeout=.02, topic_reranker_enabled=True)
    turn = prepared(plugin)
    turn.neural_ready = asyncio.Event()
    monkeypatch.setattr(plugin, "_topic_reranker", lambda: object())
    async def forbidden(*args, **kwargs):
        pytest.fail("embedding consumed the shared deadline")
    monkeypatch.setattr(plugin.thread_router, "rerank_pending", forbidden)
    await asyncio.wait_for(plugin._enrich_turn(turn), .5)
    assert {"embedding_timeout", "enrichment_budget_exhausted"} <= set(turn.node.metadata["turn_latency"]["degraded"])


@pytest.mark.asyncio
async def test_cancel_resistant_reranker_cannot_hold_turn_or_commit_late(monkeypatch):
    plugin = _plugin()
    plugin._runtime_config = replace(plugin._runtime_config, routing_neural_timeout=.03,
                                    topic_reranker_timeout=.03, topic_reranker_enabled=True)
    turn = prepared(plugin)
    turn.neural_ready = None
    # The loop's timeout is authoritative even when the diagnostic clock lags
    # behind it (Windows timers can fire before perf_counter reaches deadline).
    from astrbot_plugin_chat_dynamics.core import turn_pipeline
    monkeypatch.setattr(turn_pipeline, "time", SimpleNamespace(perf_counter=lambda: 1000.0))
    entered, cancelled, release, finished = (asyncio.Event() for _ in range(4))
    committed = []
    monkeypatch.setattr(plugin, "_topic_reranker", lambda: object())
    async def stubborn(runtime, node, reranker, *, is_current):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        if is_current():
            committed.append(node.msg_id)
        finished.set()
    monkeypatch.setattr(plugin.thread_router, "rerank_pending", stubborn)
    task = asyncio.create_task(plugin._enrich_turn(turn))
    try:
        await asyncio.wait_for(entered.wait(), .5)
        await asyncio.wait_for(task, .5)
        await asyncio.wait_for(cancelled.wait(), .5)
        assert not finished.is_set()
        assert "enrichment_budget_exhausted" in turn.node.metadata["turn_latency"]["degraded"]
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), .5)
    assert not committed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["config", "reset", "user_stop", "node"])
async def test_invalidation_during_reranking_rejects_commit(change, monkeypatch):
    plugin = _plugin()
    turn = prepared(plugin)
    turn.neural_ready = None
    entered, release = asyncio.Event(), asyncio.Event()
    committed = []
    monkeypatch.setattr(plugin, "_topic_reranker", lambda: object())
    async def rerank(runtime, node, reranker, *, is_current):
        entered.set()
        await release.wait()
        if is_current():
            committed.append(node.msg_id)
    monkeypatch.setattr(plugin.thread_router, "rerank_pending", rerank)
    task = asyncio.create_task(plugin._enrich_turn(turn))
    await asyncio.wait_for(entered.wait(), .5)
    if change == "config":
        plugin._runtime_config = replace(plugin._runtime_config, routing_neural_timeout=7.0)
    elif change == "reset":
        plugin._reset_session_state(turn.result.session_id)
    elif change == "user_stop":
        turn.runtime.user_revisions[turn.result.user_id] = turn.owner_revision + 1
    else:
        node = turn.node
        turn.dag.nodes[node.msg_id] = ConversationNode(node.msg_id, node.user_id, node.text, node.timestamp)
    release.set()
    await asyncio.wait_for(task, .5)
    assert not committed


@pytest.mark.asyncio
async def test_silent_finish_commits_participation_and_freezes_decision(monkeypatch):
    plugin = _plugin()
    monkeypatch.setattr(plugin, "_persona_mode", lambda: False)
    turn = prepared(plugin)
    calls = []
    original = turn.runtime.commit_participation
    def commit(node, level, now):
        calls.append((node.msg_id, level))
        return original(node, level, now)
    monkeypatch.setattr(turn.runtime, "commit_participation", commit)
    result = plugin._finish_turn_locked(turn)
    assert result is None
    assert calls and calls[0][0] == turn.node.msg_id
    assert turn.node.msg_id in turn.dag.nodes
    assert turn.node.metadata["routing"]
    trace = copy.deepcopy(turn.node.metadata["decision_trace"])
    assert trace["participation"]["should_reply"] is False
    assert trace["decision_finalized"] is True
    turn.node.metadata["routing"]["topic_id"] = "late-topic"
    turn.runtime.last_interlocutor = "late-user"
    assert turn.node.metadata["decision_trace"] == trace
