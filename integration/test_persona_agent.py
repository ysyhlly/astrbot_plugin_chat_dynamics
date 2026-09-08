"""Real SDK builder/runner contracts; provider and persistence are offline fakes."""

from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace

import pytest

from .test_sdk_smoke import astrbot  # noqa: F401  (real-SDK guard and import path)

import astrbot.api  # noqa: F401, E402
import astrbot.core.pipeline  # noqa: F401, E402
from astrbot.core import astr_main_agent as host  # noqa: E402
from astrbot.core.agent.tool import FunctionTool, ToolSet  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.star.context import Context  # noqa: E402
from astrbot.core.provider.entities import LLMResponse  # noqa: E402
from astrbot.api.platform import MessageType  # noqa: E402
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge  # noqa: E402
from astrbot.core.pipeline.context_utils import call_event_hook as real_call_event_hook  # noqa: E402


class Event(AstrMessageEvent):
    def __init__(self):
        obj = SimpleNamespace(type=MessageType.GROUP_MESSAGE, message=[], message_id="m", group_id="room",
                              sender=SimpleNamespace(user_id="u", nickname="user"), self_id="bot")
        meta = SimpleNamespace(id="test", name="test", support_proactive_message=False, support_streaming_message=False)
        super().__init__("完整请求", obj, meta, "room")
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class HostContext(Context):
    def __init__(self, conv, personas, provider, settings):
        self.config = {"provider_settings": settings, "subagent_orchestrator": {}}
        self.persona_manager = SimpleNamespace(personas_v3=personas)
        self.subagent_orchestrator = None
        self.knowledge_base_manager = None
        self.conv = conv
        self.provider = provider
        self.saved = []
        self.tools = {name: FunctionTool(name=name, description=name, parameters={"type": "object", "properties": {}})
                      for name in ("allowed", "forbidden")}

        async def current(umo):
            return self.conv.cid

        async def get(umo, cid):
            return self.conv

        async def update(umo, cid, **kwargs):
            self.saved.append(kwargs["history"])
            self.conv.history = json.dumps(kwargs["history"])

        self.conversation_manager = SimpleNamespace(get_curr_conversation_id=current, get_conversation=get,
                                                     update_conversation=update)

    def get_config(self, umo=None):
        return self.config

    def get_provider_by_id(self, provider_id):
        return self.provider

    def get_using_provider(self, umo=None):
        return self.provider

    def get_llm_tool_manager(self):
        return SimpleNamespace(get_func=lambda name: self.tools.get(name), get_full_tool_set=lambda: ToolSet(list(self.tools.values())))


@pytest.fixture
def host_fixture(monkeypatch):
    from astrbot.core.persona_mgr import PersonaManager

    async def no_service(**kwargs):
        return {}

    async def no_kb(**kwargs):
        return ""

    async def no_unbound_plugin_hooks(*args, **kwargs):
        return False

    from astrbot.core import astr_agent_hooks
    monkeypatch.setattr(astr_agent_hooks, "call_event_hook", no_unbound_plugin_hooks)
    from astrbot.core.pipeline import context_utils
    monkeypatch.setattr(context_utils, "call_event_hook", no_unbound_plugin_hooks)

    monkeypatch.setattr(host.sp, "get_async", no_service, raising=False) if hasattr(host, "sp") else None
    from astrbot.core import sp
    monkeypatch.setattr(sp, "get_async", no_service)
    monkeypatch.setattr(host, "retrieve_knowledge_base", no_kb)
    monkeypatch.setattr(host.SkillManager, "list_skills", lambda *args, **kwargs: [])
    persona = {"name": "quiet", "prompt": "你是克制但可靠的群友", "tools": ["allowed"], "skills": [],
               "_begin_dialogs_processed": []}
    conv = SimpleNamespace(cid="conv", persona_id="quiet", history=json.dumps([
        {"role": "user", "content": "之前的请求"}, {"role": "assistant", "content": "之前已经送达"},
    ]), token_usage=0)
    calls = []

    async def chat(**kwargs):
        calls.append(kwargs)
        return LLMResponse(role="assistant", completion_text="第一段。\n\n未送达第二段。")

    provider = SimpleNamespace(provider_config={"max_context_tokens": 100000, "modalities": ["image", "tool_use"]},
                               text_chat=chat, get_model=lambda: "offline-test")
    settings = {"computer_use_runtime": "none", "default_personality": "ignored-default",
                "proactive_capability": {"add_cron_tools": False}, "max_context_length": -1}
    ctx = HostContext(conv, [persona], provider, settings)
    # Exercise the host's actual newer resolver when the installed SDK exposes it.
    if hasattr(PersonaManager, "resolve_selected_persona"):
        async def resolve(**kwargs):
            return await PersonaManager.resolve_selected_persona(ctx.persona_manager, **kwargs)
        ctx.persona_manager.resolve_selected_persona = resolve
    return ctx, Event(), calls


@pytest.mark.asyncio
async def test_real_builder_preserves_persona_history_tools_and_delayed_commit(host_fixture):
    ctx, event, calls = host_fixture
    bridge = AstrBotAgentBridge(ctx)
    assert bridge.check(), bridge.diagnostic
    persona = await bridge.snapshot(event)
    output = await bridge.generate(event, (event,), "完整聚合请求", persona, "provider", history_text="原始用户文本")
    assert calls, "The real main-agent runner must reach the offline provider"
    messages = [m if isinstance(m, dict) else m.model_dump() for m in calls[0]["contexts"]]
    assert "克制但可靠" in messages[0]["content"]
    assert any("之前已经送达" in str(m["content"]) for m in messages)
    assert any("完整聚合请求" in str(m["content"]) for m in messages)
    assert "allowed" in calls[0]["func_tool"].names()
    assert "forbidden" not in calls[0]["func_tool"].names()
    assert event.message_str == "完整请求" and not event.sent and not ctx.saved
    assert await bridge.commit(event, output, "第一段。")
    saved = json.dumps(ctx.saved[-1], ensure_ascii=False)
    assert "第一段。" in saved and "未送达第二段" not in saved
    assert "之前已经送达" in saved
    assert "原始用户文本" in saved and "完整聚合请求" not in saved
    assert not await bridge.commit(event, output, "重复"), "A second commit must not overwrite history"


@pytest.mark.asyncio
async def test_owned_agent_passes_request_guard_but_original_event_is_blocked(host_fixture, monkeypatch):
    from astrbot.core.pipeline import context_utils
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
    from .test_sdk_smoke import SmokeContext

    ctx, event, calls = host_fixture
    plugin = ChatDynamicsPlugin(context=SmokeContext(), config={
        "enable": True, "takeover_all": True, "debounce_base_cooldown": 10,
    })
    plugin.decision_mode = "persona_model"
    event.is_at_or_wake_command = True

    async def request_hook(incoming, _kind, req):
        await plugin.on_llm_request(incoming, req)
        return incoming.is_stopped()

    monkeypatch.setattr(context_utils, "call_event_hook", request_hook)
    try:
        await plugin.on_group_message(event)
        assert not event.is_stopped()
        event.should_call_llm(False)
        await plugin.on_llm_request(event, ProviderRequest(prompt="extra default reply"))
        assert event.is_stopped() and calls == []
        bridge = AstrBotAgentBridge(ctx)
        output = await bridge.generate(event, (event,), "owned reply", await bridge.snapshot(event), "provider")
        assert output.text and len(calls) == 1
        assert event.is_stopped() and not event.sent
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_real_persona_precedence_and_disabled_persona(host_fixture, monkeypatch):
    from astrbot.core import sp

    ctx, event, _ = host_fixture
    ctx.persona_manager.personas_v3.append({"name": "forced", "prompt": "会话强制人格", "tools": [], "skills": []})

    async def forced(**kwargs):
        return {"persona_id": "forced"}

    monkeypatch.setattr(sp, "get_async", forced)
    bridge = AstrBotAgentBridge(ctx)
    assert (await bridge.snapshot(event)).prompt == "会话强制人格"

    async def empty(**kwargs):
        return {}

    monkeypatch.setattr(sp, "get_async", empty)
    ctx.conv.persona_id = "[%None]"
    assert (await bridge.snapshot(event)).prompt == ""

    ctx.persona_manager.resolve_selected_persona = None
    assert (await bridge.snapshot(event)).prompt == ""
    ctx.conv.persona_id = "quiet"
    assert "克制但可靠" in (await bridge.snapshot(event)).prompt


@pytest.mark.asyncio
async def test_real_tool_execution_is_buffered_and_journaled(host_fixture):
    from astrbot.core.message.message_event_result import MessageChain

    ctx, event, calls = host_fixture
    executed = []

    class AllowedTool(FunctionTool):
        async def call(self, context, **kwargs):
            executed.append("called")
            await context.context.event.send(MessageChain().message("工具生成的内容"))
            return "工具已执行"

    ctx.tools["allowed"] = AllowedTool(name="allowed", description="test", parameters={"type": "object", "properties": {}})

    async def chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return LLMResponse(role="assistant", completion_text="", tools_call_name=["allowed"],
                               tools_call_args=[{}], tools_call_ids=["call1"])
        return LLMResponse(role="assistant", completion_text="执行完成。")

    ctx.provider.text_chat = chat
    bridge = AstrBotAgentBridge(ctx)
    journal = deque(maxlen=32)
    output = await bridge.generate(event, (event,), "请调用 allowed", await bridge.snapshot(event), "provider", execution_log=journal)
    assert executed == ["called"] and len(calls) == 2
    assert not event.sent and not ctx.saved
    assert output.chains and "工具生成的内容" in output.chains[0].get_plain_text()
    assert [entry["status"] for entry in journal] == ["started", "completed"]
    assert await bridge.commit(event, output, "工具生成的内容")
    history = ctx.saved[-1]
    assert any(m.get("role") == "tool" for m in history)
    assert not any("执行完成" in str(m.get("content")) for m in history)


@pytest.mark.asyncio
async def test_real_image_attachment_reaches_provider(host_fixture, tmp_path):
    import base64
    from astrbot.core.message.components import Image

    ctx, event, calls = host_fixture
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5V8AAAAASUVORK5CYII="))
    event.message_obj.message = [Image.fromFileSystem(str(image_path))]
    bridge = AstrBotAgentBridge(ctx)
    await bridge.generate(event, (event,), "帮我看图", await bridge.snapshot(event), "provider", media_understand=True)
    messages = [m if isinstance(m, dict) else m.model_dump() for m in calls[0]["contexts"]]
    assert "image_url" in json.dumps(messages)
    assert len(event.message_obj.message) == 1


@pytest.mark.asyncio
async def test_bridge_refuses_stale_persona_and_history_conflicts(host_fixture):
    from astrbot_plugin_chat_dynamics.core.agent_bridge import PersonaChanged

    ctx, event, _ = host_fixture
    bridge = AstrBotAgentBridge(ctx)
    persona = await bridge.snapshot(event)
    ctx.persona_manager.personas_v3[0]["tools"] = []
    with pytest.raises(PersonaChanged):
        await bridge.generate(event, (event,), "请求", persona, "provider")
    output = await bridge.generate(event, (event,), "请求", await bridge.snapshot(event), "provider")
    ctx.conv.cid = "different"
    assert not await bridge.commit(event, output, "实际送达")
    assert not ctx.saved


def test_bridge_capability_failure_is_explicit():
    bridge = AstrBotAgentBridge(SimpleNamespace())
    assert not bridge.check()
    assert bridge.diagnostic.startswith("CD_AGENT_BRIDGE_UNAVAILABLE")


@pytest.mark.asyncio
async def test_real_sdk_dispatches_both_companions_once(host_fixture, monkeypatch):
    """Real SDK metadata, registry, hook dispatcher and runner; offline companion IO."""
    from astrbot.core.agent.message import TextPart
    from astrbot.core.pipeline import context_utils
    from astrbot.core.star import context as sdk_context
    from astrbot.core.star.star import StarMetadata, star_map
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, StarHandlerRegistry
    from astrbot_plugin_chat_dynamics.core.selflearning_bridge import SelfLearningBridge

    ctx, event, calls = host_fixture
    invoked = []

    def inject(req, text):
        part = TextPart(text=text)
        marker = getattr(part, "mark_as_temp", None)
        if callable(marker):
            req.extra_user_content_parts.append(marker())
        else:
            # Companion plugins on older SDKs can keep hints in the system prompt.
            req.system_prompt += "\n" + text

    class Learning:
        _hook_handler = object()

        async def inject_diversity_to_llm_request(self, event, req):
            invoked.append(("selflearning", event.unified_msg_origin))
            inject(req, "approved-learning-hint")

    class Memory:
        initializer = SimpleNamespace(is_initialized=True)

        async def handle_memory_recall(self, event, req):
            invoked.append(("livingmemory", event.unified_msg_origin))
            inject(req, "retrieved-memory-hint")

    registry = StarHandlerRegistry()
    stars = []
    for name, plugin, method in (
        ("astrbot_plugin_self_learning", Learning(), "inject_diversity_to_llm_request"),
        ("LivingMemory", Memory(), "handle_memory_recall"),
    ):
        module = "offline_companion_" + name
        meta = StarMetadata(name=name, star_cls=plugin, module_path=module, activated=True)
        stars.append(meta)
        monkeypatch.setitem(star_map, module, meta)
        registry.append(StarHandlerMetadata(EventType.OnLLMRequestEvent, module + "." + method,
                                           method, module, getattr(plugin, method), []))
    monkeypatch.setattr(sdk_context, "star_registry", stars)
    monkeypatch.setattr(context_utils, "star_handlers_registry", registry)
    monkeypatch.setattr(context_utils, "call_event_hook", real_call_event_hook)
    companion = SelfLearningBridge(ctx)
    assert companion.snapshot()["detail"] == "native_hooks"
    assert len(companion.snapshot()["providers"]) == 2
    bridge = AstrBotAgentBridge(ctx)
    output = await bridge.generate(event, (event,), "request", await bridge.snapshot(event), "provider")
    assert invoked == [("selflearning", event.unified_msg_origin), ("livingmemory", event.unified_msg_origin)]
    payload = str(calls[0]["contexts"])
    assert payload.count("approved-learning-hint") == 1
    assert payload.count("retrieved-memory-hint") == 1
    assert not event.sent and not ctx.saved
    assert await bridge.commit(event, output, "delivered")
    assert "approved-learning-hint" not in str(ctx.saved)
    assert "retrieved-memory-hint" not in str(ctx.saved)

    # Legacy exclusive replies must also dispatch each request hook exactly
    # once despite the ingress event already being stopped by the plugin.
    from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter
    invoked.clear()
    forwarded = []

    async def tool_loop(**kwargs):
        forwarded.append(kwargs)
        return LLMResponse(role="assistant", completion_text="legacy response")

    async def resolve(*args, **kwargs):
        return "provider"

    ctx.tool_loop_agent = tool_loop
    adapter = LLMAdapter(ctx)
    adapter.resolve_provider_id = resolve
    event.stop_event()
    assert await adapter.run_native_agent(event, "legacy request") == "legacy response"
    assert event.is_stopped()
    assert invoked == [("selflearning", event.unified_msg_origin), ("livingmemory", event.unified_msg_origin)]
    forwarded_text = forwarded[0]["prompt"] + forwarded[0]["system_prompt"]
    assert forwarded_text.count("approved-learning-hint") == 1
    assert forwarded_text.count("retrieved-memory-hint") == 1
