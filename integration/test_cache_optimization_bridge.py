"""Real-SDK integration tests for CD-1, CD-2, CD-3, CD-4 through AstrBotAgentBridge."""

from __future__ import annotations

import base64
import json
from collections import deque
from types import SimpleNamespace

import pytest

from .test_persona_agent import host_fixture as host_fixture
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Image
from astrbot.core.pipeline import context_utils
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.narrative import _narrative_scope_key
from astrbot_plugin_chat_dynamics.core.turn_decision import (
    MessageSnapshot,
    TurnContext,
    TurnDecision,
    reply_prompt,
)
from astrbot_plugin_chat_dynamics.core.vision_context import MAIN_VISION_HINT


def _managed_payload(event, persona, *, purpose="reply", internal=False):
    scope = {"platform": "test", "umo": event.unified_msg_origin, "persona_id": "quiet", "instance_id": "quiet"}
    return {
        "scope": scope,
        "purpose": purpose,
        "internal": internal,
        "effective_core_version": "v1",
        "speaker_id": "u",
        "captured_at": 100,
        "character": {"core_prompt": persona.prompt, "group_rules": "群规稳定", "fingerprint": "stable"},
        "snapshot": {"managed": True, "availability": "available", "revision": 1},
        "host_history_snapshot": {
            "conversation_id": persona.conversation_id,
            "snapshot_fingerprint": "fixed",
            "rows": [],
        },
        "evidence_view": {
            "schema_version": 1,
            "available": True,
            "purpose": purpose,
            "card_version": "v1",
            "scope_key": _narrative_scope_key(scope),
            "correction_revision": 0,
            "atoms": [],
            "slots": {},
            "history_projection": {},
            "revocations": [],
        },
    }


def _extract_system(call):
    system = str(call.get("system_prompt") or "")
    for row in call.get("contexts", ()):
        dumped = row if isinstance(row, dict) else row.model_dump()
        if dumped.get("role") == "system" and isinstance(dumped.get("content"), str):
            system += dumped["content"]
    return system


def _extract_user_text(call):
    chunks = []
    for row in call.get("contexts", ()):
        dumped = row if isinstance(row, dict) else row.model_dump()
        if dumped.get("role") != "user":
            continue
        content = dumped.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(str(part.get("text") or ""))
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_multiturn_system_prompt_byte_identity_with_visual_media_contract(host_fixture, monkeypatch, tmp_path):
    ctx, event, calls = host_fixture
    ctx.conv.history = "[]"

    vm_candidates = []

    async def visual_media_static_hook(incoming, _kind, req):
        req.system_prompt += "\n[本轮统一视觉表达] 静态规则\n[本轮回应方式] 静态协议"
        incoming.set_extra("visual_media_policy_v1", True)
        incoming.set_extra("_visual_media_static_policy_v1", True)
        incoming._stealer_joint_meme_plan_v1 = SimpleNamespace(
            _reply_actions_v1=True,
            candidates=list(vm_candidates),
            resolve=lambda raw: (raw, []),
        )
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", visual_media_static_hook)
    ctx.provider.text_chat = lambda **kwargs: (
        calls.append(kwargs)
        or _async_return(LLMResponse(role="assistant", completion_text='{"reply_mode":"text","text":"收到","meme_id":null,"narration":""}'))
    )
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)

    # Turn 1: plain text, 0 candidates
    event._extras["_narrative_managed_v1"] = _managed_payload(event, persona, purpose="reply")
    t1 = TurnContext(event.unified_msg_origin, "u", "你好呀", (MessageSnapshot("m1", "u", "你好呀"),), (), 0, 0, 0, True)
    p1 = reply_prompt(t1, TurnDecision.fallback(t1, "at"))
    notifier = SimpleNamespace(evidence_current=lambda _: True)
    await bridge.generate(event, (event,), p1, persona, "provider", narrative_override=notifier)

    # Turn 2: history_quote + 3 meme candidates
    vm_candidates[:] = [{"id": 1}, {"id": 2}, {"id": 3}]
    event._extras["_narrative_managed_v1"] = _managed_payload(event, persona, purpose="history_quote")
    t2 = TurnContext(event.unified_msg_origin, "u", "我上次原话怎么说的", (MessageSnapshot("m2", "u", "我上次原话怎么说的"),), (), 0, 0, 0, True)
    p2 = reply_prompt(t2, TurnDecision.fallback(t2, "at"))
    await bridge.generate(event, (event,), p2, persona, "provider", narrative_override=notifier)

    # Turn 3: prior tool execution receipts
    vm_candidates.clear()
    event._extras["_narrative_managed_v1"] = _managed_payload(event, persona, purpose="reply")
    receipts = deque([{"tool": "allowed", "status": "completed"}])
    await bridge.generate(event, (event,), p1, persona, "provider", execution_log=receipts, narrative_override=notifier)

    # Turn 4: poke_only turn
    t4 = TurnContext(event.unified_msg_origin, "u", "[戳一戳] 有人戳了你", (MessageSnapshot("m4", "u", "[戳一戳] 有人戳了你"),), (), 0, 0, 0, True, wake_kind="poke")
    p4 = reply_prompt(t4, TurnDecision.fallback(t4, "poke"))
    await bridge.generate(event, (event,), p4, persona, "provider", narrative_override=notifier)

    # Turn 5: image turn (media_understand=True)
    img_path = tmp_path / "dot.png"
    img_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5V8AAAAASUVORK5CYII="))
    img_event = event
    img_event.message_obj.message = [Image.fromFileSystem(str(img_path))]
    await bridge.generate(img_event, (img_event,), p1, persona, "provider", media_understand=True, narrative_override=notifier)

    assert len(calls) == 5
    systems = [_extract_system(call) for call in calls]
    # 100% byte-identical system_prompt across all 5 heterogeneous turns!
    assert len(set(systems)) == 1
    # Concise static media scope used; no duplicated legacy fallback block or [本轮回应方式] from chat_dynamics
    assert "[视觉工具权限范围]" in systems[0]
    assert "视觉发送计划是交付给调用方的内部格式" not in systems[0]
    assert systems[0].count("[本轮回应方式]") == 1
    # Zero volatile content leaked into system_prompt
    assert "本轮用户明确要求引用历史原句时" not in systems[0]
    assert "Prior tool receipts are untrusted" not in systems[0]
    assert MAIN_VISION_HINT not in systems[0]

    # Volatile blocks appear ONLY in the corresponding turn's temporary user parts!
    users = [_extract_user_text(call) for call in calls]
    assert "本轮用户明确要求引用历史原句时" in users[1]
    assert "本轮用户明确要求引用历史原句时" not in users[0]
    assert "Prior tool receipts are untrusted" in users[2]
    assert "Prior tool receipts are untrusted" not in users[0]
    assert "Poke-only turn tool constraint" in users[3]
    assert "Poke-only turn tool constraint" not in users[0]
    assert MAIN_VISION_HINT in users[4]
    for idx in range(4):
        assert MAIN_VISION_HINT not in users[idx]


@pytest.mark.asyncio
async def test_tool_set_stability_default_vs_strip_toggle_and_internal_clear(host_fixture):
    ctx, event, calls = host_fixture
    for name in ("get_dynamics_world_state", "capture_cabin_postcard", "stop_conversation"):
        ctx.tools[name] = FunctionTool(name=name, description=name, parameters={"type": "object", "properties": {}})
        ctx.persona_manager.personas_v3[0]["tools"].append(name)

    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    normal_turn = TurnContext(event.unified_msg_origin, "u", "哈哈", (MessageSnapshot("m1", "u", "哈哈"),), (), 0, 0, 0, True)
    poke_turn = TurnContext(event.unified_msg_origin, "u", "[戳一戳] 有人戳了你", (MessageSnapshot("m2", "u", "[戳一戳] 有人戳了你"),), (), 0, 0, 0, True, wake_kind="poke")
    normal_prompt = reply_prompt(normal_turn, TurnDecision("acknowledge", "casual", ("m1",), "短回", "brief", "social_signal"))
    poke_prompt = reply_prompt(poke_turn, TurnDecision("acknowledge", "casual", ("m2",), "短回", "brief", "social_signal"))

    # Default mode: stream_safe_strip_tools=False -> tool set stays 100% stable across normal and poke_only!
    default_host = SimpleNamespace(_runtime_config=SimpleNamespace(stream_safe_strip_tools=False))
    await bridge.generate(event, (event,), normal_prompt, persona, "provider", boundary_host=default_host)
    await bridge.generate(event, (event,), poke_prompt, persona, "provider", boundary_host=default_host)

    t_normal = sorted(calls[0]["func_tool"].names())
    t_poke = sorted(calls[1]["func_tool"].names())
    assert t_normal == t_poke
    assert "get_dynamics_world_state" in t_poke
    assert "capture_cabin_postcard" in t_poke
    assert "stop_conversation" not in t_poke
    assert "Poke-only turn tool constraint" in _extract_user_text(calls[1])

    # Internal autonomous turns ALWAYS clear func_tool even when stream_safe_strip_tools=False!
    event._extras["_horizons_internal_v1"] = True
    await bridge.generate(event, (event,), normal_prompt, persona, "provider", boundary_host=default_host)
    assert calls[2]["func_tool"] is None
    event._extras.pop("_horizons_internal_v1", None)

    # Opt-in toggle: stream_safe_strip_tools=True -> preserves old poke_only world-tool stripping!
    strip_host = SimpleNamespace(_runtime_config=SimpleNamespace(stream_safe_strip_tools=True))
    await bridge.generate(event, (event,), poke_prompt, persona, "provider", boundary_host=strip_host)
    t_poke_stripped = sorted(calls[3]["func_tool"].names())
    assert "get_dynamics_world_state" not in t_poke_stripped
    assert "capture_cabin_postcard" not in t_poke_stripped
    assert "Poke-only turn tool constraint" not in _extract_user_text(calls[3])

    # build_config tool_schema_mode stability: default keeps host mode stable; strip_tools=True uses skills_like on social turns
    social_event = SimpleNamespace(
        unified_msg_origin=event.unified_msg_origin,
        _extras={},
        message_obj=SimpleNamespace(message=[SimpleNamespace(text=normal_prompt)]),
    )
    cfg_stable = bridge.build_config(social_event, strip_tools=False)
    cfg_stripped = bridge.build_config(social_event, strip_tools=True)
    if hasattr(cfg_stable, "tool_schema_mode"):
        assert cfg_stable.tool_schema_mode != "skills_like"
        assert cfg_stripped.tool_schema_mode == "skills_like"


@pytest.mark.asyncio
async def test_stream_safe_tool_stripping_respects_config_toggle(host_fixture, monkeypatch):
    from unittest.mock import AsyncMock

    from .test_persona_agent import PrivateEvent

    ctx, _group_event, calls = host_fixture
    event = PrivateEvent()
    event.message_str = "晚安"
    ctx.conv.history = "[]"

    async def allow_stream_hook(incoming, _kind, _req):
        incoming._cd_reply_modes_v1 = False
        return False

    from astrbot_plugin_chat_dynamics.core import agent_bridge as agent_bridge_mod

    monkeypatch.setattr(context_utils, "call_event_hook", allow_stream_hook)
    monkeypatch.setattr(agent_bridge_mod, "_require_native_source_guard", lambda *_a, **_kw: (None, False))

    async def fake_stream(**kwargs):
        calls.append(kwargs)
        yield LLMResponse(role="assistant", completion_text="晚安。")

    ctx.provider.text_chat_stream = fake_stream
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)

    # Default stream_safe_strip_tools=False: keeps func_tool intact even with on_partial_text
    default_host = SimpleNamespace(_runtime_config=SimpleNamespace(stream_safe_strip_tools=False))
    await bridge.generate(event, (event,), "晚安", persona, "provider", boundary_host=default_host, on_partial_text=AsyncMock())
    assert calls[0]["func_tool"] is not None

    # Opt-in stream_safe_strip_tools=True: clears func_tool on stream_safe fast path
    strip_host = SimpleNamespace(_runtime_config=SimpleNamespace(stream_safe_strip_tools=True))
    await bridge.generate(event, (event,), "晚安", persona, "provider", boundary_host=strip_host, on_partial_text=AsyncMock())
    assert calls[1]["func_tool"] is None


@pytest.mark.asyncio
async def test_live_gpt_visual_policy_tuple_and_tool_ordering_and_scope_guard(host_fixture, monkeypatch):
    """Verify preserved live GPT features work alongside CD-1/CD-2 zero-jitter guarantees."""
    from astrbot_plugin_chat_dynamics.core.agent_bridge import AgentBridgeUnavailable

    ctx, event, calls = host_fixture
    ctx.conv.history = "[]"
    # Register tools out of alphabetical order to verify deterministic sorting via _order_request_tools.
    for name in ("zeta_tool", "alpha_tool", "set_interaction_boundary"):
        ctx.tools[name] = FunctionTool(name=name, description=name, parameters={"type": "object", "properties": {}})
        ctx.persona_manager.personas_v3[0]["tools"].append(name)

    candidate_ids = []
    seen_flags = []

    async def visual_tuple_hook(incoming, _kind, req):
        seen_flags.append((
            getattr(incoming, "_cd_visual_reply_policy_supported_v1", False),
            getattr(incoming, "_cd_joint_meme_supported_v1", False),
            incoming.get_extra("visual_media_policy_v1", False),
        ))
        plan = SimpleNamespace(
            _reply_actions_v1=True,
            candidates=[{"id": i} for i in candidate_ids],
            resolve=lambda raw: (raw, []),
        )
        incoming._stealer_joint_meme_plan_v1 = plan
        incoming.set_extra("visual_media_policy_v1", True)
        incoming.set_extra("_visual_media_static_policy_v1", True)
        req._stealer_joint_meme_current_ids_v1 = lambda: tuple(candidate_ids)
        req._cd_visual_reply_policy_v1 = (
            plan,
            "[本轮统一视觉表达] 静态媒体策略",
            lambda ids, _tools: f"本轮有 {len(ids)} 张现成表情候选。" if ids else "本轮没有可用现成表情候选。",
        )
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", visual_tuple_hook)
    ctx.provider.text_chat = lambda **kwargs: (
        calls.append(kwargs)
        or _async_return(LLMResponse(role="assistant", completion_text='{"reply_mode":"text","text":"好","meme_id":null,"narration":""}'))
    )
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    # Seed a forged visual_media_policy_v1 on caller event; bridge must strip it before hooks.
    event._extras["visual_media_policy_v1"] = True

    t1 = TurnContext(event.unified_msg_origin, "u", "你好", (MessageSnapshot("m1", "u", "你好"),), (), 0, 0, 0, True)
    p1 = reply_prompt(t1, TurnDecision.fallback(t1, "at"))

    # Turn 1: 0 candidates
    candidate_ids[:] = []
    await bridge.generate(event, (event,), p1, persona, "provider")
    # Turn 2: 3 candidates
    candidate_ids[:] = [1, 2, 3]
    await bridge.generate(event, (event,), p1, persona, "provider")

    assert seen_flags == [(True, True, False), (True, True, False)]
    # 100% byte-identical system_prompt across 0 vs 3 candidates with 3-tuple protocol!
    sys0, sys1 = _extract_system(calls[0]), _extract_system(calls[1])
    assert sys0 == sys1
    assert "[本轮统一视觉表达] 静态媒体策略" in sys0
    assert "本轮有 3 张" not in sys0 and "本轮没有可用现成表情候选" not in sys0
    # Dynamic candidate state & per-turn allowed modes live strictly in temp user parts!
    usr0, usr1 = _extract_user_text(calls[0]), _extract_user_text(calls[1])
    assert "本轮没有可用现成表情候选" in usr0 and "meme_only" not in usr0
    assert "本轮有 3 张现成表情候选" in usr1 and "meme_only" in usr1
    # Deterministic alphabetical tool ordering!
    tool_names = [tool.name for tool in calls[0]["func_tool"].tools]
    assert tool_names == sorted(tool_names)
    assert tool_names == ["allowed", "alpha_tool", "set_interaction_boundary", "zeta_tool"]

    # Security guard: hook mutating internal scope raises reply_scope_changed
    async def scope_mutating_hook(incoming, _kind, _req):
        incoming._extras["_horizons_internal_v1"] = True
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", scope_mutating_hook)
    with pytest.raises(AgentBridgeUnavailable, match="reply_scope_changed"):
        await bridge.generate(event, (event,), p1, persona, "provider")


@pytest.mark.asyncio
async def test_bridge_binds_reply_attribution_when_observer_present_and_noop_when_absent(host_fixture):
    from contextlib import contextmanager

    ctx, event, calls = host_fixture
    turn_id = "abcdef0123456789abcdef0123456789"
    event._extras["_cd_pipeline_turn_id_v1"] = turn_id
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    t1 = TurnContext(event.unified_msg_origin, "u", "你好", (MessageSnapshot("m1", "u", "你好"),), (), 0, 0, 0, True)
    p1 = reply_prompt(t1, TurnDecision.fallback(t1, "at"))

    # Case 1: stock provider without _series_attributed_call_v1 -> strict no-op, reply succeeds.
    out_plain = await bridge.generate(event, (event,), p1, persona, "provider")
    assert out_plain.text and len(calls) == 1

    # Case 2: provider exposing _series_attributed_call_v1 + managed HistoryViewProvider wrapper chain.
    observed = []

    @contextmanager
    def observe(caller, *, scope, operation_id):
        observed.append((caller, scope, operation_id))
        yield

    ctx.provider._series_attributed_call_v1 = observe
    event._extras["_narrative_managed_v1"] = _managed_payload(event, persona, purpose="reply")
    event._extras["_narrative_managed_v1"]["turn_id"] = turn_id
    ctx.conv.history = "[]"
    notifier = SimpleNamespace(evidence_current=lambda _: True)
    out_obs = await bridge.generate(event, (event,), p1, persona, "provider", narrative_override=notifier)
    assert out_obs.text and len(calls) == 2
    assert observed == [("chat_dynamics.reply", event.unified_msg_origin, turn_id)]


@pytest.mark.asyncio
async def test_legacy_visual_media_without_static_flag_gets_full_fallback(host_fixture, monkeypatch):
    ctx, event, calls = host_fixture

    async def legacy_visual_media_hook(incoming, _kind, req):
        incoming.set_extra("visual_media_policy_v1", True)
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", legacy_visual_media_hook)
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    t1 = TurnContext(event.unified_msg_origin, "u", "你好", (MessageSnapshot("m1", "u", "你好"),), (), 0, 0, 0, True)
    p1 = reply_prompt(t1, TurnDecision.fallback(t1, "at"))
    await bridge.generate(event, (event,), p1, persona, "provider")
    system = _extract_system(calls[0])
    assert "[已开启的视觉表达权限]" in system
    assert "[回应方式协议]" in system
    assert "发送表情只能用本轮附带的候选" in system


@pytest.mark.asyncio
async def test_phase2_isolate_volatile_system_tail_across_minutes_speakers_and_tools(host_fixture, monkeypatch):
    ctx, event, calls = host_fixture
    from astrbot.core.agent.tool import FunctionTool, ToolSet

    def make_tool(name):
        class _T(FunctionTool):
            async def call(self, context, **kwargs):
                return "ok"

        return _T(name=name, description=name, parameters={"type": "object", "properties": {}})

    turn_idx = {"n": 0}
    auto_life = "忙闲与时效只用于把握回应长短和时机。直接回应当前话题。"
    vm_static = "[本轮统一视觉表达]\n结合当前发言主动选择表达载体。"

    async def noisy_third_party_hooks(_incoming, _kind, req):
        n = turn_idx["n"]
        turn_idx["n"] += 1
        # Simulate host/hook datetime + static series blocks + volatile third-party tails
        req.system_prompt += (
            f"\nCurrent datetime: 2026-11-20 01:0{n} (CST), Weekday: Friday"
            f"\n{auto_life}"
            f"\n[Portrayal]\n当前发言者：Speaker_{n}（兴趣=Topic_{n}）"
            f"\n\n{vm_static}"
            f"\n\n[自主屏蔽状态]\nSpeaker_{n} 警告={n}"
            f"\n\n[Worldbook 动态场景]\n动态场景词=Scene_{n}"
        )
        # Also fluctuate set_interaction_boundary tool availability across turns
        if n % 2 == 0:
            req.func_tool = ToolSet([make_tool("alpha_tool"), make_tool("set_interaction_boundary")])
        else:
            req.func_tool = ToolSet([make_tool("alpha_tool")])
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", noisy_third_party_hooks)
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    host_default = SimpleNamespace(_runtime_config=SimpleNamespace(isolate_volatile_system_tail=True))

    for idx in range(3):
        turn = TurnContext(
            event.unified_msg_origin,
            f"u{idx}",
            f"你好第{idx}轮",
            (MessageSnapshot(f"m{idx}", f"u{idx}", f"你好第{idx}轮"),),
            (),
            0,
            0,
            0,
            True,
        )
        prompt = reply_prompt(turn, TurnDecision.fallback(turn, "at"), reply_guidance="保持温和自然的群聊口吻。")
        output = await bridge.generate(event, (event,), prompt, persona, "provider", boundary_host=host_default)
        assert await bridge.commit(event, output, "delivered")

    sys_prompts = [_extract_system(call) for call in calls]
    # 100% byte-identical system_prompt across minutes, speakers, third-party tails, and boundary tool flips!
    assert sys_prompts[0] == sys_prompts[1] == sys_prompts[2]
    # Static series blocks + hoisted reply_guidance + BOUNDARY_AUTHORIZATION stay cached in system_prompt
    assert auto_life in sys_prompts[0]
    assert vm_static in sys_prompts[0]
    assert "交互边界授权" in sys_prompts[0]
    assert "保持温和自然的群聊口吻。" in sys_prompts[0]
    # Zero volatile datetime / speaker / dynamic worldbook leakage into system_prompt
    for token in ("01:00", "01:01", "01:02", "Speaker_0", "Speaker_1", "Scene_0", "Scene_2", "Portrayal"):
        assert token not in sys_prompts[0]
        assert token not in sys_prompts[1]
        assert token not in sys_prompts[2]
    # All volatile context is preserved in contexts (temp user parts) and NEVER persisted to ctx.saved
    for idx in range(3):
        ctx_text = _extract_user_text(calls[idx])
        assert f"01:0{idx}" in ctx_text
        assert f"Speaker_{idx}" in ctx_text
        assert f"Scene_{idx}" in ctx_text
        assert f"警告={idx}" in ctx_text
    assert "Portrayal" not in str(ctx.saved)
    assert "Scene_0" not in str(ctx.saved)

    # Opt-out toggle: isolate_volatile_system_tail=False keeps third-party tail in system_prompt
    host_off = SimpleNamespace(_runtime_config=SimpleNamespace(isolate_volatile_system_tail=False))
    turn = TurnContext(event.unified_msg_origin, "u9", "关闭隔离测试", (MessageSnapshot("m9", "u9", "关闭隔离测试"),), (), 0, 0, 0, True)
    prompt = reply_prompt(turn, TurnDecision.fallback(turn, "at"))
    await bridge.generate(event, (event,), prompt, persona, "provider", boundary_host=host_off)
    assert "Speaker_3" in _extract_system(calls[-1])


@pytest.mark.asyncio
async def test_phase2_bridge_curb_dedup_shrinks_payload_with_valid_provenance(host_fixture):
    ctx, event, calls = host_fixture
    repeated_msgs = [
        f"历史群聊第{i}条较长的完整讨论内容，包含了详细的排查步骤、日志现象、回滚方案以及各模块配置参数说明，用于验证当前回合上下文去重后显著减小未缓存体积。"
        for i in range(6)
    ]
    quoted_msg = "这是当前发言直接引用的那条背景记录，长度超过十六字且必须原样保留正文。"
    # Seed real AstrBot conversation history with those 6 turns
    seeded = []
    for i, text in enumerate(repeated_msgs):
        seeded.append({"role": "user", "content": text})
        seeded.append({"role": "assistant", "content": f"收到第{i}条的回复说明，内容也保留在历史里。"})
    ctx.conv.history = json.dumps(seeded, ensure_ascii=False)

    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    event._extras["_narrative_managed_v1"] = _managed_payload(event, persona, purpose="reply")
    notifier = SimpleNamespace(evidence_current=lambda _: True)

    background = tuple(MessageSnapshot(f"bg-{i}", "u2", text) for i, text in enumerate(repeated_msgs)) + (
        MessageSnapshot("bg-q", "u3", quoted_msg),
    )
    turn = TurnContext(
        event.unified_msg_origin,
        "u1",
        "接着刚才聊的往下说",
        (MessageSnapshot("m-cur", "u1", "接着刚才聊的往下说", reply_to="bg-q"),),
        background,
        0,
        0,
        0,
        True,
    )
    raw_prompt = reply_prompt(turn, TurnDecision.fallback(turn, "at"), reply_guidance="自然简洁回应，不要客套。" * 6)
    await bridge.generate(event, (event,), raw_prompt, persona, "provider", narrative_override=notifier)

    assert len(calls) == 1
    system = _extract_system(calls[0])
    assert "自然简洁回应，不要客套。" in system

    # Find the final user message JSON sent to the provider
    user_texts = _extract_user_text(calls[0])
    assert '"in_history":true' in user_texts
    assert quoted_msg in user_texts
    # Measure actual compacted curB JSON vs uncompacted raw_prompt
    last_msg = calls[0]["contexts"][-1]
    parts = last_msg["content"] if isinstance(last_msg["content"], list) else [last_msg["content"]]
    sent_json = next(
        (p["text"] if isinstance(p, dict) else p)
        for p in parts
        if isinstance((p["text"] if isinstance(p, dict) else p), str)
        and (p["text"] if isinstance(p, dict) else p).startswith('{"conversation":')
    )
    assert len(sent_json.encode("utf-8")) < len(raw_prompt.encode("utf-8")) * 0.60
    parsed_sent = json.loads(sent_json)
    assert "reply_guidance" not in parsed_sent
    assert all(
        row.get("in_history") is True and "text" not in row
        for row in parsed_sent["conversation"]["background"]
        if row["message_id"].startswith("bg-") and row["message_id"] != "bg-q"
    )
    # Material payload provenance verified inside HistoryViewProvider
    assert "角色状态与有来源的背景数据：" in user_texts


async def _async_return(value):
    return value
