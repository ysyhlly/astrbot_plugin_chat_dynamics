"""Behavior regressions for the verified September audit findings."""
import asyncio
import logging
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from astrbot_plugin_chat_dynamics.core.arbiter import ArbitrationResult
from .test_plugin_lifecycle import MockContext
from .test_thread_router import setup, add


@pytest.fixture
def plugin():
    return ChatDynamicsPlugin(MockContext(), {"decision_mode": "legacy", "enable": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "raise", "cancel"])
async def test_failed_save_restores_values_and_blocks_early_application(plugin, failure):
    before = dict(plugin.config)
    started, release = asyncio.Event(), asyncio.Event()

    async def save():
        started.set()
        await release.wait()
        if failure == "raise":
            raise OSError("disk unavailable")
        return False

    plugin.save_config = save
    task = asyncio.create_task(plugin.save_config_values({"enable": False, "bot_names": ["new"]}))
    await started.wait()
    plugin._sync_runtime_from_config()
    assert plugin._runtime_config.enabled is True
    if failure == "cancel":
        task.cancel()
    else:
        release.set()
    with pytest.raises((RuntimeError, OSError, asyncio.CancelledError)):
        await task
    assert plugin.config == before
    plugin._sync_runtime_from_config()
    assert plugin._runtime_config.enabled is True
    assert not plugin._config_save_in_progress


@pytest.mark.asyncio
async def test_partial_config_assignment_is_rolled_back(plugin):
    class Config(dict):
        def __setitem__(self, key, value):
            if key == "bot_names":
                raise TypeError("read only field")
            super().__setitem__(key, value)

    plugin.config = Config(plugin.config)
    before = dict(plugin.config)
    plugin.save_config = AsyncMock()
    with pytest.raises(RuntimeError, match="failed to set bot_names"):
        await plugin.save_config_values({"enable": False, "bot_names": ["new"]})
    assert plugin.config == before
    plugin.save_config.assert_not_called()


def gate(allow=True, code="proactive", proactive=True):
    verdict = SimpleNamespace(proactive=proactive, as_dict=lambda: {"proactive": proactive})
    return SimpleNamespace(should_speak=allow, reason_code=code, reason_zh="reason",
                           proactive=verdict, rhythm=None, skin=object(),
                           length_hint="short", delay_scale=0.5)


@dataclass
class ExtendedResult(ArbitrationResult):
    extra_diagnostic: str = "preserved"


@pytest.mark.parametrize("threshold", [0.55, 0.65, 0.75])
def test_override_score_matches_threshold_and_preserves_future_fields(plugin, threshold):
    original = ExtendedResult(False, 0.1, threshold, "threshold")
    runtime = SimpleNamespace()
    result = plugin._resolve_gate_result(runtime, "session", original, gate(), 1.0)
    assert result.should_speak and result.willingness_score >= threshold
    assert result.extra_diagnostic == "preserved"
    assert original.should_speak is False
    assert plugin.arbiter.last_decision("session") == result
    assert runtime._pending_gate_skin is not None


@pytest.mark.parametrize("flag", ["in_deep_cooling", "is_energy_asymmetric", "private_topic"])
def test_proactive_cannot_override_hard_block(plugin, flag):
    original = replace(ArbitrationResult(False, 0.1, 0.75, "blocked"), **{flag: True})
    runtime = SimpleNamespace()
    result = plugin._resolve_gate_result(runtime, "session", original, gate(), 1.0)
    assert result is original
    assert not hasattr(runtime, "_pending_gate_skin")


@pytest.mark.parametrize("code,expected", [("no_gap", True), ("newcomer_caution", True), ("media", False)])
def test_gate_veto_priority_preserved(plugin, code, expected):
    original = ExtendedResult(True, 0.8, 0.75, "allowed")
    result = plugin._resolve_gate_result(SimpleNamespace(), "session", original, gate(False, code), 1.0)
    assert result.should_speak is expected
    assert result.extra_diagnostic == "preserved"


def test_router_logs_no_content_or_identifiers_even_at_debug(caplog):
    runtime, router = setup()
    with caplog.at_level(logging.DEBUG):
        add(runtime, router, "SECRET_MESSAGE_ID", "SECRET_USER_ID", "SECRET_CHAT_BODY", 1)
    records = [r for r in caplog.records if "[Router]" in r.getMessage()]
    assert records
    assert all(r.levelno == logging.DEBUG for r in records)
    assert "SECRET_" not in " ".join(r.getMessage() for r in records)


def test_arbiter_silence_keeps_gate_bookkeeping_on_the_civil_clock(plugin):
    """A withheld turn must not roll the manners day bucket back to 1970.

    The legacy pipeline hands this method the monotonic turn clock by mistake in
    an earlier revision; ``note_quiet`` dates its bucket with ``time.localtime``,
    so the monotonic value wiped today's counters on every withheld turn and
    stamped the replay rows as 1970.
    """
    session = "session-clock"
    manners = plugin.decision_gate.manners
    manners.note_intervene(session, occasion_kind="neutral", now=plugin.time_service.wall_time())
    monotonic = plugin.time_service.time()
    wall = plugin.time_service.wall_time()
    assert abs(monotonic - wall) > 1e6, "the two clocks must differ for this test to mean anything"

    silenced = ArbitrationResult(False, 0.1, 0.75, "wts_low")
    result = plugin._resolve_gate_result(
        SimpleNamespace(), session, silenced, gate(False, "wts_low", proactive=False), wall)
    assert result.should_speak is False

    stats = manners.today_stats(session)
    assert stats["quiet"] == 1
    assert stats["intervene"] == 1, "the arbiter silence must not reset today's counters"
    assert manners._day_stamp[session] == time.strftime("%Y-%m-%d")
    assert abs(float(stats["why_silent"][-1]["ts"]) - time.time()) < 120

    # Live bookkeeping obtains its own wall clock; no turn timestamp can leak in.
    manners.note_quiet(session, reason_code="wts_low", reason_zh="x", now=None)
    plugin.decision_gate.note_arbiter_silence(session, "wts_low")
    assert manners._day_stamp[session] == time.strftime("%Y-%m-%d")
    assert manners.today_stats(session)["quiet"] >= 2
    assert abs(float(manners.today_stats(session)["why_silent"][-1]["ts"]) - time.time()) < 120


def test_idle_eviction_does_not_cancel_a_quiet_window():
    """空闲清会话是为了省内存，不是为了撤销“今天别闹”。"""
    from .test_plugin_lifecycle import _plugin, _session_key

    plugin = _plugin()
    key = _session_key("quiet_group")
    plugin._get_or_create_runtime(key, group_id="quiet_group", umo=key)
    occasion = plugin.decision_gate.occasion
    occasion.note_cool_command(key, now=plugin.time_service.wall_time())

    plugin._drop_session(key)

    assert plugin._sessions.get(key) is None
    assert occasion.cool_remaining(key, now=plugin.time_service.wall_time()) > 0


def test_an_explicit_reset_does_cancel_the_quiet_window():
    """/dynamics reset 是运维动作，它必须能撤销这条指令。"""
    from .test_plugin_lifecycle import _plugin, _session_key

    plugin = _plugin()
    key = _session_key("quiet_group_2")
    plugin._get_or_create_runtime(key, group_id="quiet_group_2", umo=key)
    occasion = plugin.decision_gate.occasion
    occasion.note_cool_command(key, now=plugin.time_service.wall_time())

    plugin._reset_session_state(key)

    assert occasion.cool_remaining(key, now=plugin.time_service.wall_time()) == 0


@pytest.mark.asyncio
async def test_a_sent_reply_records_the_member_it_answered(monkeypatch):
    """机器人消息要写下它回应的是谁，逐条锚点不能退化成会话级近似。"""
    from astrbot_plugin_chat_dynamics.core.debounce import DebounceResult
    from .test_plugin_lifecycle import At, MockEvent, _plugin, _session_key

    plugin = _plugin({"pipeline_mode": "exclusive", "vibe_llm_enabled": False})
    plugin.pacer.min_typing_delay = 0.0
    plugin.pacer.max_typing_delay = 0.0
    monkeypatch.setattr(plugin.pacer, "calculate_typing_delay", lambda *a, **k: 0)

    async def reply(*_args, **_kwargs):
        return "先看日志"

    monkeypatch.setattr(plugin, "_run_native_reply", reply)
    event = MockEvent("小助手帮我看看报错", sender_id="alice", group_id="anchor",
                      message_id="a1", components=[At("bot_42")])
    key = _session_key("anchor")
    try:
        await plugin.on_turn_flushed(
            DebounceResult(key, event.sender_id, event.message_str, [], [event]))
        for _ in range(50):
            await asyncio.sleep(0)
            if event.replies_sent:
                break
        assert event.replies_sent == ["先看日志"]
        bot_nodes = [node for node in plugin.dags[key].nodes.values()
                     if node.user_id == "bot_42"]
        assert bot_nodes
        assert bot_nodes[-1].metadata["trigger_user_id"] == "alice"
    finally:
        await plugin.terminate()


def test_thread_divergence_is_observed_after_the_bot_turn_leaves_the_short_window():
    """分歧最大时惩罚不能消失：机器人上一条滚出 20 条窗口也要记录介入条数。"""
    from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
    from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
    from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime

    def observed(count):
        dag = ConversationDAG()
        bot = dag.add_message("b", "bot", "你用什么版本？", timestamp=1)
        for index in range(count):
            dag.add_message(f"x{index}", f"other{index}", "闲聊", timestamp=2 + index)
        node = dag.add_message("n", "alice", "好", timestamp=100)
        runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=dag)
        runtime.last_bot_node = bot
        score = AddressivityRouter(bot_id="bot").compute_addressivity(
            node, dag, last_bot_node=bot, bot_id="bot", runtime=runtime)
        return {item.code: item.raw_value for item in score.evidence}

    assert observed(3).get("intervening_messages") == 3
    assert observed(25).get("intervening_messages") == 25, \
        "机器人上一条离开短窗口后，介入条数被整条丢弃，惩罚反而变成加分"


def test_the_media_gate_learns_whether_the_host_can_carry_media(monkeypatch):
    """多模态能力必须接线：宿主入口带不了媒体时，L2 要安静降级而不是假装看得见。"""
    from .test_plugin_lifecycle import _plugin

    plugin = _plugin()
    # MockContext 的 llm_generate 收 **kwargs：媒体能带上，理解路径应当被允许。
    assert plugin.decision_gate.media.multimodal_available() is True
    allowed = plugin.decision_gate.media.evaluate(
        text="帮我看下这个报错", has_media=True, media_component_types=["image"],
        explicit=True, media_understand_reply_enabled=True)
    assert allowed.request_understand is True

    # 只接受 prompt/system_prompt、且没有 agent 通道的宿主：必须降级。
    async def text_only(prompt: str, system_prompt: str = ""):
        return "ok"

    plugin.context.llm_generate = text_only
    monkeypatch.setattr(plugin.persona_engine.bridge, "check", lambda: False)
    plugin._refresh_multimodal_availability()

    assert plugin.decision_gate.media.multimodal_available() is False
    degraded = plugin.decision_gate.media.evaluate(
        text="帮我看下这个报错", has_media=True, media_component_types=["image"],
        explicit=True, media_understand_reply_enabled=True)
    assert degraded.request_understand is False
    assert degraded.multimodal_degraded is True


@pytest.mark.asyncio
async def test_an_active_policy_reaches_the_routers_without_a_config_save(monkeypatch):
    """active 策略不依赖“有人保存过一次配置”才能生效。

    折入策略的代码在“配置没变就早退”的守卫之后，而策略并不属于宿主配置，
    所以未保存配置时它永远到不了路由器；refresh 必须绕过那两个守卫。
    """
    from astrbot_plugin_chat_dynamics.core.learning_policy import (
        MODE_ACTIVE, STATUS_ACTIVE, Decision,
    )
    from .test_plugin_lifecycle import _plugin

    plugin = _plugin({"learning_policy_mode": "active", "strong_addressivity_threshold": 0.70})
    plugin._sync_runtime_from_config()
    assert plugin.addressivity_router.strong_threshold == pytest.approx(0.70)

    applied = Decision(mode=MODE_ACTIVE, status=STATUS_ACTIVE, policy_id="policy_v3",
                       applied=True, overrides={"strong_addressivity_threshold": 0.85})

    async def refresh(*, effective_config):
        # The real consumer stores the resolved decision; the plugin's fold-in reads
        # it back through `consumer.decision.applied`.
        plugin.learning_policy.consumer.decision = applied
        return applied

    monkeypatch.setattr(plugin.learning_policy, "refresh", refresh)

    await plugin._refresh_learning_policy(force=True)

    assert plugin.learning_policy.last_apply_reason == "applied"
    assert plugin._runtime_config.strong_addressivity_threshold == pytest.approx(0.85)
    assert plugin.addressivity_router.strong_threshold == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_withheld_turn_uses_gate_owned_civil_clock(monkeypatch):
    """Pin the caller: the legacy pipeline must not hand over the monotonic clock.

    The unit test above covers the gate-owned clock; this one drives the real
    call site in ``_finish_turn_locked``, which is where the wrong clock came from.
    """
    from astrbot_plugin_chat_dynamics.core.debounce import DebounceResult
    from .test_plugin_lifecycle import MockEvent, _plugin, _session_key

    plugin = _plugin({"vibe_llm_enabled": False, "decision_mode": "legacy"})
    event = MockEvent("今天天气不错", group_id="clock_group", message_id="clock_1")
    key = _session_key("clock_group")
    silent_gate = SimpleNamespace(
        should_speak=False, reason_code="gap_wait", reason_zh="等待缺口",
        skin=SimpleNamespace(as_dict=lambda: {}),
        manners=SimpleNamespace(as_dict=lambda: {}),
        media=None, request_understand=False, proactive=None, rhythm=None,
        length_hint="normal", delay_scale=1.0)
    monkeypatch.setattr(plugin.decision_gate, "evaluate", lambda **kwargs: silent_gate)
    monkeypatch.setattr(plugin.arbiter, "maybe_auto_cool", lambda **kwargs: None)
    monkeypatch.setattr(plugin.arbiter, "evaluate",
                        lambda **kwargs: ArbitrationResult(False, 0.1, 0.75, "wts_low"))

    seen = {}
    original = plugin.decision_gate.note_arbiter_silence
    def spy(session_id, reason):
        seen["called"] = True
        return original(session_id, reason)
    monkeypatch.setattr(plugin.decision_gate, "note_arbiter_silence", spy)

    manners = plugin.decision_gate.manners
    manners.note_intervene(key, occasion_kind="neutral", now=plugin.time_service.wall_time())
    try:
        await plugin.on_turn_flushed(
            DebounceResult(key, event.sender_id, event.message_str, [], [event]))
    finally:
        await plugin.terminate()

    assert seen, "the withheld turn must reach the arbiter silence bookkeeping"
    stats = manners.today_stats(key)
    assert abs(float(stats["why_silent"][-1]["ts"]) - time.time()) < 300
    assert stats["intervene"] == 1, "the withheld turn wiped today's counters"
    assert stats["quiet"] == 1
    assert manners._day_stamp[key] == time.strftime("%Y-%m-%d")
