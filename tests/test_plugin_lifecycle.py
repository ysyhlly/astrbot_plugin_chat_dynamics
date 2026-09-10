"""End-to-End Integration and Lifecycle Tests for ChatDynamicsPlugin.

Validates:
1. Plugin initialization and configuration loading.
2. Event interception: verifies event.stop_event() is called to halt naive responses.
3. End-to-end state machine pipeline using official send / llm_generate shapes.
4. Energy asymmetry cut-off during live conversation flow.
5. Admin command handling (/dynamics status, /dynamics cool).
6. Commands are not swallowed; empty whitelist does not take over; loopback is ignored.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceResult
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter, LLMUnavailable
from astrbot_plugin_chat_dynamics.core.platform_bridge import chain_plain_text
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin


class At:
    def __init__(self, qq: str):
        self.qq = qq


class Reply:
    def __init__(self, id: str):
        self.id = id


class Face:
    def __init__(self, id: str = "1"):
        self.id = id


class MockContext:
    """Mock AstrBot Context using official method names."""

    def __init__(self):
        self.sent_messages: List[tuple[str, str]] = []
        self.llm_prompts: List[str] = []
        self.agent_prompts: List[str] = []
        self.web_routes: List[tuple[str, Any, Any, str]] = []

    def register_web_api(self, route, handler, methods, desc):
        self.web_routes.append((route, handler, methods, desc))

    async def send_message(self, target: Any, message: Any):
        self.sent_messages.append((str(target), chain_plain_text(message)))

    def get_using_provider(self, umo: Any = None):
        return MockLLMProvider()

    async def get_current_chat_provider_id(self, umo: Any = None):
        return "mock-provider"

    async def llm_generate(self, **kwargs):
        prompt = str(kwargs.get("prompt") or "")
        self.llm_prompts.append(prompt)
        return MockLLMProvider.make_response()

    async def tool_loop_agent(self, **kwargs):
        prompt = str(kwargs.get("prompt") or "")
        self.agent_prompts.append(prompt)
        self.llm_prompts.append(prompt)
        return MockLLMProvider.make_response()


class MockLLMProvider:
    """Mock LLM Provider returning predictable outputs."""

    @staticmethod
    def make_response():
        class Resp:
            completion_text = (
                "确实是这样，这个架构设计思路非常清晰。\n"
                "而且这个方案在很多超高并发场景下都经过了线上严苛的生产验证。\n"
                "后续如果需要扩展还可以继续拆分子服务。\n"
                "如果您还有其他问题，请随时联系我！"
            )
        return Resp()

    async def text_chat(self, prompt: str, system_prompt: str = ""):
        return self.make_response()


class MockEvent:
    """Mock AstrMessageEvent using official send / get_messages / get_self_id."""

    def __init__(
        self,
        message_str: str,
        sender_id: str = "user_1",
        group_id: str = "group_100",
        message_id: str = "msg_1",
        self_id: str = "bot_42",
        components: Optional[List[Any]] = None,
        is_at_or_wake_command: bool = False,
        is_admin_user: bool = True,
    ):
        self.message_str = message_str
        self.sender_id = sender_id
        self.group_id = group_id
        self.message_id = message_id
        self.self_id = self_id
        self.is_stopped = False
        self.call_llm = False
        self.replies_sent: List[str] = []
        self._result: Any = None
        self.is_at_or_wake_command = is_at_or_wake_command
        self._is_admin_user = is_admin_user
        self.unified_msg_origin = f"mock:GroupMessage:{group_id}"
        self.message_obj = type("MsgObj", (), {"message_id": message_id, "message": list(components or [])})()
        self._outline = ""
        if components:
            names = [type(c).__name__.lower() for c in components]
            if "face" in names:
                self._outline = "[表情]"
            elif "image" in names:
                self._outline = "[图片]"

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_group_id(self) -> str:
        return self.group_id

    def get_self_id(self) -> str:
        return self.self_id

    def get_messages(self) -> List[Any]:
        return list(self.message_obj.message)

    def get_message_outline(self) -> str:
        return self._outline or self.message_str

    def is_admin(self) -> bool:
        return self._is_admin_user

    def stop_event(self) -> None:
        self.is_stopped = True

    def continue_event(self) -> None:
        self.is_stopped = False

    def should_call_llm(self, call_llm: bool) -> None:
        self.call_llm = bool(call_llm)

    def get_result(self) -> Any:
        return self._result

    def set_result(self, result: Any) -> None:
        self._result = result

    def clear_result(self) -> None:
        self._result = None

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, chain: Any) -> Optional[str]:
        self.replies_sent.append(chain_plain_text(chain))
        return f"sent_{len(self.replies_sent)}_{self.message_id}"


def _plugin(config: Optional[dict] = None, clock: Optional[VirtualClock] = None) -> ChatDynamicsPlugin:
    ctx = MockContext()
    base = {"enable": True, "takeover_all": True, "bot_names": ["小助手"]}
    if config:
        base.update(config)
    plugin = ChatDynamicsPlugin(context=ctx, config=base)
    # Unit fixtures must not use real host KV/database storage when the SDK is installed.
    plugin._kv = {}

    async def get_kv(key, default=None):
        return plugin._kv.get(key, default)

    async def put_kv(key, value):
        plugin._kv[key] = value

    async def delete_kv(key):
        plugin._kv.pop(key, None)

    plugin.get_kv_data = get_kv
    plugin.put_kv_data = put_kv
    plugin.delete_kv_data = delete_kv
    if clock is not None:
        plugin.time_service = clock
    plugin.context = ctx
    return plugin


def test_plugin_reset_clears_decision_gate_session_state():
    plugin = _plugin()
    key = _session_key("group_reset_gate")
    now = 5_000_000.0
    plugin.decision_gate.rhythm.evaluate(
        session_id=key,
        user_id="a",
        text="晚安",
        now=now,
        telemetrics=type("T", (), {"mpm": 1.0, "scene_tags": (), "emotion_tags": ()})(),
        recent_nodes=[],
        cfg=plugin._runtime_config,
    )
    plugin.decision_gate.useful.note_proactive(key, gap_fingerprint="x", now=now)
    plugin._reset_session_state(key)
    assert plugin.decision_gate.rhythm.status(key, now=now)["state"] == "awake"
    assert plugin.decision_gate.useful.quota_status(key, now=now)["proactive_used"] == 0


def _session_key(group_id: str) -> str:
    return f"mock:GroupMessage:{group_id}"


@pytest.mark.asyncio
async def test_plugin_full_end_to_end_flow():
    """Validates complete 5-stage pipeline from incoming message event to fragmented delivery."""
    vc = VirtualClock(initial_time=5000.0)
    plugin = _plugin(
        {
            "debounce_base_cooldown": 2.0,
            "debounce_extended_cooldown": 4.0,
            "debounce_max_cap": 8.0,
            "chars_per_second": 50.0,
            "base_thinking_delay": 0.2,
        },
        clock=vc,
    )

    ev1 = MockEvent(
        "你好 小助手",
        sender_id="user_alice",
        group_id="group_test",
        message_id="m1",
        is_at_or_wake_command=True,
        components=[At(qq="bot_42")],
    )
    ev2 = MockEvent(
        "我想问一个架构设计问题",
        sender_id="user_alice",
        group_id="group_test",
        message_id="m2",
    )

    await plugin.on_group_message(ev1)
    assert ev1.is_stopped is False
    assert ev1.call_llm is True

    await vc.advance(0.5)
    await plugin.on_group_message(ev2)
    assert ev2.is_stopped is False
    assert ev2.call_llm is True

    await vc.advance(2.5)
    await vc.advance(5.0)

    assert len(ev2.replies_sent) >= 2
    full_output = "".join(ev2.replies_sent)
    assert "随时联系我" not in full_output
    assert "确实是这样" in full_output
    assert plugin.context.agent_prompts, "native tool_loop_agent was not used"

    dag = plugin.dags[_session_key("group_test")]
    assert len(dag.nodes) >= 3
    user_node = dag.get_node("m2")
    assert user_node is not None
    assert "bot_42" in user_node.mentioned_users
    first_user_node = dag.get_node("m1")
    assert first_user_node is not None
    assert "m1" in user_node.parent_ids
    bot_nodes = sorted(
        (node for node in dag.nodes.values() if node.user_id == "bot_42"),
        key=lambda node: node.timestamp,
    )
    assert bot_nodes[0].parent_ids == {"m2"}
    for previous, current in zip(bot_nodes, bot_nodes[1:]):
        assert current.parent_ids == {previous.msg_id}


@pytest.mark.asyncio
async def test_plugin_energy_asymmetry_during_interaction():
    """Verify that when a user replies with consecutive low-effort messages, bot ceases speech."""
    vc = VirtualClock(initial_time=6000.0)
    plugin = _plugin({"debounce_base_cooldown": 1.0}, clock=vc)

    ev1 = MockEvent("666", sender_id="user_bob", group_id="group_test_2", message_id="m10")
    await plugin.on_group_message(ev1)
    await vc.advance(1.5)
    await vc.advance(2.0)

    ev2 = MockEvent("哦", sender_id="user_bob", group_id="group_test_2", message_id="m11")
    await plugin.on_group_message(ev2)
    await vc.advance(1.5)
    await vc.advance(2.0)

    assert len(ev2.replies_sent) == 0


@pytest.mark.asyncio
async def test_plugin_admin_commands():
    """Verify admin command /dynamics status and /dynamics cool."""
    plugin = _plugin()

    ev_cmd_status = MockEvent("", sender_id="admin_1", group_id="group_adm", message_id="cmd_1")
    await plugin.cmd_dynamics(ev_cmd_status, action="status")
    assert len(ev_cmd_status.replies_sent) == 1
    assert "群聊动态状态监控" in ev_cmd_status.replies_sent[0]

    ev_cmd_cool = MockEvent("", sender_id="admin_1", group_id="group_adm", message_id="cmd_2")
    await plugin.cmd_dynamics(ev_cmd_cool, action="cool", param="20")
    assert len(ev_cmd_cool.replies_sent) == 1
    assert "深度冷却期" in ev_cmd_cool.replies_sent[0]
    assert plugin.arbiter.is_in_deep_cooling(_session_key("group_adm")) is True


@pytest.mark.asyncio
async def test_plugin_admin_cool_rejects_invalid_minutes():
    plugin = _plugin()
    event = MockEvent("", sender_id="admin_1", group_id="group_adm", message_id="cmd_bad")

    await plugin.cmd_dynamics(event, action="cool", param="not-a-number")

    assert event.replies_sent == ["冷却时间必须是 1 到 180 之间的分钟数。"]
    assert plugin.arbiter.is_in_deep_cooling(_session_key("group_adm")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cool", "reset"])
async def test_admin_check_exception_rejects_without_running_mutation(action):
    plugin = _plugin()
    event = MockEvent("", sender_id="admin_1", group_id="group_adm", message_id=f"cmd_{action}")

    def raising_admin_check():
        raise RuntimeError("admin lookup unavailable")

    event.is_admin = raising_admin_check

    async def forbidden(*_args, **_kwargs):
        raise AssertionError(f"{action} must not run when admin check fails")

    if action == "cool":
        plugin._cool_session_async = forbidden
    else:
        plugin._reset_session_state_async = forbidden

    await plugin.cmd_dynamics(event, action=action, param="20" if action == "cool" else "")

    assert event.replies_sent == ["仅管理员可使用此指令。"]


@pytest.mark.asyncio
async def test_empty_whitelist_does_not_takeover():
    plugin = ChatDynamicsPlugin(
        context=MockContext(),
        config={"enable": True, "takeover_all": False, "takeover_groups": []},
    )
    ev = MockEvent("小助手在吗", group_id="group_stranger", message_id="x1")
    await plugin.on_group_message(ev)
    assert ev.is_stopped is False
    assert plugin.debounce.get_pending_count() == 0


@pytest.mark.asyncio
async def test_command_messages_are_not_swallowed():
    vc = VirtualClock(initial_time=1000.0)
    plugin = _plugin(clock=vc)
    ev = MockEvent("/dynamics status", sender_id="admin_1", group_id="group_test", message_id="cmd_x")
    await plugin.on_group_message(ev)
    assert ev.is_stopped is False
    assert plugin.debounce.get_pending_count("group_test") == 0


@pytest.mark.asyncio
async def test_wake_stripped_foreign_command_is_not_taken_over():
    plugin = _plugin()
    event = MockEvent("签到", group_id="group_cmd", message_id="checkin-1", is_at_or_wake_command=True)
    event.message_obj.message_str = "/签到"

    class CommandFilter:
        command_name = "签到"

    class Handler:
        handler_name = "checkin"
        event_filters = [CommandFilter()]
        handler = lambda self: None  # noqa: E731

    extras = {
        "activated_handlers": [Handler()],
        "handlers_parsed_params": {"other.checkin": {}},
    }
    event.get_extra = lambda key, default=None: extras.get(key, default)
    await plugin.on_group_message(event)
    assert event.is_stopped is False
    assert plugin.debounce.get_pending_count(event.unified_msg_origin) == 0
    assert plugin._metrics.get("takeover_considered", 0) == 0


@pytest.mark.asyncio
async def test_loopback_self_messages_are_ignored():
    plugin = _plugin()
    ev = MockEvent("我是机器人自己发的", sender_id="bot_42", self_id="bot_42", group_id="group_test", message_id="loop1")
    await plugin.on_group_message(ev)
    assert ev.is_stopped is False
    assert plugin.debounce.get_pending_count("group_test") == 0


@pytest.mark.asyncio
async def test_reply_and_at_are_written_into_dag():
    vc = VirtualClock(initial_time=8000.0)
    plugin = _plugin({"debounce_base_cooldown": 1.0}, clock=vc)
    dag = plugin._get_or_create_runtime(
        _session_key("group_test"), group_id="group_test", umo=_session_key("group_test"), bot_id="bot_42"
    ).dag
    assert dag is not None
    dag.add_message("bot_prev", "bot_42", "之前的回答", timestamp=8000.0)

    ev = MockEvent(
        "第二点没看懂",
        sender_id="user_alice",
        group_id="group_test",
        message_id="u_reply",
        components=[Reply(id="bot_prev"), At(qq="bot_42")],
        is_at_or_wake_command=True,
    )
    await plugin.on_group_message(ev)
    await vc.advance(1.5)
    await vc.advance(4.0)

    node = dag.get_node("u_reply")
    assert node is not None
    assert node.reply_to_id == "bot_prev"
    assert "bot_42" in node.mentioned_users
    context_ids = [n.msg_id for n in dag.get_thread_context("u_reply")]
    assert "bot_prev" in context_ids


@pytest.mark.asyncio
async def test_terminate_closes_debounce_buffer():
    plugin = _plugin()
    await plugin.initialize()
    await plugin.terminate()
    assert plugin.debounce._is_closed is True


@pytest.mark.asyncio
async def test_sticker_is_suppressed_and_not_debounced():
    plugin = _plugin()
    ev = MockEvent("", sender_id="user_alice", group_id="group_test", message_id="st1", components=[Face()])
    await plugin.on_group_message(ev)
    assert ev.is_stopped is False
    assert ev.call_llm is True
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 0
    metrics = plugin.vibe_analyzer.get_telemetrics(_session_key("group_test"), current_time=plugin.time_service.time())
    assert metrics.sample_size == 1
    assert plugin.context.agent_prompts == []


@pytest.mark.asyncio
async def test_status_command_does_not_create_dag():
    plugin = _plugin()
    ev = MockEvent("", sender_id="admin_1", group_id="group_adm", message_id="st_cmd")
    await plugin.cmd_dynamics(ev, action="status")
    assert "group_adm" not in plugin.dags
    assert "群聊动态状态监控" in ev.replies_sent[0]


@pytest.mark.asyncio
async def test_terminate_discards_pending_without_llm():
    vc = VirtualClock(initial_time=1000.0)
    plugin = _plugin({"debounce_base_cooldown": 3.0}, clock=vc)
    ev = MockEvent("小助手还没说完", group_id="group_test", message_id="pend1", is_at_or_wake_command=True)
    await plugin.on_group_message(ev)
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 1
    await plugin.terminate()
    await vc.advance(10.0)
    assert plugin.context.llm_prompts == []
    assert ev.replies_sent == []


@pytest.mark.asyncio
async def test_in_flight_generation_keeps_latest_eligible_turn():
    plugin = _plugin({"base_thinking_delay": 0.0, "chars_per_second": 1000.0})
    plugin.pacer.min_typing_delay = 0.0
    plugin.pacer.max_typing_delay = 0.0
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_llm(**kwargs):
        calls["n"] += 1
        await release.wait()
        return MockLLMProvider.make_response()

    plugin.context.tool_loop_agent = slow_llm

    events = {}

    def _flush(message_id: str, text: str) -> DebounceResult:
        ev = MockEvent(text, group_id="group_test", message_id=message_id, is_at_or_wake_command=True)
        events[message_id] = ev
        return DebounceResult(
            session_id=_session_key("group_test"),
            user_id=ev.sender_id,
            consolidated_text=text,
            messages=[],
            raw_events=[ev],
        )

    await plugin.on_turn_flushed(_flush("q1", "小助手第一问"))
    for _ in range(5):
        await asyncio.sleep(0)
        if calls["n"] == 1:
            break
    assert calls["n"] == 1
    assert _session_key("group_test") in plugin._in_flight

    await plugin.on_turn_flushed(_flush("q2", "小助手第二问"))
    assert calls["n"] == 1

    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if not plugin._background_tasks:
            break
    assert calls["n"] == 2
    assert events["q1"].replies_sent == []
    assert events["q2"].replies_sent


@pytest.mark.asyncio
async def test_same_group_id_is_isolated_by_umo_and_bot_identity():
    plugin = _plugin({"debounce_base_cooldown": 10.0})
    first = MockEvent("小助手等等", group_id="same", message_id="m1", self_id="bot-a")
    second = MockEvent("小助手等等", group_id="same", message_id="m1", self_id="bot-b")
    first.unified_msg_origin = "adapter-a:GroupMessage:same"
    second.unified_msg_origin = "adapter-b:GroupMessage:same"

    await plugin.on_group_message(first)
    await plugin.on_group_message(second)

    assert set(plugin._sessions) == {
        "adapter-a:GroupMessage:same",
        "adapter-b:GroupMessage:same",
    }
    assert plugin._sessions[first.unified_msg_origin].bot_id == "bot-a"
    assert plugin._sessions[second.unified_msg_origin].bot_id == "bot-b"
    assert plugin.debounce.get_pending_count(first.unified_msg_origin) == 1
    assert plugin.debounce.get_pending_count(second.unified_msg_origin) == 1
    await plugin.terminate()


@pytest.mark.asyncio
async def test_duplicate_platform_event_is_buffered_once():
    plugin = _plugin({"debounce_base_cooldown": 10.0})
    event = MockEvent("小助手等等", group_id="group_test", message_id="duplicate")
    duplicate = MockEvent("小助手等等", group_id="group_test", message_id="duplicate")
    await plugin.on_group_message(event)
    await plugin.on_group_message(duplicate)
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 1
    assert event.is_stopped is False
    assert duplicate.call_llm is True
    await plugin.terminate()


@pytest.mark.asyncio
async def test_send_failure_does_not_create_bot_state():
    vc = VirtualClock(initial_time=1200.0)
    plugin = _plugin(
        {"debounce_base_cooldown": 0.1, "base_thinking_delay": 0.0, "chars_per_second": 1000.0},
        clock=vc,
    )
    plugin.pacer.min_typing_delay = 0.0
    plugin.pacer.max_typing_delay = 0.0
    event = MockEvent("小助手你好", group_id="send_fail", message_id="f1")

    async def fail_send(_chain):
        raise RuntimeError("transport failed")

    event.send = fail_send
    await plugin.on_group_message(event)
    await vc.advance(1.0)
    for _ in range(10):
        await asyncio.sleep(0)

    key = event.unified_msg_origin
    runtime = plugin._sessions[key]
    assert runtime.last_bot_node is None
    assert plugin.arbiter.has_bot_spoken(key) is False
    assert all(node.user_id != runtime.bot_id for node in runtime.dag.nodes.values())


@pytest.mark.asyncio
async def test_terminate_cancels_and_waits_for_generation():
    plugin = _plugin()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_llm(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    plugin.context.tool_loop_agent = slow_llm
    event = MockEvent("小助手帮我", group_id="shutdown", message_id="q1")
    result = DebounceResult(
        session_id=event.unified_msg_origin,
        user_id=event.sender_id,
        consolidated_text=event.message_str,
        messages=[],
        raw_events=[event],
    )
    await plugin.on_turn_flushed(result)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await plugin.terminate()

    assert cancelled.is_set()
    assert not plugin._background_tasks
    assert not plugin._sessions


@pytest.mark.asyncio
async def test_llm_resolves_provider_id_before_generate():
    plugin = _plugin()
    order = []
    orig_get = plugin.context.get_current_chat_provider_id
    orig_gen = plugin.context.llm_generate

    async def get_id(umo=None):
        order.append("provider")
        return await orig_get(umo)

    async def gen(**kwargs):
        order.append("generate")
        assert kwargs.get("chat_provider_id") == "mock-provider"
        return await orig_gen(**kwargs)

    plugin.context.get_current_chat_provider_id = get_id
    plugin.context.llm_generate = gen
    plugin.provider_id = ""
    plugin.llm.configure("")
    text = await plugin._generate_llm("hi", None, GroupChatMode.CHILL_FADE, "s1")
    assert order == ["provider", "generate"]
    assert "确实是这样" in text


@pytest.mark.asyncio
async def test_llm_adapter_does_not_silently_fall_back_when_generate_exists():
    class BrokenGenerateContext:
        async def get_current_chat_provider_id(self, umo=None):
            return "p1"

        async def llm_generate(self, **kwargs):
            raise TypeError("unexpected signature")

        def get_using_provider(self, umo=None):
            raise AssertionError("legacy provider path must not run")

    adapter = LLMAdapter(BrokenGenerateContext())
    with pytest.raises(TypeError):
        await adapter.generate(prompt="hi", umo="s1", system_prompt="sys")


@pytest.mark.asyncio
async def test_llm_adapter_reports_missing_context_api():
    adapter = LLMAdapter(object())
    with pytest.raises(LLMUnavailable):
        await adapter.generate(prompt="hi", umo="s1", system_prompt="sys")


@pytest.mark.asyncio
async def test_llm_adapter_legacy_provider_success_path():
    calls = []

    class Provider:
        async def text_chat(self, **kwargs):
            calls.append(kwargs)
            return MockLLMProvider.make_response()

    class LegacyContext:
        def get_using_provider(self, umo=None):
            calls.append({"umo": umo})
            return Provider()

    adapter = LLMAdapter(LegacyContext())
    text = await adapter.generate(prompt="hi", umo="legacy-room", system_prompt="sys")

    assert calls == [
        {"umo": "legacy-room"},
        {"prompt": "hi", "system_prompt": "sys"},
    ]
    assert "确实是这样" in text


@pytest.mark.asyncio
async def test_llm_adapter_does_not_switch_from_unavailable_configured_provider():
    class LegacyContext:
        def get_provider_by_id(self, _provider_id):
            return None

        def get_using_provider(self, _umo=None):
            raise AssertionError("configured provider must not fall back to session default")

    adapter = LLMAdapter(LegacyContext(), configured_provider_id="missing-provider")
    with pytest.raises(LLMUnavailable):
        await adapter.generate(prompt="hi", umo="room", system_prompt="sys")


def test_vibe_llm_can_be_disabled_without_changing_local_vibe_analysis():
    plugin = _plugin({"vibe_llm_enabled": False})
    session_id = _session_key("quiet")
    plugin._vibe_msg_counts[session_id] = 12

    plugin._schedule_vibe_llm(session_id, "最新消息", now=500.0)

    assert plugin._vibe_llm_tasks == set()
    assert plugin.vibe_analyzer.get_mode(session_id, current_time=500.0) == GroupChatMode.CHILL_FADE


@pytest.mark.asyncio
async def test_vibe_llm_allows_only_one_in_flight_task_per_session():
    plugin = _plugin({"vibe_llm_enabled": True})
    session_id = _session_key("busy")
    plugin._vibe_msg_counts[session_id] = 12
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_refresh(sid, text, now):
        calls.append((sid, text, now))
        started.set()
        await release.wait()

    plugin._refresh_vibe_from_llm = slow_refresh
    plugin._schedule_vibe_llm(session_id, "first", now=500.0)
    plugin._schedule_vibe_llm(session_id, "second", now=501.0)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert calls == [(session_id, "first", 500.0)]
    assert len(plugin._vibe_llm_tasks_by_session) == 1

    release.set()
    await asyncio.gather(*list(plugin._vibe_llm_tasks))
    await asyncio.sleep(0)
    assert session_id not in plugin._vibe_llm_tasks_by_session


def test_vibe_llm_throttles_snapshot_recorded_at_zero_time():
    plugin = _plugin({"vibe_llm_enabled": True})
    session_id = _session_key("epoch")
    plugin._vibe_msg_counts[session_id] = 12
    plugin.vibe_analyzer.mark_llm_snapshot(session_id, 0.0)

    plugin._schedule_vibe_llm(session_id, "too soon", now=60.0)

    assert plugin._vibe_llm_tasks == set()


@pytest.mark.asyncio
async def test_async_reset_discards_pending_debounce_without_reinjecting_old_turn():
    clock = VirtualClock(initial_time=2000.0)
    plugin = _plugin({"debounce_base_cooldown": 3.0}, clock=clock)
    event = MockEvent("小助手等等", group_id="reset_pending", message_id="old")
    await plugin.on_group_message(event)
    key = event.unified_msg_origin
    assert plugin.debounce.get_pending_count(key) == 1

    await plugin._reset_session_state_async(key)
    await clock.advance(10.0)

    assert plugin.debounce.get_pending_count(key) == 0
    assert plugin.context.llm_prompts == []
    assert plugin.dags[key].nodes == {}


@pytest.mark.asyncio
async def test_safe_hover_followup_is_re_evaluated_once():
    # This test counts reply generation only; topic LLM calls have their own tests.
    plugin = _plugin({"base_thinking_delay": 0.0, "chars_per_second": 1000.0,
                      "topic_reranker_enabled": False})
    plugin.pacer.min_typing_delay = 0.0
    plugin.pacer.max_typing_delay = 0.0
    key = _session_key("hover")
    runtime = plugin._get_or_create_runtime(key, group_id="hover", umo=key, bot_id="bot_42")
    bot = runtime.dag.add_message(
        "bot_prev", "bot_42", "这个方案可以继续优化", timestamp=plugin.time_service.time()
    )
    runtime.last_bot_node = bot

    first = MockEvent("这个方案", group_id="hover", message_id="h1")
    second = MockEvent("这个方案然后呢", group_id="hover", message_id="h2")
    await plugin.on_turn_flushed(DebounceResult(key, "user_1", first.message_str, [], [first]))
    assert runtime.pending_hover is not None
    assert plugin.context.llm_prompts == []

    await plugin.on_turn_flushed(DebounceResult(key, "user_1", second.message_str, [], [second]))
    for _ in range(20):
        await asyncio.sleep(0)
        if plugin.context.llm_prompts:
            break
    assert len(plugin.context.llm_prompts) == 1
    assert runtime.pending_hover is None


@pytest.mark.asyncio
async def test_vibe_classifier_prompt_contains_window_telemetrics_and_labels():
    plugin = _plugin()
    key = _session_key("vibe_prompt")
    plugin.vibe_analyzer.record_message(key, "这个 Python 接口报错了", timestamp=plugin.time_service.time())

    await plugin._classify_vibe_with_llm(key, "最新一条")

    prompt = plugin.context.llm_prompts[-1]
    assert "Telemetrics: mpm=" in prompt
    assert "technical_help" in prompt
    assert "Recent messages:" in prompt


@pytest.mark.asyncio
async def test_complete_at_message_skips_debounce_and_keeps_native_llm():
    vc = VirtualClock(initial_time=2000.0)
    plugin = _plugin({"debounce_base_cooldown": 3.5}, clock=vc)
    ev = MockEvent(
        "小助手帮我看看这段报错",
        group_id="group_test",
        message_id="fast1",
        is_at_or_wake_command=True,
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(ev)
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 0
    assert ev.is_stopped is False
    assert ev.call_llm is False
    assert plugin.context.agent_prompts == []


@pytest.mark.asyncio
async def test_exclusive_mode_still_stops_text_events():
    plugin = _plugin({"pipeline_mode": "exclusive", "debounce_base_cooldown": 10.0})
    ev = MockEvent("小助手等等", group_id="group_test", message_id="ex1", is_at_or_wake_command=True)
    await plugin.on_group_message(ev)
    assert ev.is_stopped is True
    assert ev.call_llm is True
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 1
    await plugin.terminate()


@pytest.mark.asyncio
async def test_filter_mode_does_not_stop_other_handlers():
    plugin = _plugin({"debounce_base_cooldown": 10.0})
    ev = MockEvent("小助手等等", group_id="group_test", message_id="f1", is_at_or_wake_command=True)
    await plugin.on_group_message(ev)
    assert ev.is_stopped is False
    assert ev.call_llm is True
    await plugin.terminate()


@pytest.mark.asyncio
async def test_short_at_forbids_host_default_llm():
    plugin = _plugin({"debounce_base_cooldown": 10.0})
    ev = MockEvent(
        "小助手你好",
        group_id="group_test",
        message_id="short-at",
        is_at_or_wake_command=True,
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(ev)
    assert ev.is_stopped is False
    assert ev.call_llm is True
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 1
    assert plugin.context.agent_prompts == []
    await plugin.terminate()


@pytest.mark.asyncio
async def test_incomplete_at_then_complete_at_does_not_plugin_reply():
    vc = VirtualClock(initial_time=3000.0)
    plugin = _plugin(
        {
            "debounce_base_cooldown": 3.5,
            "base_thinking_delay": 0.0,
            "chars_per_second": 1000.0,
        },
        clock=vc,
    )
    incomplete = MockEvent(
        "小助手等等",
        group_id="group_test",
        message_id="at-hang",
        is_at_or_wake_command=True,
        components=[At(qq="bot_42")],
    )
    complete = MockEvent(
        "小助手帮我看看这段报错",
        group_id="group_test",
        message_id="at-full",
        is_at_or_wake_command=True,
        components=[At(qq="bot_42")],
    )
    await plugin.on_group_message(incomplete)
    assert incomplete.call_llm is True
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 1

    await plugin.on_group_message(complete)
    assert complete.call_llm is False
    assert plugin.debounce.get_pending_count(_session_key("group_test")) == 0

    await vc.advance(10.0)
    await asyncio.sleep(0)
    assert incomplete.replies_sent == []
    assert complete.replies_sent == []
    assert plugin.context.agent_prompts == []
    await plugin.terminate()
