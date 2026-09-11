"""Exercise real turn preparation/commit with controllable model barriers."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceResult
from tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key


class Barrier:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def wait(self, *_args):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def turn(message_id, *, wake=False):
    event = MockEvent(
        'GPU cooling?' if not wake else 'Please help with GPU cooling',
        group_id='routing_concurrency', message_id=message_id,
        sender_id=message_id, is_at_or_wake_command=wake,
    )
    return DebounceResult(_session_key(event.group_id), event.sender_id,
                          event.message_str, [], [event])


def configure(monkeypatch):
    plugin = _plugin({'routing_neural_timeout': 2})
    monkeypatch.setattr(plugin, '_persona_mode', lambda: True)
    monkeypatch.setattr(plugin, '_schedule_neural_embed', lambda *_: None)
    monkeypatch.setattr(plugin, '_topic_reranker', lambda: object())
    monkeypatch.setattr(plugin.thread_router, 'rerank_pending', AsyncMock())
    monkeypatch.setattr(plugin.thread_router, 'title_topic', AsyncMock())
    # Stop at the generation boundary: these tests assert which real ingress
    # turns are allowed to request generation, without pacing/provider delays.
    monkeypatch.setattr(plugin.persona_engine, 'submit', AsyncMock())
    return plugin


async def assert_lock_available(runtime):
    await asyncio.wait_for(runtime.state_lock.acquire(), timeout=0.5)
    runtime.state_lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('slow_stage', ['rerank', 'neural'])
async def test_model_wait_releases_lock_and_explicit_wake_finishes(monkeypatch, slow_stage):
    plugin = configure(monkeypatch)
    barrier = Barrier()
    first = turn('ambient')
    if slow_stage == 'rerank':
        async def rerank(_runtime, node, _reranker):
            if node.msg_id == 'ambient':
                await barrier.wait()
        monkeypatch.setattr(plugin.thread_router, 'rerank_pending', rerank)
    else:
        monkeypatch.setattr(plugin, '_schedule_neural_embed',
                            lambda *_: SimpleNamespace(routing_ready=barrier))
    task = asyncio.create_task(plugin.on_turn_flushed(first))
    try:
        await asyncio.wait_for(barrier.entered.wait(), timeout=1)
        runtime = plugin._sessions[first.session_id]
        await assert_lock_available(runtime)
        await asyncio.wait_for(plugin.on_turn_flushed(turn('wake', wake=True)), timeout=0.5)
        assert not task.done()
        plugin.persona_engine.submit.assert_awaited_once()
        submitted = plugin.persona_engine.submit.await_args.args[1]
        assert submitted.context.explicit
        assert [message.message_id for message in submitted.context.messages] == ['wake']
        assert 'wake' in runtime.dag.nodes
        barrier.release.set()
        await asyncio.wait_for(task, timeout=1)
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await plugin.terminate()


@pytest.mark.asyncio
async def test_title_wait_is_background_and_terminate_cancels_it(monkeypatch):
    plugin = configure(monkeypatch)
    barrier = Barrier()
    monkeypatch.setattr(plugin.thread_router, 'title_topic', barrier.wait)
    try:
        first = turn('first', wake=True)
        await asyncio.wait_for(plugin.on_turn_flushed(first), timeout=0.5)
        await asyncio.wait_for(barrier.entered.wait(), timeout=1)
        await assert_lock_available(plugin._sessions[first.session_id])
        await asyncio.wait_for(plugin.on_turn_flushed(turn('second')), timeout=0.5)
        assert plugin.persona_engine.submit.await_count == 2
        assert not barrier.release.is_set()
        await asyncio.wait_for(plugin.terminate(), timeout=1)
        assert barrier.cancelled.is_set()
        assert not plugin._background_tasks
    finally:
        barrier.release.set()
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidate', ['reset', 'epoch', 'revision', 'user_revision'])
async def test_invalidated_preparation_cannot_request_generation(monkeypatch, invalidate):
    plugin = configure(monkeypatch)
    barrier = Barrier()
    monkeypatch.setattr(plugin.thread_router, 'rerank_pending', barrier.wait)
    first = turn('old')
    task = asyncio.create_task(plugin.on_turn_flushed(first))
    try:
        await asyncio.wait_for(barrier.entered.wait(), timeout=1)
        runtime = plugin._sessions[first.session_id]
        async with runtime.state_lock:
            if invalidate == 'reset':
                plugin._reset_session_state(first.session_id)
            elif invalidate == 'user_revision':
                runtime.user_revisions[first.user_id] = 1
            else:
                setattr(runtime, invalidate, getattr(runtime, invalidate) + 1)
        barrier.release.set()
        await asyncio.wait_for(task, timeout=1)
        plugin.persona_engine.submit.assert_not_awaited()
        plugin.thread_router.title_topic.assert_not_awaited()
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await plugin.terminate()


@pytest.mark.asyncio
async def test_background_rerank_does_not_start_title_after_epoch_change(monkeypatch):
    plugin = configure(monkeypatch)
    barrier = Barrier()
    monkeypatch.setattr(plugin.thread_router, 'rerank_pending', barrier.wait)
    first = turn('wake', wake=True)
    try:
        await plugin.on_turn_flushed(first)
        await asyncio.wait_for(barrier.entered.wait(), timeout=1)
        runtime = plugin._sessions[first.session_id]
        async with runtime.state_lock:
            runtime.epoch += 1
        jobs = list(plugin._background_tasks)
        barrier.release.set()
        await asyncio.gather(*jobs)
        plugin.thread_router.title_topic.assert_not_awaited()
    finally:
        barrier.release.set()
        await plugin.terminate()
