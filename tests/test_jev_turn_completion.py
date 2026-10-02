"""Jev owns utterance completion; retain fragments without fixed long waits."""
import asyncio
import json
from pathlib import Path

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.jev_decision import decision_from_answers
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import JevDouble, answers
from .test_persona_model import BridgeDouble, drain
from .test_plugin_lifecycle import At, MockEvent, _plugin


@pytest.fixture
def completion_plugin(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    p = _plugin({"daily_rhythm_enabled": False, "base_thinking_delay": 8,
                 "debounce_base_cooldown": 3.5, "debounce_extended_cooldown": 1,
                 "debounce_max_cap": 4, "topic_reranker_enabled": False})
    p.time_service = VirtualClock(100)
    p.persona_engine.bridge = BridgeDouble()
    p.jev = JevDouble(answers())
    return p


async def tick(p, seconds=0.25):
    await p.time_service.advance(seconds)
    await drain(p)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["你能解释这个报错吗", "你能解释这个报错吗，", "晚上好"])
async def test_complete_utterance_enters_reply_after_short_window_without_an_extra_pause(completion_plugin, text):
    p = completion_plugin
    event = MockEvent(text, message_id="m", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await tick(p, 0.24)
        assert not p.jev.calls and not event.replies_sent
        await tick(p, 0.02)
        assert len(p.jev.calls) == len(event.replies_sent) == 1
        assert "completion" in p.jev.calls[0]["questions"]
        assert len(p.persona_engine.bridge.requests) == 1
        assert p.time_service.time() == pytest.approx(100.26)
        assert not p.debounce.is_active(event.unified_msg_origin, event.sender_id)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_unfinished_utterance_retains_fragments_until_jev_says_complete(completion_plugin):
    p = completion_plugin
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": 0.9})
    first = MockEvent("先等我说完，我遇到的问题是", message_id="first", is_at_or_wake_command=True)
    second = MockEvent("运行后提示权限不足，应该怎么解决", message_id="second")
    try:
        await p.on_group_message(first)
        await tick(p)
        assert len(p.jev.calls) == 1 and not first.replies_sent
        assert not p.persona_engine.bridge.requests
        assert p.debounce.get_pending_count() == 1
        await p.time_service.advance(0.1)
        p.jev.payload = answers()
        await p.on_group_message(second)
        await tick(p)
        conversation = p.jev.calls[-1]["state"]["conversation"]
        assert conversation["text"] == first.message_str + "\n" + second.message_str
        assert [m["message_id"] for m in conversation["messages"]] == ["first", "second"]
        assert len(p.jev.calls) == len(p.persona_engine.bridge.requests) + 1 == 2
        assert len(second.replies_sent) == 1 and not first.replies_sent
        runtime = p._sessions[first.unified_msg_origin]
        assert set(runtime.dag.nodes) >= {"first", "second"}
        assert not p.debounce.is_active(first.unified_msg_origin, first.sender_id)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_new_fragment_invalidates_an_inflight_answer_without_losing_its_context(completion_plugin):
    p = completion_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    base = p.jev.evaluate
    async def delayed(**kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await base(**kwargs)
    p.jev.evaluate = delayed
    first = MockEvent("我有两个问题", message_id="first", is_at_or_wake_command=True)
    second = MockEvent("先解释权限，再解释路径", message_id="second")
    try:
        await p.on_group_message(first)
        await p.time_service.advance(0.25)
        await asyncio.wait_for(entered.wait(), 1)
        await p.on_group_message(second)
        release.set()
        await drain(p)
        assert not first.replies_sent and not p.persona_engine.bridge.requests
        await tick(p)
        assert len(second.replies_sent) == len(p.persona_engine.bridge.requests) == 1
        assert p.jev.calls[-1]["state"]["conversation"]["text"] == first.message_str + "\n" + second.message_str
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stop", "reset", "unload"])
async def test_unfinished_wait_is_cancelled_by_lifecycle_changes(completion_plugin, ending):
    p = completion_plugin
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": 0.9})
    event = MockEvent("我还没说完", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await tick(p)
        assert p.debounce.get_pending_count() == 1
        if ending == "stop":
            stop = MockEvent("/dynamics_stop", is_admin_user=False)
            await p.cmd_dynamics_stop(stop)
        elif ending == "reset":
            await p._reset_session_state_async(event.unified_msg_origin)
        else:
            await p.terminate()
        p.jev.payload = answers()
        await tick(p, 5)
        assert not event.replies_sent and not p.persona_engine.bridge.requests
        assert p.debounce.get_pending_count() == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_wait_has_a_total_deadline_and_does_not_repeatedly_call_jev_after_expiry(completion_plugin):
    p = completion_plugin
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": 0.9})
    event = MockEvent("先等等，我还有后半句", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        for step in (0.25, 1, 1, 1, 0.75):
            await tick(p, step)
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.model_diagnostic["reason_code"] == "jev_incomplete_expired"
        assert not p.debounce.is_active(event.unified_msg_origin, event.sender_id)
        count = len(p.jev.calls)
        await tick(p, 10)
        assert len(p.jev.calls) == count and count <= 5
        assert not event.replies_sent and not p.persona_engine.bridge.requests
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_real_at_skips_the_window_and_jev_even_with_slow_old_settings(completion_plugin):
    p = completion_plugin
    p.jev.delay = 10
    event = MockEvent("帮我看看", components=[At("bot_42")])
    try:
        await p.on_group_message(event)
        await tick(p, 0)
        assert len(event.replies_sent) == 1 and not p.jev.calls
        assert p.time_service.time() == 100
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_other_speakers_have_independent_unfinished_windows(completion_plugin):
    p = completion_plugin
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": 0.9})
    first = MockEvent("我还没说完", sender_id="a", message_id="a", is_at_or_wake_command=True)
    other = MockEvent("小助手，这题怎么做", sender_id="b", message_id="b", is_at_or_wake_command=True)
    try:
        await p.on_group_message(first)
        await tick(p)
        p.jev.payload = answers()
        await p.on_group_message(other)
        await tick(p)
        assert other.replies_sent and not first.replies_sent
        assert p.debounce.get_pending_count(user_id="a") == 1
        assert p.jev.calls[-1]["state"]["conversation"]["text"] == other.message_str
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_an_old_result_cannot_restore_fragments_after_member_discard():
    clock, flushed = VirtualClock(100), []
    buffer = DebounceBuffer(time_service=clock, semantic=True)
    async def record(result):
        flushed.append(result)
    try:
        await buffer.ingest("room", "a", "还没说完", object(), record)
        await clock.advance(0.25)
        assert len(flushed) == 1
        assert await buffer.discard("room", user_id="a") == 1
        assert not await buffer.defer_result(flushed[0])
        assert not buffer.is_active("room", "a")
    finally:
        await buffer.close(flush=False)


def test_short_defaults_match_the_published_schema():
    cfg, warnings = parse_runtime_config({})
    assert not warnings
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    for key, value in {"debounce_base_cooldown": 0.25, "debounce_extended_cooldown": 1,
                       "debounce_max_cap": 4, "base_thinking_delay": 0}.items():
        assert getattr(cfg, key) == value == schema[key]["default"]


def test_old_default_windows_upgrade_without_rewriting_custom_windows():
    cfg, warnings = parse_runtime_config({"debounce_base_cooldown": 3.5,
        "debounce_extended_cooldown": 6.5, "debounce_max_cap": 12})
    assert not warnings
    assert (cfg.debounce_base_cooldown, cfg.debounce_extended_cooldown, cfg.debounce_max_cap) == (0.25, 1, 4)
    custom, warnings = parse_runtime_config({"debounce_base_cooldown": 3.5,
        "debounce_extended_cooldown": 1.5, "debounce_max_cap": 5})
    assert not warnings
    assert (custom.debounce_base_cooldown, custom.debounce_extended_cooldown, custom.debounce_max_cap) == (3.5, 1.5, 5)


@pytest.mark.parametrize("choice,confidence,wait", [("wait", 0.9, True), ("wait", 0.2, False),
    ("complete", 0.9, False), ("unexpected", 0.9, False), ("wait", float("nan"), False)])
def test_only_a_valid_confident_completion_answer_can_extend_wait(choice, confidence, wait):
    turn = TurnContext("room", "a", "你好", (MessageSnapshot("m", "a", ""),), (), 0, 0, 0, False)
    decision = decision_from_answers(turn, answers(completion={"type":"choice", "choice":choice, "confidence":confidence}))
    assert (decision.reason_code == "jev_waiting_for_completion") is wait


@pytest.mark.asyncio
async def test_shadow_wait_records_observation_without_a_reply_outcome(completion_plugin):
    p = completion_plugin
    await p.save_config_values({"shadow_mode": True})
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": 0.9})
    event = MockEvent("我还没说完", message_id="m", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await tick(p)
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.dag.get_node("m").metadata["outcome"]["final_outcome"] == "not_attempted"
        assert p._shadow_decisions[-1]["reason"] == "jev_waiting_for_completion"
        assert not event.replies_sent and not p.persona_engine.bridge.requests
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_rechecking_anonymous_fragments_preserves_their_node_identity(completion_plugin):
    p = completion_plugin
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": 0.9})
    event = MockEvent("先等等，我要说明问题", message_id="", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await tick(p)
        first_id = p.jev.calls[0]["state"]["conversation"]["messages"][0]["message_id"]
        await tick(p, 1)
        second_id = p.jev.calls[-1]["state"]["conversation"]["messages"][0]["message_id"]
        assert first_id == second_id
        assert len(p._sessions[event.unified_msg_origin].dag.nodes) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_slow_topic_batch_does_not_delay_jev_or_the_reply(completion_plugin):
    p = completion_plugin
    await p.save_config_values({"topic_reranker_enabled": True, "presence_knob": "lively"})
    p.jev.payload = answers(reason="open_group_topic")
    entered, release = asyncio.Event(), asyncio.Event()
    async def batch(*args, **kwargs):
        entered.set()
        await release.wait()
    p.topic_batches.process = batch
    event = MockEvent("最近显卡风扇噪声太大，大家有什么办法", message_id="m")
    try:
        await p.on_group_message(event)
        await tick(p)
        assert len(p.jev.calls) == len(event.replies_sent) == 1
        assert not entered.is_set()
        await p.time_service.advance(30)
        await asyncio.wait_for(entered.wait(), 1)
        followup = MockEvent("你能具体解释一下调整风扇的办法吗", message_id="next", is_at_or_wake_command=True)
        await p.on_group_message(followup)
        await tick(p)
        assert len(p.jev.calls) == 2 and len(followup.replies_sent) == 1
        assert not release.is_set()
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_a_failed_callback_does_not_leave_a_pending_utterance_forever():
    clock = VirtualClock(100)
    buffer = DebounceBuffer(time_service=clock, semantic=True)
    async def failed(result):
        raise RuntimeError("failed")
    try:
        await buffer.ingest("room", "a", "你好", object(), failed)
        await clock.advance(0.25)
        assert not buffer.is_active("room", "a")
        assert buffer.get_pending_count() == 0
    finally:
        await buffer.close(flush=False)
