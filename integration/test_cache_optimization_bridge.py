"""Real-SDK integration tests for CD-1, CD-2, CD-3, CD-4 through AstrBotAgentBridge."""

from __future__ import annotations

import base64
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
    assert "本轮回应方式" in system
    assert "发送表情只能用本轮附带的候选" in system


async def _async_return(value):
    return value
