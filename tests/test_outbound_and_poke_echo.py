"""Comprehensive tests for OutboundLedger, OneBot outbound observer, and self-poke echo suppression."""

from __future__ import annotations

import asyncio
import functools
import threading
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.outbound_ledger import (
    OneBotOutboundObserver,
    OutboundLedger,
    extract_onebot_message_text,
    is_owned_outbound_send,
    owned_outbound_send,
)
from astrbot_plugin_chat_dynamics.core.platform_bridge import (
    build_poke_chain,
    format_delivered_action_marker,
    format_delivered_action_summary,
    send_plain,
)
from .test_persona_model import BridgeDouble, drain, flush
from .test_plugin_lifecycle import MockEvent, _plugin
from .test_poke import Poke
from .test_private_chat_chain import private_event, private_plugin as _private_plugin

private_plugin = _private_plugin


# ---------------------------------------------------------------------------
# 1. OutboundLedger retention, caps, truncation, formatting & isolation
# ---------------------------------------------------------------------------


def test_outbound_ledger_retention_caps_truncation_and_formatting():
    now = 10_000.0
    ledger = OutboundLedger(clock=lambda: now)
    umo = "aiocqhttp:FriendMessage:1001"

    assert ledger.format_recent_outbound_note(umo, now=now) == ""

    # Expired item (> 30 min = 1800s ago) must be pruned
    assert ledger.record_external_outbound(
        umo,
        text="【旧提醒】这条早就过期了",
        now=now - 1805.0,
    )
    assert ledger.get_recent_entries(umo, now=now) == []
    assert ledger.format_recent_outbound_note(umo, now=now) == ""

    # Insert 10 items inside 30 min window -> capped at latest 8
    for idx in range(10):
        long_text = f"【撤回提醒#{idx}】\n\t" + ("甲" * 160)
        assert ledger.record_external_outbound(
            umo,
            text=long_text,
            now=now - 600.0 + idx * 30.0,
        )

    entries = ledger.get_recent_entries(umo, now=now)
    assert len(entries) == 8
    assert entries[0].text.startswith("【撤回提醒#2】")
    assert entries[-1].text.startswith("【撤回提醒#9】")
    for item in entries:
        assert len(item.text) <= 120
        assert "\n" not in item.text
        assert "\t" not in item.text

    # Empty / whitespace text ignored
    assert ledger.record_external_outbound(umo, text="   \n\t  ", now=now) is False

    note = ledger.format_recent_outbound_note(umo, now=now)
    assert "[近期其他插件或动作的对外记录（只读参考，非你本轮发言）]" in note
    assert "[外部插件]" in note
    assert "【撤回提醒#9】" in note
    assert "【撤回提醒#1】" not in note


def test_outbound_ledger_session_mapping_and_scope_platform_isolation():
    now = 5000.0
    ledger = OutboundLedger(clock=lambda: now)

    # Record via raw scope+target before UMO is registered
    ledger.record_external_outbound(
        scope="private",
        target_id="1001",
        platform_id="qq_main",
        text="【撤回提醒】小明撤回了一条消息",
        now=now - 20.0,
    )
    ledger.record_external_outbound(
        scope="group",
        target_id="1001",
        platform_id="qq_main",
        text="【B站推送】UP主开播了",
        now=now - 10.0,
    )
    ledger.record_external_outbound(
        scope="group",
        target_id="1001",
        platform_id="qq_other",
        text="【其他机器人实例】不应串号",
        now=now - 5.0,
    )

    dm_note = ledger.format_recent_outbound_note("qq_main:FriendMessage:1001", now=now)
    assert "【撤回提醒】" in dm_note
    assert "【B站推送】" not in dm_note
    assert "不应串号" not in dm_note

    grp_note = ledger.format_recent_outbound_note("qq_main:GroupMessage:1001", now=now)
    assert "【B站推送】" in grp_note
    assert "【撤回提醒】" not in grp_note
    assert "不应串号" not in grp_note

    other_bot_note = ledger.format_recent_outbound_note("qq_other:GroupMessage:1001", now=now)
    assert "不应串号" in other_bot_note
    assert "【B站推送】" not in other_bot_note

    # Raw ID fallback lookup with explicit scope & platform_id
    raw_dm = ledger.format_recent_outbound_note(
        "1001",
        now=now,
        platform_id="qq_main",
        target_id="1001",
        scope="private",
    )
    assert "【撤回提醒】" in raw_dm
    assert "【B站推送】" not in raw_dm


def test_outbound_ledger_thread_safety():
    ledger = OutboundLedger(clock=lambda: 1000.0)
    errors: list[BaseException] = []

    def writer(tid: int) -> None:
        try:
            for i in range(40):
                ledger.record_external_outbound(
                    f"aiocqhttp:GroupMessage:{tid % 4}",
                    text=f"msg-{tid}-{i}",
                    now=1000.0 + i,
                )
                ledger.format_recent_outbound_note(f"aiocqhttp:GroupMessage:{tid % 4}", now=1050.0)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(idx,)) for idx in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for gid in range(4):
        assert len(ledger.get_recent_entries(f"aiocqhttp:GroupMessage:{gid}", now=1050.0)) == 8


def test_extract_onebot_message_text_handles_cq_and_segments():
    assert (
        extract_onebot_message_text("【撤回提醒】[CQ:at,qq=1001] 撤回了 [CQ:image,file=abc.png] &#91;ok&#93;")
        == "【撤回提醒】@1001 撤回了 [图片] [ok]"
    )
    segments = [
        {"type": "reply", "data": {"id": "999"}},
        {"type": "text", "data": {"text": "【B站推送】新视频："}},
        {"type": "image", "data": {"file": "cover.jpg"}},
        {"type": "face", "data": {"id": "66"}},
    ]
    assert extract_onebot_message_text(segments) == "【B站推送】新视频：[图片][表情]"


# ---------------------------------------------------------------------------
# 2. OneBotOutboundObserver: capture, dedup, re-entrancy, idempotency, uninstall
# ---------------------------------------------------------------------------


class _FakeCQHttpClient:
    """Mimics real aiocqhttp.CQHttp where send_private_msg/send_group_msg use __getattr__ -> call_action."""

    def __init__(self, *, fail_next: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail_next = fail_next

    async def call_action(self, action: str, **params):
        self.calls.append((action, dict(params)))
        if self.fail_next:
            self.fail_next = False
            return {"status": "failed", "retcode": 1400}
        return {"status": "ok", "retcode": 0, "data": {"message_id": 100 + len(self.calls)}}

    def __getattr__(self, item: str):
        if item.startswith("_"):
            raise AttributeError(item)
        return functools.partial(self.call_action, item)


class _FakeDirectMethodClient:
    """Client with explicit send_private_msg that internally delegates to call_action."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **params):
        self.calls.append((action, dict(params)))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 200}}

    async def send_private_msg(self, **params):
        return await self.call_action("send_private_msg", **params)

    async def send_group_msg(self, **params):
        return await self.call_action("send_group_msg", **params)


@pytest.mark.asyncio
async def test_observer_captures_external_plugins_and_dedups_chat_dynamics_sends():
    now = 2000.0
    ledger = OutboundLedger(clock=lambda: now)
    observer = OneBotOutboundObserver(ledger)

    cq = _FakeCQHttpClient()
    assert observer.install_client(cq, platform_id="qq_main") is True
    # Idempotent re-install must not double-wrap
    assert observer.install_client(cq, platform_id="qq_main") is True
    assert observer.wrapped_count == 1

    # 1) External plugin calls send_private_msg (via __getattr__ -> call_action)
    await cq.send_private_msg(user_id=1001, message="【撤回提醒】小张撤回了一条消息：哈哈发错了")
    # 2) External plugin calls send_group_msg via call_action
    await cq.call_action("send_group_msg", group_id=2002, message=[{"type": "text", "data": {"text": "【B站推送】新动态"}}])
    # 3) Failed external send must NOT be recorded
    cq.fail_next = True
    await cq.send_private_msg(user_id=1001, message="这条发送失败不应记录")

    # 4) chat_dynamics own sends inside owned_outbound_send() / send_plain MUST be ignored!
    assert is_owned_outbound_send() is False
    with owned_outbound_send():
        assert is_owned_outbound_send() is True
        await cq.send_private_msg(user_id=1001, message="这是chat_dynamics自己的回复")
        await cq.call_action("send_poke", user_id="1001")
    assert is_owned_outbound_send() is False

    # Also test platform_bridge.send_plain wrapping send_poke with owned_outbound_send
    poke_event = MockEvent("hi", sender_id="1001", group_id="", message_id="m1")
    poke_event.get_platform_name = lambda: "aiocqhttp"
    poke_event.bot = cq
    res = await send_plain(poke_event, None, "qq_main:FriendMessage:1001", build_poke_chain("1001"))
    assert res.success is True

    dm_entries = ledger.get_recent_entries("qq_main:FriendMessage:1001", now=now + 5)
    assert len(dm_entries) == 1
    assert "【撤回提醒】小张撤回了一条消息" in dm_entries[0].text
    assert all("chat_dynamics自己" not in e.text for e in dm_entries)
    assert all("发送失败" not in e.text for e in dm_entries)

    grp_entries = ledger.get_recent_entries("qq_main:GroupMessage:2002", now=now + 5)
    assert len(grp_entries) == 1
    assert grp_entries[0].text == "【B站推送】新动态"

    # 5) Clean uninstall restores original methods
    assert observer.uninstall() == 1
    assert observer.wrapped_count == 0
    await cq.send_private_msg(user_id=1001, message="卸载后不应再被捕获")
    assert len(ledger.get_recent_entries("qq_main:FriendMessage:1001", now=now + 10)) == 1


@pytest.mark.asyncio
async def test_observer_prevents_double_record_on_delegating_client_and_isolates_concurrent_tasks():
    ledger = OutboundLedger(clock=lambda: 3000.0)
    observer = OneBotOutboundObserver(ledger)
    client = _FakeDirectMethodClient()
    assert observer.install_client(client, platform_id="aiocqhttp") is True

    # Direct method delegates to call_action -> must record ONCE, not twice
    await client.send_private_msg(user_id="888", message="【定时提醒】该喝水啦")
    entries = ledger.get_recent_entries("aiocqhttp:FriendMessage:888", now=3005.0)
    assert len(entries) == 1
    assert entries[0].text == "【定时提醒】该喝水啦"

    # Concurrent task isolation: chat_dynamics awaiting inside owned_outbound_send()
    # must NOT suppress an unrelated external plugin task running concurrently!
    gate = asyncio.Event()
    done_ext = asyncio.Event()

    async def owned_task() -> None:
        with owned_outbound_send():
            gate.set()
            await done_ext.wait()
            await client.send_group_msg(group_id="777", message="owned-reply")

    async def external_task() -> None:
        await gate.wait()
        await client.send_group_msg(group_id="777", message="【外部通知】并发推送")
        done_ext.set()

    await asyncio.gather(owned_task(), external_task())
    grp_entries = ledger.get_recent_entries("aiocqhttp:GroupMessage:777", now=3005.0)
    assert [e.text for e in grp_entries] == ["【外部通知】并发推送"]
    observer.uninstall()


@pytest.mark.asyncio
async def test_observer_install_from_context_and_terminate_uninstall():
    cq = _FakeCQHttpClient()
    plat = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="qq_live", name="aiocqhttp"),
        get_client=lambda: cq,
    )
    plugin = _plugin({"decision_mode": "legacy", "pipeline_mode": "filter"})
    plugin.context.platform_manager = SimpleNamespace(get_insts=lambda: [plat])
    plugin._ensure_outbound_observer()
    assert plugin._outbound_observer.wrapped_count == 1

    await cq.send_group_msg(group_id=55555, message="【撤回提醒】测试群撤回")
    note = plugin.outbound_ledger.format_recent_outbound_note("qq_live:GroupMessage:55555")
    assert "【撤回提醒】测试群撤回" in note

    await plugin.terminate()
    assert plugin._outbound_observer.wrapped_count == 0
    assert "call_action" not in cq.__dict__


# ---------------------------------------------------------------------------
# 3. Self-poke echo suppression & action_text backfill (Private & Group)
# ---------------------------------------------------------------------------


def test_outbound_ledger_self_poke_echo_matching_rules():
    ledger = OutboundLedger(clock=lambda: 1000.0, echo_window_seconds=15.0)
    dm_umo = "aiocqhttp:FriendMessage:1001"
    grp_umo = "aiocqhttp:GroupMessage:9001"

    # Private NapCat echo (user_id == target_id == friend_id) WITHOUT recent bot poke -> NOT echo
    assert (
        ledger.match_and_consume_self_poke_echo(
            dm_umo,
            sender_id="1001",
            target_id="1001",
            bot_id="bot_42",
            now=1001.0,
            action_text="戳了戳自己",
            scope="private",
        )
        is False
    )

    # Bot pokes friend 1001 -> NapCat private echo arrives 2s later -> MATCH & BACKFILL
    ledger.record_bot_poke_sent(dm_umo, "1001", "bot_42", 1010.0, scope="private")
    assert (
        ledger.match_and_consume_self_poke_echo(
            dm_umo,
            sender_id="1001",
            target_id="1001",
            bot_id="bot_42",
            now=1012.0,
            action_text="揉了揉...的脸，喵呜~",
            scope="private",
        )
        is True
    )
    note = ledger.format_recent_outbound_note(dm_umo, now=1015.0)
    assert "[戳一戳回显]" in note
    assert "刚才你戳了对方（实际展示动作：揉了揉...的脸，喵呜~）" in note

    # Consumed once: immediate second self-poke must NOT be swallowed
    assert (
        ledger.match_and_consume_self_poke_echo(
            dm_umo,
            sender_id="1001",
            target_id="1001",
            bot_id="bot_42",
            now=1013.0,
            action_text="又戳了自己",
            scope="private",
        )
        is False
    )

    # Expired echo (> 15s) must NOT match
    ledger.record_bot_poke_sent(dm_umo, "1001", "bot_42", 1020.0, scope="private")
    assert (
        ledger.match_and_consume_self_poke_echo(
            dm_umo,
            sender_id="1001",
            target_id="1001",
            bot_id="bot_42",
            now=1040.0,
            action_text="过期回显",
            scope="private",
        )
        is False
    )

    # User poking bot back (sender_id=1001, target_id=bot_42) right after bot poked 1001 -> NOT echo!
    ledger.record_bot_poke_sent(dm_umo, "1001", "bot_42", 1050.0, scope="private")
    assert (
        ledger.match_and_consume_self_poke_echo(
            dm_umo,
            sender_id="1001",
            target_id="bot_42",
            bot_id="bot_42",
            now=1052.0,
            action_text="拍了拍...头顶",
            scope="private",
        )
        is False
    )

    # Group standard echo (sender_id == bot_id) -> ALWAYS match + backfill
    ledger.record_bot_poke_sent(grp_umo, "1001", "bot_42", 1060.0, scope="group")
    assert (
        ledger.match_and_consume_self_poke_echo(
            grp_umo,
            sender_id="bot_42",
            target_id="1001",
            bot_id="bot_42",
            now=1061.0,
            action_text="拍了拍...肩膀",
            scope="group",
        )
        is True
    )
    grp_note = ledger.format_recent_outbound_note(grp_umo, now=1065.0)
    assert "刚才你戳了对方（实际展示动作：拍了拍...肩膀）" in grp_note

    # Group peer poke (1002 -> 1001) right after bot poked 1001 -> NOT echo!
    ledger.record_bot_poke_sent(grp_umo, "1001", "bot_42", 1070.0, scope="group")
    assert (
        ledger.match_and_consume_self_poke_echo(
            grp_umo,
            sender_id="1002",
            target_id="1001",
            bot_id="bot_42",
            now=1071.0,
            action_text="戳了戳",
            scope="group",
        )
        is False
    )


@pytest.mark.asyncio
async def test_private_napcat_self_poke_echo_suppressed_and_backfilled_e2e(private_plugin):
    plugin, bridge = private_plugin
    umo = "aiocqhttp:FriendMessage:1001"
    cq = _FakeCQHttpClient()

    # Step 1: Simulate bot poking friend 1001 via _send_owned + record_bot_poke (full live path)
    seed_ev = private_event("你好", sender="1001", message_id="dm-seed", umo=umo)
    seed_ev.get_platform_name = lambda: "aiocqhttp"
    seed_ev.bot = cq
    runtime = plugin._get_or_create_runtime(umo, group_id="", umo=umo, bot_id="bot_42", scope_kind="private")
    plugin._ensure_outbound_observer(seed_ev)

    send_res = await plugin._send_owned(runtime, seed_ev, build_poke_chain("1001"))
    assert send_res.success is True
    plugin.record_bot_poke(umo, bot_id="bot_42", target_id="1001")

    # Chat dynamics' own send_poke must NOT be recorded as an external plugin message
    assert plugin.outbound_ledger.get_recent_entries(umo) == []

    # Step 2: NapCat echoes private poke with user_id=1001 AND target_id=1001 (NOT bot_42!)
    echo_ev = MockEvent("", sender_id="1001", group_id="", message_id="dm-poke-echo", self_id="bot_42", components=[Poke("1001")])
    echo_ev.unified_msg_origin = umo
    echo_ev.get_platform_name = lambda: "aiocqhttp"
    echo_ev.bot = cq
    echo_ev.message_obj.raw_message = {
        "notice_type": "notify",
        "sub_type": "poke",
        "user_id": 1001,
        "target_id": 1001,
        "self_id": "bot_42",
        "raw_info": [
            {"type": "qq", "uid": "bot_42"},
            {"type": "nor", "txt": "揉了揉"},
            {"type": "qq", "uid": "1001"},
            {"type": "nor", "txt": "的脸，喵呜~"},
        ],
    }

    try:
        await plugin.on_private_message(echo_ev)
        await flush(plugin, echo_ev)
        await drain(plugin)

        # Must be completely blocked for BOTH chat_dynamics AND native AstrBot
        assert echo_ev._chat_dynamics_blocks_native is True
        assert echo_ev.is_stopped is True
        assert plugin._metrics.get("poke_self_echo_suppressed", 0) == 1
        assert bridge.requests == []
        assert echo_ev.replies_sent == []
        assert runtime.dag.get_node("dm-poke-echo") is None

        # Backfilled action_text is available in read-only outbound note
        note = plugin.outbound_ledger.format_recent_outbound_note(umo)
        assert "刚才你戳了对方（实际展示动作：揉了揉...的脸，喵呜~）" in note

        # Step 3: Real user poking the bot back MUST still be processed normally!
        real_poke = MockEvent("", sender_id="1001", group_id="", message_id="dm-poke-real", self_id="bot_42", components=[Poke("bot_42")])
        real_poke.unified_msg_origin = umo
        real_poke.get_platform_name = lambda: "aiocqhttp"
        real_poke.bot = cq
        await plugin.on_private_message(real_poke)
        await flush(plugin, real_poke)
        await drain(plugin)

        assert len(bridge.requests) == 1
        assert len(real_poke.replies_sent) == 1
        assert runtime.dag.get_node("dm-poke-real") is not None
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_group_self_poke_echo_suppressed_and_backfilled_e2e(monkeypatch):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    plugin = _plugin({
        "decision_mode": "persona_model",
        "pipeline_mode": "filter",
        "base_thinking_delay": 0,
        "daily_rhythm_enabled": False,
    })
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge

    group_id = "987654321"
    umo = f"aiocqhttp:GroupMessage:{group_id}"
    plugin._get_or_create_runtime(umo, group_id=group_id, umo=umo, bot_id="bot_42")
    plugin.record_bot_poke(umo, bot_id="bot_42", target_id="1001")

    # Standard group self-poke echo where sender_id == bot_id
    echo_ev = MockEvent(
        "",
        sender_id="bot_42",
        group_id=group_id,
        message_id="grp-poke-echo-1",
        self_id="bot_42",
        components=[Poke("1001")],
    )
    echo_ev.unified_msg_origin = umo
    echo_ev.message_obj.raw_message = {
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": int(group_id),
        "user_id": "bot_42",
        "target_id": "1001",
        "action": "轻轻拍了拍",
        "suffix": "的脑袋",
    }

    try:
        await plugin.on_group_message(echo_ev)
        await flush(plugin, echo_ev)
        await drain(plugin)

        assert echo_ev._chat_dynamics_blocks_native is True
        assert echo_ev.is_stopped is True
        assert plugin._metrics.get("poke_self_echo_suppressed", 0) == 1
        assert bridge.requests == []
        assert echo_ev.replies_sent == []
        assert plugin._sessions[umo].dag.get_node("grp-poke-echo-1") is None

        note = plugin.outbound_ledger.format_recent_outbound_note(umo)
        assert "刚才你戳了对方（实际展示动作：轻轻拍了拍...的脑袋）" in note

        # Anomalous group echo where sender_id == target_id right after bot poke
        plugin.record_bot_poke(umo, bot_id="bot_42", target_id="2002")
        anomalous_ev = MockEvent(
            "",
            sender_id="2002",
            group_id=group_id,
            message_id="grp-poke-echo-2",
            self_id="bot_42",
            components=[Poke("2002")],
        )
        anomalous_ev.unified_msg_origin = umo
        anomalous_ev.message_obj.raw_message = {
            "action": "戳了戳",
            "suffix": "的小酒窝",
        }
        await plugin.on_group_message(anomalous_ev)
        assert anomalous_ev.is_stopped is True
        assert plugin._metrics.get("poke_self_echo_suppressed", 0) == 2
        note2 = plugin.outbound_ledger.format_recent_outbound_note(umo)
        assert "刚才你戳了对方（实际展示动作：戳了戳...的小酒窝）" in note2
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_failed_send_poke_cancels_pre_recorded_echo_slot(private_plugin):
    plugin, _bridge = private_plugin
    umo = "aiocqhttp:FriendMessage:3003"
    cq = _FakeCQHttpClient(fail_next=True)
    ev = private_event("hi", sender="3003", message_id="dm-fail-poke", umo=umo)
    ev.get_platform_name = lambda: "aiocqhttp"
    ev.bot = cq
    runtime = plugin._get_or_create_runtime(umo, group_id="", umo=umo, bot_id="bot_42", scope_kind="private")

    try:
        res = await plugin._send_owned(runtime, ev, build_poke_chain("3003"))
        assert res.success is False
        # Failed poke must not leave a stale pending echo slot behind
        assert (
            plugin.outbound_ledger.match_and_consume_self_poke_echo(
                umo,
                sender_id="3003",
                target_id="3003",
                bot_id="bot_42",
                scope="private",
            )
            is False
        )
    finally:
        await plugin.terminate()


# ---------------------------------------------------------------------------
# 4. platform_bridge action-marker helpers
# ---------------------------------------------------------------------------


def test_format_delivered_action_marker_and_summary():
    assert format_delivered_action_marker("poke") == "[动作: 戳了对方]"
    assert format_delivered_action_marker("poke", target_label="小明") == "[动作: 戳了 小明]"
    assert format_delivered_action_marker("meme") == "[动作: 发了一张表情]"
    assert format_delivered_action_marker("meme", detail="猫猫探头") == "[动作: 发了一张表情（猫猫探头）]"
    assert format_delivered_action_marker("image", detail="晚霞") == "[动作: 发了一张图片（晚霞）]"

    assert (
        format_delivered_action_summary("醒着呢，这么晚还不睡。", actions=[format_delivered_action_marker("poke")])
        == "[动作: 戳了对方]\n醒着呢，这么晚还不睡。"
    )
    assert format_delivered_action_summary("", actions=[format_delivered_action_marker("poke")]) == "[动作: 戳了对方]"
    assert format_delivered_action_summary("纯文字", actions=()) == "纯文字"
    assert format_delivered_action_summary("", actions=()) == ""
