"""Behavior contracts for the opt-in persona pipeline (explicit SDK doubles)."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AgentOutput, AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.persona_engine import delivery_fragments
from astrbot_plugin_chat_dynamics.core.dashboard import snapshot_overview
from astrbot_plugin_chat_dynamics.core.pacer import PacingShaper
from astrbot_plugin_chat_dynamics.core.reactions import ReactionPolicy
from astrbot_plugin_chat_dynamics.core.semantics import classify_message, semantic_match
from astrbot_plugin_chat_dynamics.core.style_shaper import StyleShaper
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, PersonaSnapshot, TurnContext, TurnDecision
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from .test_plugin_lifecycle import MockEvent, _plugin


class BridgeDouble:
    def __init__(self):
        self.persona = PersonaSnapshot("v1", "conv", "quiet", "克制、简短，但认真回应求助")
        self.requests = []
        self.commits = []
        self.response = "收到，我看看。"
        self.before_reply = None

    def check(self):
        return True

    async def snapshot(self, event):
        return self.persona

    async def current(self, event, persona):
        return persona == self.persona

    @asynccontextmanager
    async def session_lock(self, umo):
        yield

    async def generate(self, event, events, prompt, persona, provider, **kwargs):
        self.requests.append((json.loads(prompt), persona, events))
        if self.before_reply:
            await self.before_reply()
        return AgentOutput(self.response, "conv", "[]", [])

    async def commit(self, event, output, delivered):
        self.commits.append(delivered)
        return True


@pytest.fixture
def model_plugin(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    # daily_rhythm stays off: ambient persona contracts must not depend on the
    # wall-clock hour (night self-sleep would veto ambient replies >= 23:00).
    p = _plugin({
        "decision_mode": "persona_model",
        "base_thinking_delay": 0,
        "debounce_base_cooldown": 10,
        "daily_rhythm_enabled": False,
    })
    bridge = BridgeDouble()
    p.persona_engine.bridge = bridge
    p.decision_calls = []

    async def decide(**kwargs):
        p.decision_calls.append(kwargs)
        payload = json.loads(kwargs["prompt"])
        ids = [m["message_id"] for m in payload["conversation"]["messages"]]
        return SimpleNamespace(completion_text=json.dumps({
            "action": "reply", "state": "focused", "target_message_ids": ids,
            "response_goal": "回应完整请求", "length": "brief", "reason_code": "relevant_request",
        }))

    p.context.llm_generate = decide
    return p, bridge


async def flush(p, event):
    await p.debounce.flush(session_id=event.unified_msg_origin)


async def drain(p):
    for _ in range(50):
        await asyncio.sleep(0.01)
        tasks = [r.generation_task for r in p._sessions.values() if r.generation_task and not r.generation_task.done()]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
        if not any(
            (r.generation_task and not r.generation_task.done())
            or (hasattr(r, "model_queue") and r.model_queue)
            for r in p._sessions.values()
        ):
            break


@pytest.mark.asyncio
async def test_full_fragment_turn_persona_and_single_delivery(model_plugin):
    p, bridge = model_plugin
    texts = ["帮我写一封邮件", "委婉拒绝邀请", "不要超过一百字"]
    events = [MockEvent(t, message_id=str(i), is_at_or_wake_command=(i == 0)) for i, t in enumerate(texts)]
    for event in events:
        await p.on_group_message(event)
        assert event.call_llm  # forbid host default LLM; persona owns the turn
    await flush(p, events[-1])
    await drain(p)
    assert len(p.decision_calls) == len(bridge.requests) == 1
    request = bridge.requests[0][0]
    assert request["conversation"]["text"] == "\n".join(texts)
    assert request["conversation"]["background"] == []
    assert "克制" in p.decision_calls[0]["system_prompt"]
    assert bridge.requests[0][1] == bridge.persona
    assert bridge.commits == [bridge.response]
    assert len(events[-1].replies_sent) == 1 and events[-1].replies_sent[0].endswith(bridge.response)
    await p.terminate()


@pytest.mark.asyncio
async def test_distinct_users_are_fifo_not_latest_only(model_plugin):
    p, bridge = model_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()

    bridge.before_reply = slow
    a = MockEvent("帮我看第一个请求", sender_id="a", message_id="a", is_at_or_wake_command=True)
    b = MockEvent("帮我看第二个请求", sender_id="b", message_id="b", is_at_or_wake_command=True)
    await p.on_group_message(a)
    await flush(p, a)
    await entered.wait()
    await p.on_group_message(b)
    await flush(p, b)
    release.set()
    await drain(p)
    assert [r[0]["conversation"]["author"] for r in bridge.requests] == ["a", "b"]
    assert a.replies_sent and b.replies_sent
    await p.terminate()


@pytest.mark.asyncio
async def test_persona_switch_discards_draft(model_plugin):
    p, bridge = model_plugin

    async def switch():
        bridge.persona = replace(bridge.persona, fingerprint="v2", prompt="新的性格")

    bridge.before_reply = switch
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert not event.replies_sent and not bridge.commits
    await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_invalid_decision_fallback_only_for_explicit_request(model_plugin, explicit):
    p, bridge = model_plugin

    async def invalid(**kwargs):
        return SimpleNamespace(completion_text="not json")

    p.context.llm_generate = invalid
    event = MockEvent("今天晚上吃什么", is_at_or_wake_command=explicit)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert bool(bridge.requests) == explicit
    await p.terminate()


@pytest.mark.asyncio
async def test_shadow_decides_without_agent_or_state_mutation(model_plugin):
    p, bridge = model_plugin
    p.config["shadow_mode"] = True
    p._sync_runtime_from_config()
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    runtime = p._sessions[event.unified_msg_origin]
    assert not event.call_llm and p.decision_calls
    assert not bridge.requests and not bridge.commits and not event.replies_sent
    assert runtime.interaction_state == "observing"
    assert runtime.model_diagnostic["shadow"]
    assert p._shadow_decisions[-1]["action"] == "reply"
    assert p._shadow_decisions[-1]["reason"] == "relevant_request"
    assert p._shadow_decisions[-1]["state"] == "focused"
    await p.terminate()


@pytest.mark.asyncio
async def test_send_failure_never_commits_history(model_plugin):
    p, bridge = model_plugin
    event = MockEvent("帮我回答", is_at_or_wake_command=True)

    async def fail(*args, **kwargs):
        return SimpleNamespace(success=False, message_id="")

    p._send_owned = fail
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert not bridge.commits
    assert p._sessions[event.unified_msg_origin].last_bot_node is None
    await p.terminate()


@pytest.mark.asyncio
async def test_persona_gate_uses_civil_wall_time(model_plugin):
    p, bridge = model_plugin
    mono = 12_345.0
    wall = 1_741_989_600.0

    class SplitClock:
        def time(self):
            return mono

        def wall_time(self):
            return wall

        async def sleep(self, seconds):
            return None

    p.time_service = SplitClock()
    seen = {}
    orig_eval = p.decision_gate.evaluate
    orig_spoke = p.decision_gate.note_spoke

    def wrap_eval(**kwargs):
        seen["eval_now"] = kwargs.get("now")
        return orig_eval(**kwargs)

    def wrap_spoke(*args, **kwargs):
        seen["spoke_now"] = kwargs.get("now")
        return orig_spoke(*args, **kwargs)

    p.decision_gate.evaluate = wrap_eval
    p.decision_gate.note_spoke = wrap_spoke
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert seen.get("eval_now") == wall
    assert seen.get("spoke_now") == wall
    runtime = p._sessions[event.unified_msg_origin]
    assert runtime.last_model_send == mono
    await p.terminate()


@pytest.mark.asyncio
async def test_cooling_prevents_even_decision_call(model_plugin):
    p, bridge = model_plugin
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await p._cool_session_async(event.unified_msg_origin, 1)
    await flush(p, event)
    await drain(p)
    assert not p.decision_calls and not bridge.requests
    await p.terminate()


@pytest.mark.asyncio
async def test_unload_cancels_model_and_waits(model_plugin):
    p, bridge = model_plugin
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def wait(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    p.context.llm_generate = wait
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await entered.wait()
    await p.terminate()
    assert cancelled.is_set() and not bridge.requests and not p._background_tasks


def test_structured_decision_rejects_unknown_targets():
    turn = TurnContext("s", "u", "hello", (MessageSnapshot("m", "u", ""),), (), 0, 0, 0, False)
    payload = dict(action="reply", state="focused", target_message_ids=["unknown"], response_goal="test", length="brief", reason_code="test")
    with pytest.raises(ValueError, match="decision_target"):
        TurnDecision.parse(json.dumps(payload), turn)


@pytest.mark.parametrize("change", [
    {"action": "send_to_everyone"}, {"state": "invented"}, {"length": "infinite"},
    {"target_message_ids": []}, {"response_goal": "x" * 601}, {"reason_code": "详细推理"}, {"extra": True},
])
def test_invalid_decision_protocol_cannot_drive_delivery(change):
    turn = TurnContext("s", "u", "hello", (MessageSnapshot("m", "u", ""),), (), 0, 0, 0, False)
    payload = dict(action="reply", state="focused", target_message_ids=["m"], response_goal="test", length="brief", reason_code="test")
    payload.update(change)
    with pytest.raises(ValueError):
        TurnDecision.parse(json.dumps(payload), turn)
    with pytest.raises(ValueError):
        TurnDecision.parse(" " * 8193, turn)


def test_negation_and_content_protection():
    assert "positive" not in classify_message("不开心")[1]
    assert "negative" not in classify_message("不烦")[1]
    for text in ("2 * 3 * 4 = 24", "`a * b * c`", "https://example.com/a_b_c"):
        assert StyleShaper().adapt_style(text, GroupChatMode.FAST_BANTER) == text
    policy = ReactionPolicy()
    for trigger in ("不开心", "不烦", "开心但很累", "你好"):
        assert not policy.decide(
            reply_text="嗯", trigger_text=trigger, mode=GroupChatMode.FAST_BANTER, enabled=True
        ).emoji
    text = "```python\n" + "print('因此，')\n" * 200 + "```"
    assert PacingShaper.persona_fragments(text) == [text]


def test_question_words_alone_do_not_link_topics():
    assert not semantic_match("今天吃饭了吗", "今天下雨了吗").should_link()


@pytest.mark.asyncio
async def test_media_reaches_agent_without_content_guess(model_plugin):
    p, bridge = model_plugin
    image = type("Image", (), {})()
    event = MockEvent("", message_id="media", components=[image], is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert event.call_llm
    assert bridge.requests[0][2][0].message_obj.message[0] is image
    assert "[图片]" in bridge.requests[0][0]["conversation"]["text"]
    await p.terminate()


@pytest.mark.asyncio
async def test_private_window_cannot_silence_other_conversation(model_plugin):
    p, bridge = model_plugin
    p.vibe_analyzer.record_message("mock:GroupMessage:group_100", "这是秘密，别告诉别人", user_id="other")
    event = MockEvent("今晚吃什么", message_id="dinner")
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert p.decision_calls and bridge.requests
    assert not bridge.requests[0][0]["conversation"]["background"]
    await p.terminate()


@pytest.mark.asyncio
async def test_partial_send_commits_only_delivered_fragment(model_plugin):
    p, bridge = model_plugin
    bridge.response = "甲" * 1000 + "\n\n" + "乙" * 1000
    p.time_service.sleep = lambda seconds: asyncio.sleep(0)
    sent = []

    async def deliver(runtime, event, fragment, **kwargs):
        sent.append(fragment)
        return SimpleNamespace(success=len(sent) == 1, message_id="delivered")

    p._send_owned = deliver
    event = MockEvent("帮我详细解释", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert bridge.commits == ["甲" * 1000]
    runtime = p._sessions[event.unified_msg_origin]
    assert runtime.last_bot_node.text == "甲" * 1000
    await p.terminate()


def test_delivery_fragments_skip_text_that_duplicates_tool_chain():
    pacer = SimpleNamespace(persona_fragments=lambda text: [text])
    chain = SimpleNamespace(chain=["同一答案"], get_plain_text=lambda: "同一答案")
    assert delivery_fragments([chain], "同一答案", pacer) == [chain]
    assert delivery_fragments([chain], "补充说明", pacer) == [chain, "补充说明"]
    assert delivery_fragments([], "普通回复", pacer) == ["普通回复"]


@pytest.mark.asyncio
async def test_persona_does_not_resend_matching_tool_chain_and_final_text(model_plugin):
    p, bridge = model_plugin
    answer = "同一答案"
    bridge.response = answer
    chain = SimpleNamespace(chain=[answer], get_plain_text=lambda: answer)
    original = bridge.generate

    async def generate(*args, **kwargs):
        output = await original(*args, **kwargs)
        return AgentOutput(
            output.text,
            output.conversation_id,
            output.base_history,
            output.history,
            output.tool_count,
            [chain],
        )

    bridge.generate = generate
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert event.replies_sent == [answer]
    await p.terminate()


@pytest.mark.asyncio
async def test_same_user_new_input_invalidates_old_draft(model_plugin):
    p, bridge = model_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()

    bridge.before_reply = slow
    first = MockEvent("帮我写邮件", message_id="first", is_at_or_wake_command=True)
    second = MockEvent("补充：不要超过一百字", message_id="second")
    await p.on_group_message(first)
    await flush(p, first)
    await entered.wait()
    await p.on_group_message(second)
    await flush(p, second)
    release.set()
    await drain(p)
    assert not first.replies_sent
    assert second.replies_sent
    assert "帮我写邮件" in json.dumps(bridge.requests[-1][0], ensure_ascii=False)
    await p.terminate()


@pytest.mark.asyncio
async def test_queue_pressure_preserves_explicit_request_with_one_fallback(model_plugin):
    p, bridge = model_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()

    bridge.before_reply = slow
    first = MockEvent("帮我处理", sender_id="0", message_id="0", is_at_or_wake_command=True)
    await p.on_group_message(first)
    await flush(p, first)
    await entered.wait()
    runtime = p._sessions[first.unified_msg_origin]
    for i in range(1, 9):
        event = MockEvent("帮我处理", sender_id=str(i), message_id=str(i), is_at_or_wake_command=True)
        await p.on_group_message(event)
        await flush(p, event)
    last = MockEvent("帮我处理最后一个", sender_id="last", message_id="last", is_at_or_wake_command=True)
    await p.on_group_message(last)
    waiting = asyncio.create_task(flush(p, last))
    await asyncio.sleep(0)
    assert not runtime.state_lock.locked()
    assert len(runtime.model_queue) == 8
    release.set()
    await asyncio.wait_for(waiting, 2)
    await drain(p)
    assert len(bridge.requests) == 10
    assert len(p.decision_calls) == 9
    assert last.replies_sent
    await p.terminate()


@pytest.mark.asyncio
async def test_ambient_opening_budget(model_plugin):
    p, bridge = model_plugin
    for i in range(3):
        event = MockEvent("聊聊今天的趣事", sender_id=str(i), message_id=str(i))
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
    assert len(p.decision_calls) == 3 and len(bridge.requests) == 2
    assert p._sessions[event.unified_msg_origin].model_diagnostic["reason_code"] == "ambient_budget"
    await p.terminate()


@pytest.mark.asyncio
async def test_decision_timeout_has_no_retry(model_plugin):
    p, bridge = model_plugin
    p._runtime_config = replace(p._runtime_config, decision_timeout=0.01)
    calls = []

    async def slow(**kwargs):
        calls.append(kwargs)
        await asyncio.Event().wait()

    p.context.llm_generate = slow
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    # on_group_message refreshes configuration, so override after ingress.
    await p.on_group_message(event)
    p._runtime_config = replace(p._runtime_config, decision_timeout=0.01)
    await flush(p, event)
    await drain(p)
    assert len(calls) == len(bridge.requests) == 1
    assert p._sessions[event.unified_msg_origin].model_diagnostic["reason_code"] == "decision_timeout"
    await p.terminate()


@pytest.mark.asyncio
async def test_reset_while_agent_is_running_drops_queue_and_cancels(model_plugin):
    p, bridge = model_plugin
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    bridge.before_reply = blocked
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await entered.wait()
    other = MockEvent("也帮我回答", sender_id="other", message_id="other", is_at_or_wake_command=True)
    await p.on_group_message(other)
    await flush(p, other)
    await p._reset_session_state_async(event.unified_msg_origin)
    runtime = p._sessions[event.unified_msg_origin]
    assert not runtime.model_queue and not runtime.last_bot_node
    assert not event.replies_sent and not other.replies_sent and not bridge.commits
    await p.terminate()


@pytest.mark.asyncio
async def test_model_failure_is_diagnosed_and_never_retried(model_plugin):
    p, bridge = model_plugin

    async def broken():
        raise RuntimeError("offline")

    bridge.before_reply = broken
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    await flush(p, event)
    await drain(p)
    assert len(bridge.requests) == 1 and not event.replies_sent
    assert p._sessions[event.unified_msg_origin].model_diagnostic["reason_code"] == "agent_failed"
    await p.terminate()


def test_model_mode_cannot_enable_without_host_bridge(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: False)
    with pytest.raises(RuntimeError):
        _plugin({"decision_mode": "persona_model"})


def test_console_marks_inactive_legacy_options(model_plugin):
    p, _ = model_plugin
    data = snapshot_overview(p)
    assert data["decision_mode"] == "persona_model"
    assert "WTS weights" in data["inactive_options"]
    assert "vibe_llm_enabled" in data["inactive_options"]
    assert "casual_emoji_enabled" in data["inactive_options"]
    assert data["provider_resolution"]["decision"]


@pytest.mark.asyncio
async def test_owned_media_chain_preserves_components(model_plugin):
    from astrbot_plugin_chat_dynamics.core.platform_bridge import MessageChain

    p, bridge = model_plugin
    event = MockEvent("帮我回答", is_at_or_wake_command=True)
    await p.on_group_message(event)
    runtime = p._sessions[event.unified_msg_origin]
    chain = MessageChain().message("工具输出")
    result = await p._send_owned(runtime, event, chain, reply_to_id="input")
    assert result.success and event.replies_sent[0].endswith("工具输出")
    assert len(chain.chain) == 1, "Adding reply metadata must not mutate the original tool result"
    await p.terminate()


@pytest.mark.asyncio
async def test_new_mode_hooks_do_not_reshape_or_duplicate_host_results(model_plugin):
    p, bridge = model_plugin
    event = MockEvent("test")
    request = SimpleNamespace(prompt="unchanged")
    response = SimpleNamespace(completion_text="2 * 3 * 4 = 24")
    await p.on_llm_request(event, request)
    await p.on_llm_response(event, response)
    await p.on_decorating_result(event)
    await p.after_message_sent(event)
    assert request.prompt == "unchanged" and response.completion_text == "2 * 3 * 4 = 24"
    assert not event.replies_sent
    await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["at", "reply"])
async def test_persona_owned_turn_rejects_extra_host_request_and_still_replies(model_plugin, trigger):
    from .test_plugin_lifecycle import At, Reply

    p, bridge = model_plugin
    event = MockEvent("哄我睡觉就给你换床", is_at_or_wake_command=True,
                      components=[At(qq="bot_42") if trigger == "at" else Reply(id="bot-message")])
    try:
        await p.on_group_message(event)
        assert not event.is_stopped  # filter still lets other message handlers run
        event.should_call_llm(False)  # a later handler overrides the default gate
        request = SimpleNamespace(prompt="extra host reply", system_prompt="")
        await p.on_llm_request(event, request)
        assert event.is_stopped
        assert event.call_llm
        assert p._metrics["duplicate_native_request_blocked"] == 1
        await flush(p, event)
        await drain(p)
        assert len(p.decision_calls) == len(bridge.requests) == 1
        assert len(event.replies_sent) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_persona_owned_agent_request_is_allowed_through_hooks(model_plugin):
    import copy

    p, _ = model_plugin
    event = MockEvent("帮我看看", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        owned = copy.copy(event)
        owned._chat_dynamics_owned_request = True
        request = SimpleNamespace(prompt="owned reply", system_prompt="")
        await p.on_llm_request(owned, request)
        assert not owned.is_stopped
        assert p._metrics["duplicate_native_request_blocked"] == 0
    finally:
        await p.terminate()
