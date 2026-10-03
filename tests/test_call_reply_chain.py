"""Call admission, member cancellation and poke deadlines through the owned pipeline."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from astrbot_plugin_chat_dynamics.core.agent_bridge import AgentOutput
from astrbot_plugin_chat_dynamics.core.persona_engine import turn_is_continuation
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent
from .test_poke import Poke
from .test_dialogue_context import route

async def wait_until(predicate):
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(.001)
    raise AssertionError("probe setup never reached required state")

@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_full_queue_continuation_still_gets_a_decision(jev_plugin, monkeypatch, available):
    p, bridge = jev_plugin
    if not available:
        p.jev.payload = None
    event = MockEvent("修改后仍然报错，请继续排查", message_id="continuation")
    runtime = p._get_or_create_runtime(event.unified_msg_origin, group_id=event.group_id,
                                       umo=event.unified_msg_origin, bot_id="bot_42")
    now = p.time_service.time()
    human = route(runtime.dag.add_message("original", event.sender_id, "修复插件", timestamp=now - 200))
    bot = route(runtime.dag.add_message("delivered", "bot_42", "先修改配置", timestamp=now - 199,
        reply_to_id=human.msg_id, metadata={"dialogue_delivered": True, "trigger_user_id": event.sender_id,
                                          "dialogue_state": "focused", "dialogue_stop_revision": 0}))
    runtime.last_bot_node, runtime.last_model_send, runtime.last_interlocutor = bot, bot.timestamp, event.sender_id
    monkeypatch.setattr(p, "_route_message", lambda rt, node: route(node))
    for _ in range(9):
        await runtime.model_admission.acquire()
    task = None
    try:
        await p.on_group_message(event)
        task = asyncio.create_task(flush(p, event))
        await wait_until(lambda: bool(runtime.model_waiting))
        waiting = next(iter(runtime.model_waiting.values()))
        assert waiting.context.wake_kind == "none"
        assert turn_is_continuation(runtime, waiting.context, now)
        runtime.model_admission.release()
        await asyncio.wait_for(task, 2)
        await drain(p)
        assert p.jev.calls
        assert bool(bridge.requests) is available
        assert bool(event.replies_sent) is available
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await p.terminate()

@pytest.mark.asyncio
async def test_member_stop_also_blocks_poke_reply(jev_plugin):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_reply(*args, **kwargs):
        entered.set()
        await release.wait()
        return AgentOutput("这是停止之后仍发出的戳一戳回复", "conv", "[]", [])

    bridge.generate = slow_reply
    p.poke_policy = SimpleNamespace(decide=lambda **kwargs: SimpleNamespace(speak=True, poke_back=False))
    event = MockEvent("", message_id="poke", components=[Poke("bot_42")])
    task = None
    try:
        task = asyncio.create_task(p.on_group_message(event))
        await asyncio.wait_for(entered.wait(), 2)
        await p.cmd_dynamics_stop(MockEvent("/dynamics_stop", group_id=event.group_id,
                                           sender_id=event.sender_id, message_id="stop", is_admin_user=False))
        release.set()
        await asyncio.wait_for(task, 2)
        await drain(p)
        assert not event.replies_sent
    finally:
        release.set()
        if task:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        await p.terminate()

@pytest.mark.asyncio
async def test_member_stop_cancels_remaining_agent_work(jev_plugin):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def old_agent_work():
        entered.set()
        await release.wait()
        effects.append("tool executed after stop")

    bridge.before_reply = old_agent_work
    event = MockEvent("处理这个任务", message_id="active", components=[At("bot_42")])
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await asyncio.wait_for(entered.wait(), 2)
        await p.cmd_dynamics_stop(MockEvent("/dynamics_stop", group_id=event.group_id,
                                           sender_id=event.sender_id, message_id="stop", is_admin_user=False))
        release.set()
        await drain(p)
        assert not event.replies_sent
        assert not effects
        from astrbot_plugin_chat_dynamics.core.outcome_recorder import read_outcome
        runtime = p._sessions[event.unified_msg_origin]
        outcome = read_outcome(runtime.dag.get_node("active"))
        assert outcome["suppression_reason"] == "member_stopped"
        assert not runtime.owned_turn_tasks and runtime.model_admission._value == 9
    finally:
        release.set()
        await p.terminate()

@pytest.mark.asyncio
async def test_reply_calls_across_groups_obey_shared_provider_capacity(jev_plugin):
    p, bridge = jev_plugin
    release = asyncio.Event()

    async def slow_reply():
        await release.wait()

    bridge.before_reply = slow_reply
    try:
        capacity = p.llm.provider_budget.capacity
        for index in range(capacity + 2):
            event = MockEvent("处理任务", group_id=f"budget-room-{index}", message_id=f"budget-{index}",
                              components=[At("bot_42")])
            await p.on_group_message(event)
            await flush(p, event)
        await wait_until(lambda: p.llm.provider_budget.diagnostics()["queued"] == 2)
        assert p.llm.provider_budget.diagnostics()["active"] == capacity
        assert len(bridge.requests) == capacity
        release.set()
        await drain(p)
        assert len(bridge.requests) == capacity + 2
        assert p.llm.provider_budget.diagnostics()["active"] == 0
        assert p.llm.provider_budget.diagnostics()["stages"]["reply"]["generation"]["calls"] == capacity + 2
    finally:
        release.set()
        await p.terminate()

@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["lookup", "host_lock", "provider_queue", "generation"])
async def test_poke_call_respects_the_reply_timeout(jev_plugin, stage):
    from contextlib import AsyncExitStack, asynccontextmanager

    p, bridge = jev_plugin
    p._runtime_config = replace(p._runtime_config, tool_agent_timeout=1, reply_timeout=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hanging_reply(*args, **kwargs):
        entered.set()
        await release.wait()
        return AgentOutput("戳一戳回复", "conv", "[]", [])

    async def lookup(*args):
        return "provider"

    @asynccontextmanager
    async def locked(*args):
        entered.set()
        await release.wait()
        yield

    p.llm.resolve_provider_id = hanging_reply if stage == "lookup" else lookup
    if stage == "host_lock":
        bridge.session_lock = locked
    bridge.generate = hanging_reply
    p.poke_policy = SimpleNamespace(decide=lambda **kwargs: SimpleNamespace(speak=True, poke_back=False))
    event = MockEvent("", message_id="hanging-poke", components=[Poke("bot_42")])
    task = None
    held = AsyncExitStack()
    try:
        if stage == "provider_queue":
            for _ in range(p.llm.provider_budget.capacity):
                await held.enter_async_context(p.llm.provider_budget.slot("provider", "reply"))
        task = asyncio.create_task(p.on_group_message(event))
        if stage == "provider_queue":
            await wait_until(lambda: p.llm.provider_budget.diagnostics()["queued"] == 1)
        else:
            await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(1.2)
        assert task.done(), "poke generation should stop when the configured deadline expires"
        await task
        runtime = p._sessions[event.unified_msg_origin]
        assert not event.replies_sent and not runtime.owned_turn_tasks
        assert runtime.model_diagnostic["reason_code"] == "poke_reply_timeout"
        assert (runtime.session_key, "hanging-poke") not in p._poke_replied_ids
        assert p.llm.provider_budget.diagnostics()["queued"] == 0
    finally:
        release.set()
        await held.aclose()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await p.terminate()
@pytest.mark.asyncio
async def test_poke_stop_only_cancels_its_member_and_returns_budget(jev_plugin):
    p, bridge = jev_plugin
    release = asyncio.Event()
    entered = []

    async def reply(event, *args, **kwargs):
        entered.append(event.sender_id)
        await release.wait()
        return AgentOutput("戳一戳回复", "conv", "[]", [])

    bridge.generate = reply
    p.poke_policy = SimpleNamespace(decide=lambda **kwargs: SimpleNamespace(speak=True, poke_back=False))
    alice = MockEvent("", sender_id="alice", message_id="poke-alice", components=[Poke("bot_42")])
    bob = MockEvent("", sender_id="bob", message_id="poke-bob", components=[Poke("bot_42")])
    tasks = [asyncio.create_task(p.on_group_message(e)) for e in (alice, bob)]
    try:
        await wait_until(lambda: len(entered) == 2)
        runtime = p._sessions[alice.unified_msg_origin]
        await p.cmd_dynamics_stop(MockEvent("/dynamics_stop", sender_id="alice", message_id="stop"))
        await wait_until(lambda: len(runtime.owned_turn_tasks) == 1)
        assert set(runtime.owned_turn_tasks.values()) == {"bob"}
        assert p.llm.provider_budget.diagnostics()["active"] == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert not alice.replies_sent and bob.replies_sent == ["戳一戳回复"]
        assert not runtime.owned_turn_tasks
        assert p.llm.provider_budget.diagnostics()["active"] == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await p.terminate()


def test_fixed_history_boundary_keeps_reasoning_and_tool_media():
    from astrbot_plugin_chat_dynamics.core.agent_bridge import history_with_delivered_reply

    call_reasoning = {"type": "think", "think": "", "encrypted": "native-reasoning"}
    final_reasoning = {"type": "think", "think": "整理", "encrypted": None}
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,offline"}}
    history = [
        {"role": "user", "content": "旧请求"},
        {"role": "assistant", "content": "旧的已送达回复"},
        {"role": "user", "content": "本轮请求"},
        {"role": "assistant", "content": [call_reasoning, {"type": "text", "text": "工具草稿"}],
         "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "图片生成结果"},
        {"role": "user", "content": [{"type": "text", "text": "工具图片"}, image]},
        {"role": "assistant", "content": [final_reasoning, {"type": "text", "text": "未发送尾段"}]},
    ]
    saved = history_with_delivered_reply(history, "实际送达", turn_start=3)
    assert saved[:3] == history[:3]
    assert saved[3]["content"] == [call_reasoning]
    assert saved[3]["tool_calls"] == [{"id": "c1"}]
    assert saved[4:6] == history[4:6]
    assert saved[-1]["content"] == [final_reasoning, {"type": "text", "text": "实际送达"}]
    assert "工具草稿" not in str(saved) and "未发送尾段" not in str(saved)
    assert "工具草稿" in str(history), "History cleanup must not mutate the runner output"


def test_same_caption_keeps_distinct_media_chains():
    from astrbot_plugin_chat_dynamics.core.persona_engine import delivery_fragments
    from astrbot_plugin_chat_dynamics.core.platform_bridge import build_plain_chain

    first, second = build_plain_chain("图片"), build_plain_chain("图片")
    first.chain.append(SimpleNamespace(file="first.png"))
    second.chain.append(SimpleNamespace(file="second.png"))
    pacer = SimpleNamespace(persona_fragments=lambda text: [text])
    assert delivery_fragments([first, second], "图片", pacer) == [first, second]
