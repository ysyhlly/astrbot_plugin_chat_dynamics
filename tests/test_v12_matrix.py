from __future__ import annotations

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace

import pytest

import astrbot_plugin_chat_dynamics.core.platform_bridge as bridge
import astrbot_plugin_chat_dynamics.core.web_compat as web_compat
import astrbot_plugin_chat_dynamics.core.web_api as web_api
import astrbot_plugin_chat_dynamics.main as main_module
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.embedding_adapter import (
    EmbeddingAdapter,
    _extract_vector,
    _normalize_vec,
    _provider_id,
)
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter, LLMUnavailable
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRegistry, SessionRuntime
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode, VibeAnalyzer, parse_mode_label
from astrbot_plugin_chat_dynamics.core.debounce import DefaultIncompletenessDetector
from astrbot_plugin_chat_dynamics.core.reactions import ReactionPolicy
from astrbot_plugin_chat_dynamics.core.time_service import SystemClock
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key


def test_platform_bridge_private_helpers_cover_missing_and_exceptional_shapes(monkeypatch):
    assert bridge._safe_call(None, "missing", "fallback") == "fallback"
    assert bridge._safe_call(SimpleNamespace(value=3), "value") == 3
    assert bridge._safe_call(SimpleNamespace(value=lambda: 4), "value") == 4

    class TypeErrorObject:
        def value(self):
            raise TypeError("wrong signature")

    class BrokenObject:
        def value(self):
            raise RuntimeError("broken")

    assert bridge._safe_call(TypeErrorObject(), "value", "fallback") == "fallback"
    assert bridge._safe_call(BrokenObject(), "value", "fallback") == "fallback"
    assert bridge._extract_components(None) == []

    class MessageOnly:
        message_obj = SimpleNamespace(message=["fallback"])

    assert bridge._extract_components(MessageOnly()) == ["fallback"]

    class BrokenMessages(MessageOnly):
        def get_messages(self):
            raise RuntimeError("adapter")

    assert bridge._extract_components(BrokenMessages()) == ["fallback"]


def test_platform_bridge_mentions_reply_and_command_edge_cases():
    at_by_user = type("At", (), {"user_id": "u-2"})()
    empty_at = type("At", (), {"qq": 0})()
    reply_by_message = type("Reply", (), {"message_id": "reply-2"})()
    at_all = type("AtAll", (), {})()

    event = SimpleNamespace(get_messages=lambda: [at_by_user, empty_at, reply_by_message, at_all])
    mentions, reply = bridge.extract_mentions_and_reply(event, "hello @name, and @name")
    assert mentions == ["u-2", "name"]
    assert reply == "reply-2"
    assert bridge.is_command_like("") is False
    assert bridge.is_command_like("custom command", extra_prefixes=["custom"]) is True
    assert bridge.is_command_like("custom command", extra_prefixes=[]) is False


def test_platform_bridge_parse_fallback_ids_and_outline_media():
    class Event:
        message_str = ""
        message_id = ""
        unified_msg_origin = "adapter:room"
        message_obj = SimpleNamespace(message_id="object-mid", message=[])
        is_at_or_wake_command = False

        def get_group_id(self):
            return "g"

        def get_sender_id(self):
            return "u"

        def get_self_id(self):
            return "bot"

        def get_messages(self):
            return []

        def get_message_outline(self):
            return "[文件]"

    parsed = bridge.parse_group_event(Event())
    assert parsed.message_id == "object-mid"
    assert parsed.has_media is True
    assert parsed.media_only is True
    assert parsed.media_component_types == ["outline"]

    class NoGroup(Event):
        def get_group_id(self):
            return ""

    assert bridge.parse_group_event(NoGroup()).group_id == ""


def test_platform_bridge_chain_text_fallback_shapes(monkeypatch):
    class TypeErrorChain:
        def get_plain_text(self, *_args, **kwargs):
            if kwargs:
                raise TypeError
            return "plain"

    assert bridge.chain_plain_text(TypeErrorChain()) == "plain"

    class BrokenChain:
        def get_plain_text(self):
            raise RuntimeError

        chain = ["a", SimpleNamespace(text="b"), SimpleNamespace()]

    assert bridge.chain_plain_text(BrokenChain()) == "abnamespace()"
    assert bridge.chain_plain_text(SimpleNamespace(chain=["x", SimpleNamespace(text="y")])) == "xy"
    assert bridge.chain_plain_text(42) == "42"


@pytest.mark.asyncio
async def test_platform_bridge_send_result_matrix(monkeypatch):
    class NoApi:
        pass

    missing = await bridge.send_plain(NoApi(), None, "room", "x")
    assert missing.success is False

    class Context:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def send_message(self, target, chain):
            self.calls.append((target, bridge.chain_plain_text(chain)))
            return self.result

    failed_dict = await bridge.send_plain(None, Context({"success": "false", "id": "failed-id"}), "room", "x")
    assert failed_dict.success is False and failed_dict.message_id == "failed-id"
    failed_obj = await bridge.send_plain(
        None,
        Context(SimpleNamespace(success="0", message_id="failed-object")),
        "room",
        "x",
    )
    assert failed_obj.success is False and failed_obj.message_id == "failed-object"
    failed_ok = await bridge.send_plain(None, Context({"ok": False, "id": "failed-ok"}), "room", "x")
    assert failed_ok.success is False and failed_ok.message_id == "failed-ok"
    failed_status = await bridge.send_plain(None, Context({"status": 500}), "room", "x")
    assert failed_status.success is False
    failed_error = await bridge.send_plain(None, Context({"error": "rejected"}), "room", "x")
    assert failed_error.success is False
    success_dict = await bridge.send_plain(None, Context({"ok": True, "id": "ok-id"}), "room", "x")
    assert success_dict.success is True and success_dict.message_id == "ok-id"

    class SyncEvent:
        unified_msg_origin = "sync-room"

        def send(self, _chain):
            return 0

    assert (await bridge.send_plain(SyncEvent(), None, "room", "x")).success is False


def test_config_parser_coercion_matrix_and_non_mapping_inputs():
    class BrokenMapping:
        def get(self, *_args):
            raise RuntimeError

    cfg, warnings = parse_runtime_config(BrokenMapping())
    assert cfg.pipeline_mode == "filter"
    assert warnings == ()

    cfg, _ = parse_runtime_config(
        {
            "enable": "off",
            "ambient_intervention": "yes",
            "takeover_all": 0,
            "bot_names": " bot, bot,  ",
            "takeover_groups": ("g1", 2, ""),
            "exclude_groups": {"g2", "g2"},
            "pipeline_mode": "EXCLUSIVE",
            "command_prefix": "  #  ",
        }
    )
    assert cfg.enabled is False
    assert cfg.ambient_intervention is True
    assert cfg.takeover_all is False
    assert cfg.bot_names == ("bot",)
    assert cfg.takeover_groups == frozenset({"g1", "2"})
    assert cfg.exclude_groups == frozenset({"g2"})
    assert cfg.pipeline_mode == "exclusive"
    assert cfg.command_prefix == "#"

    class Weird:
        enable = True

    cfg, _ = parse_runtime_config(Weird())
    assert cfg.enabled is True


def test_session_runtime_state_and_registry_edges():
    runtime = SessionRuntime("s", "g", "u", last_activity=4.0)
    runtime.pending_followup_fragments = ["a", "b"]
    assert runtime.pending_followup_fragments == ["a", "b"]
    runtime.remember_seen("m")
    runtime.remember_sent("out")
    runtime.remember_hover(SimpleNamespace(timestamp=1.0), now=1.0, ttl=5.0)
    runtime.expire_hovers(now=10.0, ttl=5.0)
    assert runtime.pending_hover is None
    runtime.reset_conversation_state()
    assert runtime.pending_followup_fragments == []
    assert not runtime.seen_message_ids and not runtime.sent_message_ids

    clock = VirtualClock(initial_time=10.0)
    registry = SessionRegistry(clock, max_sessions=2)
    first = registry.get_or_create("s", group_id="old", umo="u")
    registry.get_or_create("s", group_id="new", umo="u2", bot_id="b")
    assert registry.resolve("new") == "s"
    assert registry.resolve("old") is None
    second = registry.get_or_create("s2", group_id="new", umo="u3")
    assert registry.capacity_reached() is True
    assert registry.oldest_idle() in {first.session_key, second.session_key}
    assert registry.drop("s2") is second
    assert registry.resolve("new") == "s"
    registry.clear()
    assert registry.known_ids() == set()


@pytest.mark.asyncio
async def test_embedding_provider_shapes_cache_and_sync_provider():
    assert _normalize_vec([]) == ()
    assert _normalize_vec([float("nan")]) == ()
    assert _normalize_vec([0.0, 0.0]) == (0.0, 0.0)
    assert _extract_vector({"data": [{"embedding": [0.0, 2.0]}]})[-1] == pytest.approx(1.0)
    assert _extract_vector({"unknown": []}) is None
    assert _provider_id(SimpleNamespace(provider_config={"id": "cfg"})) == "cfg"
    assert _provider_id(SimpleNamespace(provider_id="direct")) == "direct"

    class SyncProvider:
        provider_id = "sync"

        def get_embedding(self, _text):
            return [3.0, 4.0]

    class Context:
        def get_provider_by_id(self, provider_id):
            return SyncProvider() if provider_id == "sync" else None

    adapter = EmbeddingAdapter(Context(), enabled=True, provider_id="sync", cache_size=32)
    vector = await adapter.embed("sync")
    assert vector is not None and vector[-1] == pytest.approx(0.8)
    assert adapter.cached("sync") == vector
    adapter.configure(cache_size=32, link_threshold=0.5)
    assert adapter.should_link(adapter.match("sync", "sync")) is True


@pytest.mark.asyncio
async def test_embedding_timeout_and_provider_discovery_edges():
    class Slow:
        async def get_embedding(self, _text):
            await asyncio.sleep(0.05)
            return [1.0]

    adapter = EmbeddingAdapter(SimpleNamespace(get_all_embedding_providers=lambda: [Slow()]), enabled=True)
    adapter.timeout = 0.001
    assert await adapter.embed("slow") is None
    assert adapter.last_backend == "hashed"

    class Listing:
        def get_all_embedding_providers(self):
            return [SimpleNamespace(meta=SimpleNamespace(id="a")), SimpleNamespace(meta=SimpleNamespace(id="b"))]

    assert EmbeddingAdapter(Listing(), enabled=True, provider_id="b").resolve_provider().meta.id == "b"
    assert EmbeddingAdapter(Listing(), enabled=True, provider_id="missing").resolve_provider() is None
    assert EmbeddingAdapter(SimpleNamespace(get_all_embedding_providers=lambda: (_ for _ in ()).throw(RuntimeError())), enabled=True).resolve_provider() is None


@pytest.mark.asyncio
async def test_llm_adapter_legacy_and_missing_provider_edges():
    class Resp:
        completion_text = "legacy"

    class Provider:
        def text_chat(self, **_kwargs):
            return Resp()

    class Legacy:
        def get_using_provider(self):
            return Provider()

    assert await LLMAdapter(Legacy()).generate(prompt="p", umo="u", system_prompt="s") == "legacy"

    class Missing:
        def get_using_provider(self, _umo):
            return None

    with pytest.raises(LLMUnavailable):
        await LLMAdapter(Missing()).generate(prompt="p", umo="u", system_prompt="s")

    class NoLookup:
        async def llm_generate(self, **_kwargs):
            return Resp()

    with pytest.raises(LLMUnavailable):
        await LLMAdapter(NoLookup()).generate(prompt="p", umo="u", system_prompt="s")


def test_vibe_label_and_analyzer_lifecycle_edges():
    assert parse_mode_label("") is None
    assert parse_mode_label("FAST-BANTER") is GroupChatMode.FAST_BANTER
    assert parse_mode_label("这是一段严肃探讨") is GroupChatMode.SERIOUS_INQUIRY
    assert parse_mode_label("冷清") is GroupChatMode.CHILL_FADE
    assert parse_mode_label("unknown") is None

    analyzer = VibeAnalyzer()
    analyzer.set_mode("room", GroupChatMode.FAST_BANTER, source="manual")
    assert analyzer.mode_source("room") == "manual"
    analyzer.mark_llm_snapshot("room", 3.0)
    assert analyzer.has_llm_snapshot("room")
    assert analyzer.llm_snapshot_count("room") == 1
    analyzer.reset_session("room")
    assert analyzer.mode_source("room") == "telemetrics"
    assert analyzer.has_llm_snapshot("room") is False


@pytest.mark.asyncio
async def test_plugin_config_coercion_and_web_wrappers(monkeypatch):
    plugin = _plugin()
    assert plugin._coerce_config(None) == {}
    assert plugin._coerce_config({"x": 1}) == {"x": 1}
    assert plugin._coerce_config('{"x": 1}') == {"x": 1}
    assert plugin._coerce_config("not-json") == {}
    assert plugin._coerce_config("[1]") == {}
    assert plugin._coerce_config(42) == {}

    async def result(value):
        return value

    wrappers = (
        ("overview", "web_api_overview"),
        ("sessions", "web_api_sessions"),
        ("session", "web_api_session"),
        ("cool", "web_api_cool"),
        ("reset", "web_api_reset"),
        ("presets", "web_api_presets"),
        ("apply_preset", "web_api_apply_preset"),
    )
    for value, method in wrappers:
        monkeypatch.setattr(plugin._web, value, lambda value=value: result(value))
        assert await getattr(plugin, method)() == value


def test_plugin_text_bounds_and_outgoing_identifiers():
    plugin = _plugin()
    bounded = plugin._bounded_text("x" * 100, limit=20)
    assert len(bounded) <= 20
    assert "内容已截断" in bounded
    assert plugin._bounded_text("x" * 10, limit=4) == "xxxx"

    assert plugin._outgoing_message_id(None) is None
    assert plugin._outgoing_message_id({"message_id": "m-1"}) == "m-1"
    assert plugin._outgoing_message_id({"id": 7}) == "7"
    assert plugin._outgoing_message_id({"unknown": True}) is None
    assert plugin._outgoing_message_id(SimpleNamespace(msg_id="m-2")) == "m-2"
    assert plugin._outgoing_message_id(SimpleNamespace()) is None

    parsed = SimpleNamespace(
        message_id="",
        unified_msg_origin="mock:room",
        sender_id="user-1",
        group_id="room",
        text="same payload",
        outline="",
    )
    fingerprint = plugin._fallback_message_fingerprint(parsed)
    assert fingerprint.startswith("fp:")
    assert plugin._fallback_message_fingerprint(SimpleNamespace(message_id="known")) == ""

    class ObjectConfig:
        def __init__(self):
            self.value = 1

    assert plugin._coerce_config(ObjectConfig()) == {"value": 1}


def test_plugin_helper_guards_cover_mapping_locks_and_addressivity():
    plugin = _plugin()

    class GetterConfig:
        def get(self, key, default=None):
            return default

    mapping = GetterConfig()
    assert plugin._coerce_config(mapping) is mapping

    class BrokenEvent:
        def should_call_llm(self, _value):
            raise RuntimeError("host")

    plugin._suppress_native_llm(BrokenEvent())
    plugin.pipeline_mode = "exclusive"
    stopped = SimpleNamespace(stop_event=lambda: setattr(stopped, "stopped", True), stopped=False)
    plugin._block_native_for_mode(stopped)
    assert stopped.stopped is True

    runtime = plugin._get_or_create_runtime("helper", group_id="helper", umo="helper", bot_id="bot")
    bot_node = runtime.dag.add_message("bot-node", "bot", "answer", timestamp=1.0)
    assert plugin._looks_like_strong_address(
        SimpleNamespace(is_at_or_wake=False, self_id="", mentions=["bot"], reply_to_id=None), runtime
    ) is True
    assert plugin._looks_like_strong_address(
        SimpleNamespace(is_at_or_wake=False, self_id="", mentions=[], reply_to_id="bot-node"), runtime
    ) is True
    assert bot_node is not None

    async def lock_guard():
        await runtime.state_lock.acquire()
        try:
            plugin._drop_session("helper")
            assert "helper" in plugin._sessions
        finally:
            runtime.state_lock.release()

    asyncio.run(lock_guard())

    nodes = [
        SimpleNamespace(user_id="bot", text="reply"),
        SimpleNamespace(user_id="u-1", text="question"),
    ]
    assert plugin._build_context_prompt(nodes, bot_id="bot") == "Bot: reply\nUser_u-1: question"


def test_reaction_policy_decision_matrix_and_apply():
    policy = ReactionPolicy()
    assert policy.decide(reply_text="", mode=GroupChatMode.FAST_BANTER, enabled=True).reason == "code-or-empty"
    assert policy.decide(reply_text="```code```", mode=GroupChatMode.FAST_BANTER, enabled=True).reason == "code-or-empty"
    assert policy.decide(reply_text="已有😀", mode=GroupChatMode.FAST_BANTER, enabled=True).reason == "already-has-emoji"
    assert policy.decide(reply_text="x" * 73, mode=GroupChatMode.FAST_BANTER, enabled=True).reason == "long-reply"
    assert policy.decide(reply_text="x" * 41, mode=GroupChatMode.FAST_BANTER, enabled=True, fragment_count=2).reason == "multi-fragment"
    assert policy.decide(reply_text="ok", mode=GroupChatMode.FAST_BANTER, trigger_text="紧张", enabled=True).emoji in {"👍", "👀"}
    assert policy.decide(reply_text="ok", mode=GroupChatMode.FAST_BANTER, trigger_text="普通消息", enabled=True).append_to_text is False
    assert policy.apply("  ok  ", mode=GroupChatMode.FAST_BANTER, trigger_text="哈哈", enabled=True).endswith("😂")


def test_default_incompleteness_detector_covers_safe_boundaries():
    detector = DefaultIncompletenessDetector()
    for value in (None, "", "   "):
        assert detector.is_incomplete(value) is False
    assert detector.is_incomplete("因为") is True
    assert detector.is_incomplete("hello,") is True
    assert detector.is_incomplete("```") is True
    assert detector.is_incomplete("(") is True
    assert detector.is_incomplete("(ok)") is False


@pytest.mark.asyncio
async def test_web_compat_fallback_and_request_shapes(monkeypatch):
    original_web = sys.modules.get("astrbot.api.web")
    original_quart = sys.modules.get("quart")

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.headers = {}

    fake_quart = types.ModuleType("quart")
    fake_quart.request = SimpleNamespace()
    fake_quart.jsonify = lambda payload: Response(payload)
    sys.modules["astrbot.api.web"] = None
    sys.modules["quart"] = fake_quart
    try:
        fallback = importlib.reload(web_compat)
        response, status = fallback.json_response({"ok": True}, status_code=201, headers={"X-Test": "1"})
        assert status == 201 and response.payload["ok"] is True and response.headers["X-Test"] == "1"
        error_response = fallback.error_response("bad", status_code=400)
        assert error_response[1] == 400

        class PropertyRequest:
            json = asyncio.sleep(0, result={"property": True})

        fallback.request = PropertyRequest()
        assert await fallback.request_json({}) == {"property": True}

        class GetJsonRequest:
            async def get_json(self, **_kwargs):
                return None

        fallback.request = GetJsonRequest()
        assert await fallback.request_json({"default": True}) == {"default": True}

        class SyncGetJsonRequest:
            def get_json(self, **_kwargs):
                return {"sync": True}

        fallback.request = SyncGetJsonRequest()
        assert await fallback.request_json({}) == {"sync": True}
        fallback.request = object()
        assert await fallback.request_json("default") == "default"
    finally:
        if original_web is None:
            sys.modules.pop("astrbot.api.web", None)
        else:
            sys.modules["astrbot.api.web"] = original_web
        if original_quart is None:
            sys.modules.pop("quart", None)
        else:
            sys.modules["quart"] = original_quart
        importlib.reload(web_compat)
        importlib.import_module("astrbot_plugin_chat_dynamics.core.web_api")


def test_addressivity_helper_matrix():
    from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityLevel, AddressivityRouter
    from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG

    router = AddressivityRouter(bot_id="bot", bot_names=["小助手", "bot"])
    dag = ConversationDAG(session_id="address")
    bot = dag.add_message("bot-msg", "bot", "这个方案可以继续优化", timestamp=1.0)
    assert router._matching_mentions(["", "BOT", "other"], "bot", {"小助手"}) == ["BOT"]
    assert router._name_mentioned_in_text("bot", "bot please") is True
    assert router._name_mentioned_in_text("bot", "both") is False
    assert router._name_mentioned_in_text("助手", "助手席") is False
    assert router._name_mentioned_in_text("小助手", "@小助手，请看") is True
    assert router._name_mentioned_in_text("", "text") is False
    weak = router.compute_addressivity(dag.add_message("u", "u", "无关", timestamp=2.0), dag, last_bot_node=bot)
    assert weak.level in {AddressivityLevel.WEAK, AddressivityLevel.SAFE_HOVER}
    assert router._tokenize("abc 中文")


@pytest.mark.asyncio
async def test_virtual_and_system_clock_edges():
    clock = VirtualClock(initial_time=10.0)
    assert clock.now() == 10.0
    with pytest.raises(ValueError):
        await clock.advance(-1)
    with pytest.raises(ValueError):
        clock.advance_sync(-1)
    await clock.sleep(-1)
    assert clock.sleep_history[-1] == 0.0
    system = SystemClock()
    before = system.time()
    await system.sleep(-1)
    assert system.time() >= before


@pytest.mark.asyncio
async def test_plugin_shadow_transition_and_auxiliary_config_sync(monkeypatch):
    plugin = _plugin({"vibe_llm_enabled": True, "neural_embedding_enabled": True})
    key = _session_key("transition")
    runtime = plugin._get_or_create_runtime(key, group_id="transition", umo=key, bot_id="bot")
    generation = asyncio.create_task(asyncio.sleep(60))
    vibe = asyncio.create_task(asyncio.sleep(60))
    embed = asyncio.create_task(asyncio.sleep(60))
    hook = asyncio.create_task(asyncio.sleep(60))
    runtime.generation_task = generation
    plugin._vibe_llm_tasks_by_session[key] = vibe
    plugin._vibe_llm_tasks.add(vibe)
    plugin._embedding_tasks_by_session[key] = {embed}
    plugin._hook_tasks_by_session[key] = {hook}
    plugin.config.update(
        {
            "shadow_mode": True,
            "vibe_llm_enabled": False,
            "debounce_base_cooldown": 1.0,
            "debounce_extended_cooldown": 2.0,
            "debounce_max_cap": 3.0,
            "chars_per_second": 40.0,
        }
    )
    plugin.refresh_config()
    assert plugin.is_group_takeover_enabled("transition") is True
    await asyncio.gather(generation, vibe, embed, hook, return_exceptions=True)
    assert plugin.shadow_mode is True
    assert plugin.debounce.base_cooldown == 1.0
    assert plugin.pacer.chars_per_second == 40.0
    assert plugin._metrics["shadow_transition"] == 1


@pytest.mark.asyncio
async def test_plugin_ingress_stale_and_closed_buffer_paths(monkeypatch):
    plugin = _plugin({"debounce_base_cooldown": 10.0})
    key = _session_key("ingress-edges")
    runtime = plugin._get_or_create_runtime(key, group_id="ingress-edges", umo=key, bot_id="bot")
    stale = MockEvent("old", group_id="ingress-edges", message_id="old")
    stale._chat_dynamics_epoch = runtime.epoch - 1
    await plugin.on_group_message(stale)
    assert plugin._metrics["stale_turn_ignored"] >= 1

    no_attr = MockEvent("no attr", group_id="ingress-edges", message_id="no-attr")
    no_attr.__class__ = type("NoAttrEvent", (no_attr.__class__,), {"__slots__": ()})
    # A normal event remains the supported path; this call also covers the
    # ingress fallback when a host object refuses a dynamic epoch attribute.
    await plugin.on_group_message(no_attr)
    await plugin.debounce.close(flush=False)
    with pytest.raises(RuntimeError):
        await plugin.on_group_message(MockEvent("closed", group_id="ingress-edges", message_id="closed"))


@pytest.mark.asyncio
async def test_plugin_generation_empty_and_stale_dispatch_paths(monkeypatch):
    plugin = _plugin()
    key = _session_key("dispatch-edges")
    runtime = plugin._get_or_create_runtime(key, group_id="dispatch-edges", umo=key, bot_id="bot")
    trigger = runtime.dag.add_message("trigger", "user", "trigger", timestamp=1.0)
    plugin._run_native_reply = lambda *_args, **_kwargs: asyncio.sleep(0, result="")
    await plugin._dispatch_bot_response(runtime, trigger, GroupChatMode.CHILL_FADE, None, runtime.revision, epoch=runtime.epoch)
    plugin._run_native_reply = lambda *_args, **_kwargs: asyncio.sleep(0, result="reply")
    runtime.revision += 1
    await plugin._dispatch_bot_response(runtime, trigger, GroupChatMode.CHILL_FADE, None, runtime.revision - 1, epoch=runtime.epoch)
    await plugin._dispatch_bot_response(runtime, trigger, GroupChatMode.CHILL_FADE, None, runtime.revision, epoch=runtime.epoch - 1)
    assert runtime.last_bot_node is None


@pytest.mark.asyncio
async def test_plugin_native_and_llm_failure_wrappers(monkeypatch):
    plugin = _plugin()
    key = _session_key("wrapper-fail")
    plugin._get_or_create_runtime(key, group_id="wrapper-fail", umo=key, bot_id="bot")

    async def unavailable(*_args, **_kwargs):
        raise LLMUnavailable("missing")

    monkeypatch.setattr(plugin.llm, "run_native_agent", unavailable)
    assert await plugin._run_native_reply(MockEvent("x", group_id="wrapper-fail"), text="x", vibe_mode=GroupChatMode.CHILL_FADE, session_id=key) == ""
    monkeypatch.setattr(plugin.llm, "generate", unavailable)
    assert await plugin._generate_llm("x", None, GroupChatMode.CHILL_FADE, key) == ""

    async def broken(*_args, **_kwargs):
        raise RuntimeError("failed")

    monkeypatch.setattr(plugin.llm, "run_native_agent", broken)
    assert await plugin._run_native_reply(MockEvent("x", group_id="wrapper-fail"), text="x", vibe_mode=GroupChatMode.CHILL_FADE, session_id=key) == ""
    monkeypatch.setattr(plugin.llm, "generate", broken)
    assert await plugin._generate_llm("x", None, GroupChatMode.CHILL_FADE, key) == ""


@pytest.mark.asyncio
async def test_web_api_remaining_rate_and_snapshot_guards(monkeypatch, offline_web_responses):
    plugin = _plugin({"takeover_all": True})
    api = plugin._web

    monkeypatch.setattr(web_api, "query_value", lambda _name: (_ for _ in ()).throw(RuntimeError("query")))
    assert web_api._query_param("session_key") == ""

    class BadJson:
        content_length = None

    monkeypatch.setattr(web_api, "request", BadJson())

    async def unserializable(_default):
        return {"bad": object()}

    monkeypatch.setattr(web_api, "request_json", unserializable)
    assert (await web_api._json_body())["__invalid_body__"] == "invalid JSON body"

    class HeaderObject:
        def __init__(self):
            self.headers = {}

    original_error = web_api.error_response
    for response_shape in ("dict", "tuple-dict", "tuple-object", "object"):
        def legacy_error(_message, *, status_code=400, data=None, shape=response_shape):
            if shape == "dict":
                return {"status_code": status_code}
            if shape == "tuple-dict":
                return ({}, status_code)
            if shape == "tuple-object":
                return (HeaderObject(), status_code)
            return HeaderObject()

        def reject_headers(*args, **kwargs):
            if kwargs.get("headers") is not None:
                raise TypeError("legacy")
            return legacy_error(*args, **kwargs)

        monkeypatch.setattr(web_api, "error_response", reject_headers)
        response = web_api._json_err("bad", 429, headers={"Retry-After": "1"})
        if response_shape == "dict":
            assert response["headers"]["Retry-After"] == "1"
        elif response_shape == "tuple-dict":
            assert response[0]["headers"]["Retry-After"] == "1"
        elif response_shape == "tuple-object":
            assert response[0].headers["Retry-After"] == "1"
        else:
            assert response.headers["Retry-After"] == "1"
    monkeypatch.setattr(web_api, "error_response", original_error)

    class BadUsername:
        @property
        def username(self):
            raise RuntimeError("username")

    monkeypatch.setattr(web_api, "request", BadUsername())
    assert api._request_identity() == "anonymous"

    limited = {"status_code": 429}
    monkeypatch.setattr(api, "_rate_limit", lambda *_args: limited)
    for handler in (api.overview, api.sessions, api.session, api.cool, api.reset, api.presets, api.apply_preset):
        assert await handler() is limited

    monkeypatch.setattr(api, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(web_api, "query_value", lambda name: "known" if name == "session_key" else "")
    monkeypatch.setattr(web_api, "snapshot_session_or_none", lambda *_args: None)
    assert (await api.session())["status_code"] == 404

    key = _session_key("web-failure")
    plugin._get_or_create_runtime(key, group_id="web-failure", umo=key, bot_id="bot")
    async def valid_body(_default):
        return {"session_key": key, "minutes": 15}
    monkeypatch.setattr(web_api, "request_json", valid_body)
    monkeypatch.setattr(plugin, "_cool_session_async", lambda *_args: asyncio.sleep(0, result=False))
    assert (await api.cool())["status_code"] == 404

    plugin._shutting_down = True
    assert (await api.reset())["status_code"] == 503
    plugin._shutting_down = False

    async def apply_body(_default):
        return {"name": "active", "confirm": True}
    monkeypatch.setattr(web_api, "request_json", apply_body)
    async def broken_apply(_name):
        raise RuntimeError("save")
    monkeypatch.setattr(plugin, "apply_preset", broken_apply)
    assert (await api.apply_preset())["status_code"] == 503


@pytest.mark.asyncio
async def test_plugin_initialize_terminate_and_admin_command_branches(monkeypatch):
    plugin = _plugin()
    await plugin.initialize()
    assert plugin._session_sweep_task is not None
    await plugin.terminate()

    plugin = _plugin()
    denied = MockEvent("/dynamics status", group_id="admin-branches", is_admin_user=False)
    await plugin.cmd_dynamics(denied, "status", "")
    private = MockEvent("/dynamics status", group_id="")
    await plugin.cmd_dynamics(private, "status", "")
    for action, param in (("cool", "nan"), ("cool", "inf"), ("cool", "181"), ("unknown", ""), ("reset", "")):
        event = MockEvent("/dynamics", group_id="admin-branches", message_id=f"{action}-{param}")
        await plugin.cmd_dynamics(event, action, param)
    assert len(denied.replies_sent) == 1


@pytest.mark.asyncio
async def test_plugin_drop_guard_and_capacity_filters(monkeypatch):
    plugin = _plugin()
    plugin._registry.max_sessions = 1
    key = _session_key("capacity-filters")
    runtime = plugin._get_or_create_runtime(key, group_id="capacity-filters", umo=key, bot_id="bot")
    for reason in ("generation", "vibe", "state", "send", "followup", "debounce", "hook"):
        runtime.generation_task = None
        plugin._vibe_llm_tasks_by_session.pop(key, None)
        plugin._embedding_tasks_by_session.pop(key, None)
        plugin._hook_tasks_by_session.pop(key, None)
        runtime.followup_queue.clear()
        if reason == "generation":
            runtime.generation_task = asyncio.create_task(asyncio.sleep(60))
        elif reason == "vibe":
            task = asyncio.create_task(asyncio.sleep(60))
            plugin._vibe_llm_tasks_by_session[key] = task
        elif reason == "state":
            await runtime.state_lock.acquire()
        elif reason == "send":
            await runtime.send_lock.acquire()
        elif reason == "followup":
            runtime.followup_queue.append(main_module.FollowupBatch())
        elif reason == "debounce":
            await plugin.debounce.ingest(key, "u", "pending", object(), plugin.on_turn_flushed)
        else:
            plugin._hook_tasks_by_session[key] = {asyncio.create_task(asyncio.sleep(60))}
        assert plugin._ensure_runtime_capacity("new-" + reason) is False
        if runtime.state_lock.locked():
            runtime.state_lock.release()
        if runtime.send_lock.locked():
            runtime.send_lock.release()
        for task in list(plugin._vibe_llm_tasks_by_session.values()) + list(plugin._hook_tasks_by_session.get(key, set())) + ([runtime.generation_task] if runtime.generation_task else []):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.sleep(0)
        await plugin.debounce.discard(key)
    await plugin.terminate()
