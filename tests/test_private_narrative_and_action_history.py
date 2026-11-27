"""Tests for private narrative pipeline, action history persistence, outbound notes, and sleep wake-up merge."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.api.event import MessageChain

try:
    from astrbot.api.message_components import At, Image, Plain
except ImportError:
    class Plain:  # type: ignore[no-redef]
        def __init__(self, text: str = ""):
            self.text = text

    class Image:  # type: ignore[no-redef]
        def __init__(self, file: str = "", url: str = "", summary: str = ""):
            self.file = file
            self.url = url
            if summary:
                self.summary = summary

    class At:  # type: ignore[no-redef]
        def __init__(self, qq: str = ""):
            self.qq = qq

try:
    from astrbot.core.platform.message_type import MessageType
except ImportError:
    class MessageType:  # type: ignore[no-redef]
        FRIEND_MESSAGE = "FRIEND_MESSAGE"
        GROUP_MESSAGE = "GROUP_MESSAGE"

from astrbot_plugin_chat_dynamics.core.agent_bridge import AgentOutput, AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.narrative import MANAGED_KEY, NarrativeCoordinator
from astrbot_plugin_chat_dynamics.core.natural_rhythm import IngressPacingDecision
from astrbot_plugin_chat_dynamics.core.persona_engine import (
    _join_history_delivered,
)
from astrbot_plugin_chat_dynamics.core.turn_decision import PersonaSnapshot
from .test_persona_model import BridgeDouble, drain, flush
from .test_plugin_lifecycle import MockEvent, _plugin


def _private_friend_event(
    text: str = "这么晚还不睡呀",
    *,
    sender: str = "1001",
    message_id: str = "dm-1",
    platform: str = "aiocqhttp",
    components=None,
):
    event = MockEvent(text, sender_id=sender, group_id="", message_id=message_id, components=components)
    event.unified_msg_origin = f"{platform}:FriendMessage:{sender}"
    event.get_platform_name = lambda: platform
    event.get_message_type = lambda: MessageType.FRIEND_MESSAGE
    event.get_self_id = lambda: "bot_42"
    event.get_sender_id = lambda: sender
    event.get_group_id = lambda: ""
    event._extras = {}
    event.get_extra = lambda k, d=None: event._extras.get(k, d)
    event.set_extra = lambda k, v: event._extras.__setitem__(k, v)
    return event


def _make_narrative_providers(*, availability: str = "available", umo: str = "aiocqhttp:FriendMessage:1001"):
    scope = {
        "platform": "aiocqhttp",
        "umo": umo,
        "persona_id": "persona_v1",
        "instance_id": "persona_v1",
    }
    worldbook = SimpleNamespace(
        get_character_context=AsyncMock(
            return_value={
                "schema_version": 1,
                "available": True,
                "disabled": False,
                "card_id": "card-1",
                "name": "小夜",
                "persona_id": "persona_v1",
                "instance_id": "persona_v1",
                "fingerprint": "fp-card-1",
                "core_prompt": "你是小夜，说话温柔带一点傲娇。",
                "group_rules": "",
                "scope": dict(scope),
            }
        )
    )
    life = SimpleNamespace(
        get_snapshot=AsyncMock(
            return_value={
                "schema_version": 1,
                "managed": True,
                "paused": False,
                "availability": availability,
                "activity": "正在睡觉" if availability == "asleep" else "在看书",
                "revision": 7,
                "scope_revision": 2,
                "stale": False,
            }
        ),
        advance=AsyncMock(return_value={}),
    )
    livingmemory = SimpleNamespace(
        recall_context=AsyncMock(
            return_value={
                "schema_version": 1,
                "available": True,
                "evidence_status": "ready",
                "items": [],
                "evidence_view": None,
            }
        ),
        get_evidence_view=AsyncMock(return_value=None),
        peek_evidence_revision=lambda _scope: {
            "schema_version": 1,
            "available": True,
            "correction_revision": 0,
        },
    )
    self_learning = SimpleNamespace(
        get_social_snapshot=AsyncMock(return_value={"schema_version": 1, "available": True, "items": []}),
        get_social_context=AsyncMock(
            return_value={
                "schema_version": 1,
                "available": True,
                "items": [
                    {
                        "source_id": "sl-1",
                        "scope": dict(scope),
                        "text": "熟悉的老朋友，喜欢半夜聊天",
                        "fact_kind": "observation",
                    }
                ],
            }
        ),
    )
    portrayal = SimpleNamespace(
        get_profiles=AsyncMock(
            return_value={
                "schema_version": 1,
                "available": True,
                "items": [
                    {
                        "source_id": "prof-1",
                        "scope": dict(scope),
                        "text": "用户1001：夜猫子程序员",
                        "fact_kind": "profile",
                    }
                ],
            }
        )
    )
    return {
        "worldbook": worldbook,
        "dynamic_life_state": life,
        "livingmemory": livingmemory,
        "self_learning": self_learning,
        "portrayal": portrayal,
    }


def _install_narrative_providers(plugin, providers: dict):
    orig = getattr(plugin.integrations, "narrative_provider", None)
    plugin.integrations.narrative_provider = lambda name: providers.get(name) or (
        orig(name) if callable(orig) else None
    )


@pytest.mark.asyncio
async def test_private_direct_runs_full_narrative_prepare_and_passes_override(monkeypatch, tmp_path):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    plugin = _plugin(
        {
            "decision_mode": "persona_model",
            "decision_backend": "jev",
            "private_chat_enabled": True,
            "narrative_enabled": True,
            "narrative_private_chat_enabled": True,
            "base_thinking_delay": 0,
            "debounce_base_cooldown": 10,
            "daily_rhythm_enabled": False,
            "natural_reply_rhythm_enabled": False,
        }
    )
    plugin.narrative = NarrativeCoordinator(plugin, tmp_path / "narrative.sqlite3")
    providers = _make_narrative_providers(availability="available", umo="aiocqhttp:FriendMessage:1001")
    _install_narrative_providers(plugin, providers)

    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge
    plugin.persona_engine.decide = AsyncMock(side_effect=AssertionError("private_direct must not call decide()"))
    observe_spy = AsyncMock()
    plugin.narrative.observe_private_turn = observe_spy

    prepare_phases: list[bool] = []
    orig_prepare = plugin.narrative.prepare

    async def spy_prepare(runtime, item, persona, *, lightweight=False):
        prepare_phases.append(bool(lightweight))
        return await orig_prepare(runtime, item, persona, lightweight=lightweight)

    plugin.narrative.prepare = spy_prepare

    event = _private_friend_event("最近在干嘛呢？", sender="1001", message_id="dm-narr-1")
    try:
        await plugin.on_private_message(event)
        await flush(plugin, event)
        await drain(plugin)

        # Both lightweight decision-phase prepare and full reply-phase prepare ran
        assert prepare_phases == [True, False]
        providers["worldbook"].get_character_context.assert_awaited()
        providers["dynamic_life_state"].get_snapshot.assert_awaited()
        providers["livingmemory"].recall_context.assert_awaited_once()
        providers["self_learning"].get_social_context.assert_awaited_once()
        providers["portrayal"].get_profiles.assert_awaited_once()

        # Bound as narrative managed on the event
        managed = event._extras.get(MANAGED_KEY)
        assert isinstance(managed, dict)
        assert managed["scope"]["umo"] == "aiocqhttp:FriendMessage:1001"
        assert managed["character"]["name"] == "小夜"
        assert any(row.get("provider") == "portrayal" for row in managed["context_items"])

        # Private direct semantics preserved: decide() and duplicate observe_private_turn NOT called
        assert plugin.persona_engine.decide.await_count == 0
        observe_spy.assert_not_awaited()
        assert len(event.replies_sent) == 1
        assert len(bridge.requests) == 1
    finally:
        await plugin.narrative.close_io()
        await plugin.terminate()


@pytest.mark.asyncio
async def test_private_direct_passes_narrative_override_to_real_bridge_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    plugin = _plugin(
        {
            "decision_mode": "persona_model",
            "private_chat_enabled": True,
            "narrative_enabled": True,
            "narrative_private_chat_enabled": True,
            "base_thinking_delay": 0,
        }
    )
    plugin.narrative = NarrativeCoordinator(plugin, tmp_path / "narrative_override.sqlite3")
    captured: list[dict] = []

    async def fake_bridge_generate(self, event, events, prompt, persona, provider, **kwargs):
        captured.append(dict(kwargs))
        return AgentOutput("好呀", "conv-1", "[]", [])

    monkeypatch.setattr(AstrBotAgentBridge, "generate", fake_bridge_generate)
    ev = _private_friend_event("嗨", sender="1001", message_id="dm-ov-1")
    persona = PersonaSnapshot("fp", "conv-1", "persona_v1", "base")
    try:
        # When narrative_private_chat_enabled=True, narrative_override is plugin.narrative
        await plugin.persona_engine.generate(ev, (ev,), "hello", persona, "prov-1", private_direct=True)
        assert captured[-1]["narrative_override"] is plugin.narrative

        # When narrative_private_chat_enabled=False and event is not bound, narrative_override is None
        plugin._runtime_config = replace(plugin._runtime_config, narrative_private_chat_enabled=False)
        await plugin.persona_engine.generate(ev, (ev,), "hello", persona, "prov-1", private_direct=True)
        assert captured[-1]["narrative_override"] is None
    finally:
        await plugin.narrative.close_io()
        await plugin.terminate()


@pytest.mark.asyncio
async def test_narrative_managed_private_turn_prevents_double_recall_in_dynamics_memory(monkeypatch, tmp_path):
    # Verify NarrativeCoordinator.prepare_memory_request skips managed private events first
    plugin = _plugin(
        {
            "decision_mode": "persona_model",
            "private_chat_enabled": True,
            "narrative_enabled": True,
            "narrative_private_chat_enabled": True,
        }
    )
    coord = NarrativeCoordinator(plugin, tmp_path / "narrative_pmr.sqlite3")
    ev = _private_friend_event("还记得昨晚吗", sender="1001", message_id="dm-mem-1")
    ev._chat_dynamics_owned_request = True
    ev._cd_owned_original_input_v1 = True
    ev._extras[MANAGED_KEY] = {
        "scope": {
            "platform": "aiocqhttp",
            "umo": "aiocqhttp:FriendMessage:1001",
            "persona_id": "persona_v1",
            "instance_id": "persona_v1",
        },
        "turn_id": "t1",
        "bundle_id": "b1",
    }
    req = SimpleNamespace(prompt="还记得昨晚吗")
    try:
        receipt = await coord.prepare_memory_request(ev, req)
        assert receipt["status"] == "skipped"
        assert receipt["reason"] == "cd_owned_request"
    finally:
        await coord.close_io()
        await plugin.terminate()

    # Verify dynamics_memory's is_managed_event and EventHandler skip managed private turns cleanly
    dm_repo = Path(__file__).parent.parent.parent / "astrbot_plugin_dynamics_memory"
    if str(dm_repo.parent) not in sys.path:
        sys.path.insert(0, str(dm_repo.parent))
    # Load narrative_api directly so unit test works even without full AstrBot host SDK installed
    import importlib.util

    na_spec = importlib.util.spec_from_file_location(
        "_dm_narrative_api_test", dm_repo / "core" / "narrative_api.py"
    )
    na_mod = importlib.util.module_from_spec(na_spec)
    assert na_spec.loader is not None
    na_spec.loader.exec_module(na_mod)
    is_managed_event = na_mod.is_managed_event

    assert is_managed_event(ev) is True
    # Even an event whose get_extra is missing (dict-only _extras) is recognized
    bare_managed = SimpleNamespace(_extras={MANAGED_KEY: {"scope": {"umo": "aiocqhttp:FriendMessage:1001"}}})
    assert is_managed_event(bare_managed) is True
    # Dynamic MagicMock without explicit marker must remain False
    assert is_managed_event(MagicMock()) is False

    try:
        from astrbot_plugin_dynamics_memory.core.event_handler import EventHandler
    except ImportError:
        EventHandler = None

    if EventHandler is not None:
        handler = EventHandler.__new__(EventHandler)
        handler.config_manager = MagicMock()
        handler._memory_recall = SimpleNamespace(handle_memory_recall=AsyncMock())
        handler._memory_reflection = SimpleNamespace(handle_memory_reflection=AsyncMock())

        req_managed = SimpleNamespace()
        await handler.handle_memory_recall(ev, req_managed)
        assert getattr(req_managed, "_lm_private_hook_outcome_v1", None) == "skipped_managed"
        handler._memory_recall.handle_memory_recall.assert_not_awaited()

        await handler.handle_memory_reflection(ev, SimpleNamespace())
        handler._memory_reflection.handle_memory_reflection.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_bridge_managed_private_turn_skips_owned_original_flag_and_injects_temp_turn_notes(monkeypatch):
    """Verify AstrBotAgentBridge:
    1) does NOT set _cd_owned_original_input_v1 on narrative-managed private turns;
    2) injects turn_notes (outbound ledger + sleep wake-up note) as temp (_no_save) TextParts;
    3) does NOT persist turn_notes into committed conversation history or expose them to OnLLMRequest hooks.
    """
    class TextPart:
        def __init__(self, text: str = ""):
            self.type = "text"
            self.text = text
            self._no_save = False

        def mark_as_temp(self):
            self._no_save = True
            return self

        def model_dump(self):
            return {"type": "text", "text": self.text}

    class Message:
        def __init__(self, role: str, content):
            self.role = role
            self.content = content
            self._no_save = False

        def model_dump(self):
            return {
                "role": self.role,
                "content": [p.model_dump() if hasattr(p, "model_dump") else p for p in self.content]
                if isinstance(self.content, list)
                else self.content,
            }

    from dataclasses import dataclass

    @dataclass
    class MainAgentBuildConfig:
        provider_settings: dict = None
        tool_call_timeout: int = 60
        streaming_response: bool = False
        provider_wake_prefix: str = ""
        kb_agentic_mode: bool = False
        subagent_orchestrator: dict = None
        timezone: str = None
        sandbox_cfg: dict = None
        add_cron_tools: bool = True
        file_extract_enabled: bool = False
        file_extract_prov: str = "moonshotai"
        file_extract_msh_api_key: str = ""

    hook_seen_events = []
    hook_seen_parts = []
    captured_user_parts = []

    async def fake_call_event_hook(ev, _event_type, req):
        hook_seen_events.append(ev)
        hook_seen_parts.extend(list(getattr(req, "extra_user_content_parts", ()) or ()))
        return False

    class FakeRunner:
        def __init__(self, req):
            self.req = req
            self.provider = SimpleNamespace(text_chat=AsyncMock())
            self.fallback_providers = []
            self.tool_executor = SimpleNamespace(execute=AsyncMock())
            self.agent_hooks = SimpleNamespace(
                on_tool_start=AsyncMock(), on_tool_end=AsyncMock(), on_agent_done=AsyncMock()
            )
            self.request_context_manager = SimpleNamespace()
            self.streaming = False
            self.run_context = SimpleNamespace(messages=[])

        async def step_until_done(self, _max_steps):
            captured_user_parts.extend(self.req.extra_user_content_parts)
            if False:
                yield None

        def get_final_llm_resp(self):
            return SimpleNamespace(
                completion_text=json.dumps(
                    {"reply_mode": "text", "text": "唔…刚睡着就被你吵醒了。", "meme_id": None, "narration": ""},
                    ensure_ascii=False,
                ),
                result_chain=None,
            )

    async def fake_build_main_agent(*, event, plugin_context, config, provider, apply_reset=False):
        req = SimpleNamespace(
            prompt=event.message_str,
            system_prompt="base system",
            extra_user_content_parts=[],
            func_tool=None,
            image_urls=[],
            conversation=SimpleNamespace(cid="conv-priv-1", history="[]"),
        )
        runner = FakeRunner(req)

        async def do_reset():
            parts = [TextPart(text=req.prompt), *req.extra_user_content_parts]
            runner.run_context.messages = [Message(role="user", content=parts)]

        return SimpleNamespace(provider_request=req, agent_runner=runner, reset_coro=do_reset())

    # Install lightweight host module doubles if real AstrBot core modules aren't present
    def _ensure_mod(name: str):
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            monkeypatch.setitem(sys.modules, name, mod)
        return mod

    main_agent_mod = _ensure_mod("astrbot.core.astr_main_agent")
    monkeypatch.setattr(main_agent_mod, "build_main_agent", fake_build_main_agent, raising=False)
    monkeypatch.setattr(main_agent_mod, "MainAgentBuildConfig", MainAgentBuildConfig, raising=False)

    msg_comp_mod = _ensure_mod("astrbot.core.message.components")
    monkeypatch.setattr(msg_comp_mod, "Plain", Plain, raising=False)

    agent_msg_mod = _ensure_mod("astrbot.core.agent.message")
    monkeypatch.setattr(agent_msg_mod, "TextPart", TextPart, raising=False)

    ctx_utils_mod = _ensure_mod("astrbot.core.pipeline.context_utils")
    monkeypatch.setattr(ctx_utils_mod, "call_event_hook", fake_call_event_hook, raising=False)

    star_handler_mod = _ensure_mod("astrbot.core.star.star_handler")
    monkeypatch.setattr(
        star_handler_mod, "EventType", SimpleNamespace(OnLLMRequestEvent="OnLLMRequestEvent"), raising=False
    )

    core_mod = _ensure_mod("astrbot.core")
    if not hasattr(core_mod, "sp"):
        monkeypatch.setattr(core_mod, "sp", SimpleNamespace(get_async=AsyncMock(return_value={})), raising=False)

    conv_obj = SimpleNamespace(cid="conv-priv-1", persona_id="persona_v1", history="[]")
    conv_mgr = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="conv-priv-1"),
        get_conversation=AsyncMock(return_value=conv_obj),
        update_conversation=AsyncMock(),
    )
    persona_mgr = SimpleNamespace(
        resolve_selected_persona=AsyncMock(
            return_value=("persona_v1", {"name": "persona_v1", "prompt": "base persona"}, None, None)
        ),
        personas_v3=[{"name": "persona_v1", "prompt": "base persona"}],
    )
    ctx = SimpleNamespace(
        conversation_manager=conv_mgr,
        persona_manager=persona_mgr,
        get_config=lambda umo="": {"provider_settings": {}},
        get_provider_by_id=lambda pid: SimpleNamespace(id=pid),
    )

    bridge = AstrBotAgentBridge(ctx)
    ev = _private_friend_event("醒醒呀！", sender="1001", message_id="dm-temp-1")
    ev._extras[MANAGED_KEY] = {
        "scope": {
            "platform": "aiocqhttp",
            "umo": "aiocqhttp:FriendMessage:1001",
            "persona_id": "persona_v1",
            "instance_id": "persona_v1",
        },
        "character": {"core_prompt": "你是小夜", "group_rules": "", "persona_id": "persona_v1"},
        "snapshot": {"availability": "asleep", "revision": 1},
        "context_items": [],
        "availability": {},
        "turn_id": "turn-1",
        "bundle_id": "bundle-1",
    }
    persona = await bridge.snapshot(ev)
    outbound_note = "[近期其他插件/系统对外消息（只读参考）]\n- 【撤回提醒】对方撤回了一条消息"
    sleep_note = "【当前作息提醒】你原本在睡觉，刚被对方的连续消息/戳一戳吵醒。"

    out = await bridge.generate(
        ev,
        (ev,),
        json.dumps({"conversation": {"text": "醒醒呀！", "messages": [], "background": []}}, ensure_ascii=False),
        persona,
        "prov-1",
        history_text="用户原话：醒醒呀！",
        narrative_override=SimpleNamespace(evidence_current=lambda _m: True),
        turn_notes=(outbound_note, sleep_note),
    )

    # 1. _cd_owned_original_input_v1 is NOT set for narrative-managed private turns
    assert len(hook_seen_events) == 1
    assert getattr(hook_seen_events[0], "_cd_owned_original_input_v1", False) is False
    # 2. OnLLMRequest hook (dynamics_memory) NEVER saw turn_notes
    hook_texts = [getattr(p, "text", "") for p in hook_seen_parts]
    assert not any("撤回提醒" in t or "当前作息提醒" in t for t in hook_texts)
    # 3. Model request DID receive both turn_notes marked as temporary (_no_save=True)
    temp_texts = [getattr(p, "text", "") for p in captured_user_parts if getattr(p, "_no_save", False)]
    assert outbound_note in temp_texts
    assert sleep_note in temp_texts
    # 4. Persisted conversation history does NOT contain outbound_note or sleep_note
    serialized_history = json.dumps(out.history, ensure_ascii=False)
    assert "撤回提醒" not in serialized_history
    assert "当前作息提醒" not in serialized_history
    assert out.text == "唔…刚睡着就被你吵醒了。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "text", "has_meme", "meme_desc", "poke_target_uid", "private_mode", "expected_commit", "expected_verbal"),
    [
        (
            "poke",
            "",
            False,
            "",
            "",
            True,
            "[动作: 戳了对方]",
            None,
        ),
        (
            "poke_text",
            "醒着呢，这么晚还不睡，戳吾辈干嘛。",
            False,
            "",
            "",
            True,
            "[动作: 戳了对方]\n醒着呢，这么晚还不睡，戳吾辈干嘛。",
            "醒着呢，这么晚还不睡，戳吾辈干嘛。",
        ),
        (
            "poke_text",
            "顺手戳你一下！",
            False,
            "",
            "1002",
            False,
            "[动作: 戳了 小明]\n顺手戳你一下！",
            "顺手戳你一下！",
        ),
        (
            "meme_only",
            "",
            True,
            "",
            "",
            True,
            "[动作: 发了一张表情]",
            None,
        ),
        (
            "meme_only",
            "",
            True,
            "猫猫探头",
            "",
            True,
            "[动作: 发了一张表情（猫猫探头）]",
            None,
        ),
        (
            "text_meme",
            "别闹啦",
            True,
            "",
            "",
            True,
            "别闹啦\n[动作: 发了一张表情]",
            "别闹啦",
        ),
        (
            "poke_meme",
            "",
            True,
            "",
            "",
            True,
            "[动作: 戳了对方]\n[动作: 发了一张表情]",
            None,
        ),
    ],
)
async def test_nonverbal_action_history_committed_and_verbal_consumers_stay_clean(
    monkeypatch,
    mode,
    text,
    has_meme,
    meme_desc,
    poke_target_uid,
    private_mode,
    expected_commit,
    expected_verbal,
):
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    plugin = _plugin(
        {
            "decision_mode": "persona_model",
            "decision_backend": "jev",
            "private_chat_enabled": True,
            "natural_reply_rhythm_enabled": True,
            "base_thinking_delay": 0,
            "debounce_base_cooldown": 10,
            "daily_rhythm_enabled": False,
        }
    )
    bridge = BridgeDouble()
    plugin.persona_engine.bridge = bridge

    horizons_texts: list[str] = []
    rhythm_texts: list[str] = []
    from astrbot_plugin_chat_dynamics.core.integrations import dynamics_horizons

    monkeypatch.setattr(
        dynamics_horizons,
        "delivered",
        lambda _p, _rt, _it, delivered_text, **_kw: horizons_texts.append(delivered_text),
    )
    if getattr(plugin, "natural_rhythm", None) is not None:
        monkeypatch.setattr(plugin.natural_rhythm, "_probe_world_availability", AsyncMock(return_value="available"))
        monkeypatch.setattr(
            plugin.natural_rhythm,
            "evaluate_ingress_pacing",
            lambda *_a, **_kw: IngressPacingDecision(defer=False),
        )
        monkeypatch.setattr(plugin.natural_rhythm, "micro_pacing_enabled", lambda: False)
        monkeypatch.setattr(
            plugin.natural_rhythm,
            "on_turn_delivered",
            lambda *_a, **kw: rhythm_texts.append(kw.get("delivered_text", "")),
        )

    if private_mode:
        ev = _private_friend_event("戳一下", sender="1001", message_id=f"act-{mode}-{meme_desc}")
    else:
        ev = MockEvent(
            "哈哈哈",
            sender_id="1001",
            group_id="987654321",
            message_id=f"act-{mode}-{poke_target_uid}",
            components=[At(qq="bot_42")],
        )
        ev.get_platform_name = lambda: "aiocqhttp"
        ev.get_self_id = lambda: "bot_42"
        ev.get_sender_id = lambda: "1001"
        ev.get_group_id = lambda: "987654321"
        ev.unified_msg_origin = "aiocqhttp:GroupMessage:987654321"
        from astrbot_plugin_chat_dynamics.core.turn_decision import TurnDecision

        plugin.persona_engine.decide = AsyncMock(
            return_value=TurnDecision(
                action="reply",
                state="playful",
                target_message_ids=(ev.message_id,),
                response_goal="轻松接一句",
                length="brief",
                reason_code="jev_direct_reply",
            )
        )
        rt = plugin._get_or_create_runtime(ev.unified_msg_origin, group_id="987654321", umo=ev.unified_msg_origin)
        rt.dag.add_message(
            "seed-1002", "1002", "我也来", timestamp=plugin.time_service.time() - 5, metadata={"sender_name": "小明"}
        )

    ev.bot = SimpleNamespace(call_action=AsyncMock(return_value={"status": "ok", "message_id": "poke-mid"}))
    chains = []
    if has_meme:
        if meme_desc:
            class _DescribedImage:
                def __init__(self, file: str, summary: str):
                    self.file = file
                    self.url = file
                    self.summary = summary

            _DescribedImage.__name__ = "Image"
            img = _DescribedImage(file="/tmp/meme.png", summary=meme_desc)
        else:
            img = Image(file="/tmp/meme.png")
        if mode == "text_meme":
            chains = [MessageChain([Plain(text), img])]
            out_text = ""
        else:
            chains = [MessageChain([img])]
            out_text = text
    else:
        out_text = text

    bridge.generate = AsyncMock(
        return_value=AgentOutput(
            out_text,
            "conv",
            "[]",
            [],
            chains=chains,
            reply_mode=mode,
            requested_reply_mode=mode,
            reply_reason=f"model_selected_{mode}",
            poke_target_uid=poke_target_uid,
        )
    )

    try:
        if private_mode:
            await plugin.on_private_message(ev)
        else:
            await plugin.on_group_message(ev)
        await flush(plugin, ev)
        await drain(plugin)

        assert bridge.commits[-1] == expected_commit
        if expected_verbal is None:
            assert horizons_texts == []
            assert rhythm_texts == []
        else:
            assert horizons_texts == [expected_verbal]
            assert rhythm_texts == [expected_verbal]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_private_sleep_wakeup_merges_pending_messages_and_injects_notes(monkeypatch, tmp_path):
    """End-to-end test: private narrative with availability=asleep + real NaturalRhythmController + OutboundLedger."""
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    plugin = _plugin(
        {
            "decision_mode": "persona_model",
            "decision_backend": "jev",
            "private_chat_enabled": True,
            "narrative_enabled": True,
            "narrative_private_chat_enabled": True,
            "natural_reply_rhythm_enabled": True,
            "base_thinking_delay": 0,
            "debounce_base_cooldown": 10,
            "daily_rhythm_enabled": False,
        }
    )
    plugin.narrative = NarrativeCoordinator(plugin, tmp_path / "narrative_sleep.sqlite3")
    providers = _make_narrative_providers(availability="asleep", umo="aiocqhttp:FriendMessage:1001")
    _install_narrative_providers(plugin, providers)

    # Use REAL OutboundLedger attached on plugin (end-to-end with @outbound-dev!)
    now_mono = plugin.time_service.time()
    plugin.outbound_ledger.record_external_outbound(
        "aiocqhttp:FriendMessage:1001",
        text="【撤回提醒】对方撤回了一条消息",
        now=now_mono,
        platform_id="aiocqhttp",
        target_id="1001",
        scope="private",
    )
    plugin.outbound_ledger.record_bot_poke_sent(
        "aiocqhttp:FriendMessage:1001",
        "1001",
        "bot_42",
        now=now_mono,
        scope="private",
        platform_id="aiocqhttp",
    )
    matched = plugin.outbound_ledger.match_and_consume_self_poke_echo(
        "aiocqhttp:FriendMessage:1001",
        sender_id="1001",
        target_id="1001",
        bot_id="bot_42",
        now=now_mono + 1.0,
        action_text="揉了揉对方的脸，喵呜~",
        scope="private",
        platform_id="aiocqhttp",
    )
    assert matched is True

    # Use real NaturalRhythmController attached on plugin
    assert plugin.natural_rhythm is not None

    captured_calls: list[dict] = []
    bridge = BridgeDouble()

    async def capture_generate(event, events, prompt, persona, provider, **kwargs):
        captured_calls.append({"prompt": prompt, "kwargs": dict(kwargs), "events": events})
        return AgentOutput("唔…别吵啦，刚睡着被你叫醒了。", "conv", "[]", [])

    bridge.generate = capture_generate
    plugin.persona_engine.bridge = bridge

    ev1 = _private_friend_event("睡了吗", sender="1001", message_id="sleep-m1")
    ev2 = _private_friend_event("在不在呀", sender="1001", message_id="sleep-m2")
    ev3 = _private_friend_event("快起来看看这个", sender="1001", message_id="sleep-m3")
    ev4 = _private_friend_event("顺便帮我看一下明天的安排", sender="1001", message_id="sleep-m4")

    try:
        # Msg 1 -> deferred while asleep
        await plugin.on_private_message(ev1)
        await flush(plugin, ev1)
        await drain(plugin)
        assert len(captured_calls) == 0

        # Msg 2 -> deferred while asleep
        await plugin.on_private_message(ev2)
        await flush(plugin, ev2)
        await drain(plugin)
        assert len(captured_calls) == 0

        # Msg 3 within 90s -> triggers sleep wake-up breakthrough!
        await plugin.on_private_message(ev3)
        await flush(plugin, ev3)
        await drain(plugin)
        assert len(captured_calls) == 1

        call1 = captured_calls[0]
        prompt1 = json.loads(call1["prompt"])
        # Chronological merge: earlier deferred messages appear BEFORE current message
        conv_text = prompt1["conversation"]["text"]
        assert "睡了吗" in conv_text
        assert "在不在呀" in conv_text
        assert "快起来看看这个" in conv_text
        assert conv_text.index("睡了吗") < conv_text.index("在不在呀") < conv_text.index("快起来看看这个")
        # Not downgraded to brief acknowledge by narrative_asleep
        assert prompt1["response_plan"]["action"] == "reply"
        # Both outbound ledger note (external msg + backfilled poke action) AND sleep wake-up note injected
        turn_notes = call1["kwargs"].get("turn_notes", ())
        assert len(turn_notes) == 2
        assert "撤回提醒" in turn_notes[0]
        assert "揉了揉对方的脸，喵呜~" in turn_notes[0]
        assert "作息" in turn_notes[1] or "睡" in turn_notes[1]

        # Msg 4 during temp awake window -> NOT deferred, NOT blocked by narrative_asleep, still has sleep_wake_note
        await plugin.on_private_message(ev4)
        await flush(plugin, ev4)
        await drain(plugin)
        assert len(captured_calls) == 2
        turn_notes4 = captured_calls[1]["kwargs"].get("turn_notes", ())
        assert any("作息" in n or "睡" in n for n in turn_notes4)
    finally:
        await plugin.narrative.close_io()
        await plugin.terminate()


def test_history_join_preserves_double_newline_paragraphs_and_single_newline_actions():
    assert _join_history_delivered(["第一段。", "第二段。"]) == "第一段。\n\n第二段。"
    assert _join_history_delivered(["[动作: 戳了对方]", "第一段。", "第二段。"]) == "[动作: 戳了对方]\n第一段。\n\n第二段。"
    assert _join_history_delivered(["别闹", "[动作: 发了一张表情]"]) == "别闹\n[动作: 发了一张表情]"
    assert _join_history_delivered(["[动作: 戳了对方]", "[动作: 发了一张表情]"]) == "[动作: 戳了对方]\n[动作: 发了一张表情]"


@pytest.mark.asyncio
async def test_group_poke_at_bot_sleep_wakeup_overrides_narrative_asleep_and_injects_note(monkeypatch, tmp_path):
    """Verify group chat double poke-at-bot wakes from availability=asleep and is NOT suppressed by narrative_asleep."""
    monkeypatch.setattr(AstrBotAgentBridge, "check", lambda self: True)
    group_umo = "aiocqhttp:GroupMessage:987654321"
    plugin = _plugin(
        {
            "decision_mode": "persona_model",
            "decision_backend": "jev",
            "narrative_enabled": True,
            "narrative_all_groups": True,
            "natural_reply_rhythm_enabled": True,
            "base_thinking_delay": 0,
            "debounce_base_cooldown": 10,
            "daily_rhythm_enabled": False,
        }
    )
    plugin.narrative = NarrativeCoordinator(plugin, tmp_path / "narrative_grp_sleep.sqlite3")
    providers = _make_narrative_providers(availability="asleep", umo=group_umo)
    _install_narrative_providers(plugin, providers)

    from astrbot_plugin_chat_dynamics.core.turn_decision import TurnDecision

    async def fake_decide(item, _persona, _state, *, runtime=None):
        target = item.context.messages[-1].message_id if item.context.messages else ""
        return TurnDecision(
            action="reply",
            state="playful",
            target_message_ids=(target,) if target else (),
            response_goal="被连戳吵醒，迷迷糊糊回一句",
            length="brief",
            reason_code="jev_poke_reply",
        )

    plugin.persona_engine.decide = fake_decide

    captured_calls: list[dict] = []
    bridge = BridgeDouble()

    async def capture_generate(event, events, prompt, persona, provider, **kwargs):
        captured_calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
        return AgentOutput("唔…别戳啦，刚睡着呢。", "conv", "[]", [])

    bridge.generate = capture_generate
    plugin.persona_engine.bridge = bridge

    class Poke:
        def __init__(self, target: str):
            self.qq = target
            self.id = target

    def make_group_poke(mid: str):
        ev = MockEvent(
            "[戳一戳] 有人戳了你",
            sender_id="1001",
            group_id="987654321",
            message_id=mid,
            components=[Poke("bot_42")],
        )
        ev.unified_msg_origin = group_umo
        ev.get_platform_name = lambda: "aiocqhttp"
        ev.get_self_id = lambda: "bot_42"
        ev.get_sender_id = lambda: "1001"
        ev.get_group_id = lambda: "987654321"
        ev._extras = {}
        ev.get_extra = lambda k, d=None: ev._extras.get(k, d)
        ev.set_extra = lambda k, v: ev._extras.__setitem__(k, v)
        return ev

    ev1 = make_group_poke("gp-sleep-1")
    ev2 = make_group_poke("gp-sleep-2")

    try:
        # 1st poke while asleep -> deferred
        await plugin.on_group_message(ev1)
        await flush(plugin, ev1)
        await drain(plugin)
        assert len(captured_calls) == 0

        # 2nd poke within 90s -> triggers sleep wake-up and overrides narrative_asleep!
        await plugin.on_group_message(ev2)
        await flush(plugin, ev2)
        await drain(plugin)
        assert len(captured_calls) == 1
        turn_notes = captured_calls[0]["kwargs"].get("turn_notes", ())
        assert any("作息" in n or "睡" in n for n in turn_notes)
    finally:
        await plugin.narrative.close_io()
        await plugin.terminate()
