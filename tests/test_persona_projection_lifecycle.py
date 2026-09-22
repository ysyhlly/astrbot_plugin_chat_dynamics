"""Projection latency must not extend session ownership or block reset."""
import asyncio
from types import SimpleNamespace

import pytest
from .test_persona_model import model_plugin, flush, drain  # noqa: F401
from .test_plugin_lifecycle import MockEvent

from astrbot_plugin_chat_dynamics.core.persona_engine import PersonaEngine
from astrbot_plugin_chat_dynamics.core.provider_budget import ProviderBudget


def test_projection_is_background_coalesced_budgeted_and_cancellable():
    async def run():
        started = asyncio.Event()
        hold = asyncio.Event()
        tasks = set()
        budget = ProviderBudget()
        data = {}

        async def generate(**kwargs):
            started.set()
            await hold.wait()
            return SimpleNamespace(completion_text='{"humour": 3}')

        async def get(key, default):
            return data.get(key, default)

        async def put(key, value):
            data[key] = value

        def spawn(coro):
            task = asyncio.create_task(coro)
            tasks.add(task)
            return task

        plugin = SimpleNamespace(context=SimpleNamespace(llm_generate=generate),
            llm=SimpleNamespace(provider_budget=budget), _shutting_down=False,
            _runtime_config=SimpleNamespace(decision_backend="model", decision_provider_id="provider", decision_timeout=10),
            get_kv_data=get, put_kv_data=put, _create_background_task=spawn)
        engine = PersonaEngine(plugin)
        persona = SimpleNamespace(fingerprint="fp", persona_id="p", prompt="card")
        turn = SimpleNamespace(session_key="s")
        state_lock = asyncio.Lock()
        async with state_lock:
            assert await engine._project_persona(persona, turn) is None
            assert await engine._project_persona(persona, turn) is None
        await asyncio.wait_for(started.wait(), 1)
        assert len(tasks) == 1
        assert engine.slots._value == 4
        assert budget.active["provider"] == (1, 1)
        # Reset can acquire the session lock while the projection is suspended.
        await asyncio.wait_for(state_lock.acquire(), 1)
        state_lock.release()
        plugin._shutting_down = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert not engine._projection_tasks
        assert not budget.active and not budget.waiters
        assert not data

    asyncio.run(run())


def test_typed_backends_only_warm_existing_cache():
    async def run(backend):
        calls = []
        tasks = []

        async def get(*args):
            return {"fp": {"axes": {"humour": 1}}}

        async def generate(**kwargs):
            calls.append(kwargs)
            raise AssertionError("typed backend must not call teacher")

        def spawn(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        plugin = SimpleNamespace(context=SimpleNamespace(llm_generate=generate),
            _runtime_config=SimpleNamespace(decision_backend=backend, decision_timeout=1),
            get_kv_data=get, _create_background_task=spawn)
        engine = PersonaEngine(plugin)
        persona = SimpleNamespace(fingerprint="fp", persona_id="p", prompt="card")
        turn = SimpleNamespace(session_key="s")
        assert await engine._project_persona(persona, turn) is None
        await asyncio.gather(*tasks)
        assert await engine._project_persona(persona, turn) == {"humour": 1}
        assert not calls

    for backend in ("jev", "laya"):
        asyncio.run(run(backend))


# Reuse the real plugin lifecycle fixture, with only host services doubled.


@pytest.mark.asyncio
async def test_slow_projection_does_not_delay_reply_reset_or_unload(model_plugin):  # noqa: F811
    plugin, bridge = model_plugin
    original_generate = plugin.context.llm_generate
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def generate(**kwargs):
        if kwargs.get("prompt", "").startswith("Character card:"):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original_generate(**kwargs)

    plugin.context.llm_generate = generate
    event = MockEvent("帮我回答", message_id="projection", is_at_or_wake_command=True)
    await plugin.on_group_message(event)
    await flush(plugin, event)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(drain(plugin), 1)
    assert event.replies_sent
    await asyncio.wait_for(plugin._reset_session_state_async(event.unified_msg_origin), 1)
    # A projection belongs to the shared persona cache, so session reset need not
    # cancel it. Plugin unload must cancel and drain it.
    await asyncio.wait_for(plugin.terminate(), 1)
    assert cancelled.is_set()
    assert not plugin.persona_engine._projection_tasks


@pytest.mark.parametrize("transition", ["jev", "laya", "shutdown"])
@pytest.mark.asyncio
async def test_projection_rechecks_backend_after_budget_admission(transition):
    budget = ProviderBudget(capacity=2)
    tasks = []
    calls = []
    config = SimpleNamespace(decision_backend="model", decision_provider_id="provider", decision_timeout=5)

    async def get(*args):
        return {}

    async def put(*args):
        raise AssertionError("obsolete projection must not write cache")

    async def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text='{"humour": 3}')

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    plugin = SimpleNamespace(context=SimpleNamespace(llm_generate=generate),
        llm=SimpleNamespace(provider_budget=budget), _runtime_config=config,
        _shutting_down=False, get_kv_data=get, put_kv_data=put, _create_background_task=spawn)
    engine = PersonaEngine(plugin)
    async with budget.slot("provider", "persona_projection"):
        await engine._project_persona(SimpleNamespace(fingerprint="fp", persona_id="p", prompt="card"),
                                      SimpleNamespace(session_key="s"))
        for _ in range(20):
            if budget.waiters:
                break
            await asyncio.sleep(0)
        assert budget.waiters
        if transition == "shutdown":
            plugin._shutting_down = True
        else:
            config.decision_backend = transition
    await asyncio.gather(*tasks)
    assert not calls
    assert not budget.active and not budget.waiters
