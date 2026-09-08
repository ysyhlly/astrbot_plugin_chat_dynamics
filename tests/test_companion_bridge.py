"""Companion contracts: real metadata shape, native ownership and scoped async IO."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.selflearning_bridge import SelfLearningBridge
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook


def context(*plugins):
    return NS(get_all_stars=lambda: list(plugins))


def metadata(plugin, name="astrbot_plugin_self_learning", activated=True):
    return NS(name=name, star_cls=plugin, activated=activated)


class API:
    def get_approved_memories(self, *, umo, user_id, limit):
        return [{"tag": "calm", "umo": umo, "user_id": user_id}]


@pytest.mark.asyncio
@pytest.mark.parametrize("cap,method", [
    ("memories", "get_approved_memories"), ("memories", "list_approved_memories"),
    ("memories", "fetch_memories"), ("memories", "query_memories"),
    ("relationships", "get_relationship_hints"), ("relationships", "list_relationships"),
    ("relationships", "relationship_snapshot"), ("slang", "get_slang_candidates"),
    ("slang", "list_slang"), ("slang", "approved_slang"), ("slang", "list_approved_slang"),
])
async def test_every_direct_alias_reaches_model_context(cap, method):
    calls = []

    async def read(**kwargs):
        calls.append(kwargs)
        return {cap: [{"text": "approved context", "approved": True, "umo": kwargs["umo"]},
                      {"text": "foreign", "approved": True, "umo": "other"}]}

    api = NS(**{method: read})
    bridge = SelfLearningBridge(context(metadata(api)))
    payload = await bridge.model_context(umo="room", peer_id="alice")
    assert payload == {cap: [{"text": "approved context"}]}
    assert len(calls) == 1
    assert calls[0]["umo"] == "room"


@pytest.mark.asyncio
async def test_model_context_keeps_long_memory_and_slang_meaning_and_can_omit_memories():
    class FullAPI(API):
        async def get_approved_memories(self, **kwargs):
            return [{"text": "成员喜欢用 Python 讨论异步编程，回答时保留完整代码示例。"}]

        async def list_approved_slang(self, **kwargs):
            return [{"term": "咕咕", "meaning": "推迟约定", "approved": True}]

    bridge = SelfLearningBridge(context(metadata(FullAPI())))
    payload = await bridge.model_context(umo="room", peer_id="alice")
    assert "完整代码示例" in payload["memories"][0]["text"]
    assert payload["slang"][0]["meaning"] == "推迟约定"
    assert "memories" not in await bridge.model_context(umo="room", peer_id="alice", allow_memories=False)


@pytest.mark.asyncio
async def test_relationship_context_skips_native_per_provider_and_filters_scope():
    calls = []

    class Relationships:
        async def get_relationship_hints(self, *, umo, user_id, limit):
            calls.append((umo, user_id))
            return [{"hint": "经常讨论编程", "user_id": user_id},
                    {"hint": "other", "umo": "foreign"},
                    {"hint": "other user", "user_id": "foreign"}]

    native = Relationships()
    native.inject_diversity_to_llm_request = lambda *args: None
    native._hook_handler = object()
    bridge = SelfLearningBridge(context(metadata(native), metadata(Relationships(), "LivingMemory")))
    rows = await bridge.relationship_context(umo="room", peer_id="alice")
    assert rows == [{"hint": "经常讨论编程", "user_id": "alice"}]
    assert calls == [("room", "alice")]
    assert bridge.snapshot()["providers"][1]["direct_methods"]["relationships"] == ["get_relationship_hints"]


@pytest.mark.asyncio
async def test_relationship_hints_reach_main_request():
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin, MockEvent

    class Relationships:
        async def get_relationship_hints(self, *, umo, user_id, limit):
            return [{"hint": "熟悉的技术讨论伙伴"}]

    plugin = _plugin()
    companion = Relationships()
    plugin.context.get_all_stars = lambda: [metadata(companion)]
    plugin.selflearning = SelfLearningBridge(plugin.context)
    request = NS(prompt="original", extra_user_content_parts=[])
    await plugin.on_llm_request(MockEvent("hello"), request)
    assert "熟悉的技术讨论伙伴" in str(request.prompt) + str(request.extra_user_content_parts)
    await plugin.terminate()


@pytest.mark.asyncio
async def test_only_successful_owned_sends_are_learned_without_mutating_event(monkeypatch):
    import astrbot_plugin_chat_dynamics.main as main_module
    from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult, chain_plain_text
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin, MockEvent, _session_key

    captured = []

    class Capture:
        async def on_bot_message_sent(self, event):
            captured.append((event.unified_msg_origin, chain_plain_text(event.get_result())))

    plugin = _plugin()
    companion = Capture()
    plugin.context.get_all_stars = lambda: [metadata(companion)]
    key = _session_key("delivery")
    runtime = plugin._get_or_create_runtime(key, group_id="delivery", umo=key, bot_id="bot")
    event = MockEvent("request", group_id="delivery")
    event.set_result("original draft")
    original = event.get_result()

    async def failure(*args, **kwargs):
        return SendResult(False)

    monkeypatch.setattr(main_module, "send_plain", failure)
    assert not (await plugin._send_owned(runtime, event, "failed")).success
    assert not plugin.selflearning._pending

    async def success(*args, **kwargs):
        return SendResult(True, "sent-id")

    monkeypatch.setattr(main_module, "send_plain", success)
    assert (await plugin._send_owned(runtime, event, "delivered")).success
    await asyncio.gather(*tuple(plugin.selflearning._pending))
    assert captured == [(event.unified_msg_origin, "delivered")]
    assert event.get_result() is original
    await plugin.terminate()


@pytest.mark.asyncio
async def test_delivery_callback_cancelled_on_unload():
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent
    started, finished = asyncio.Event(), asyncio.Event()

    class Capture:
        async def on_bot_message_sent(self, event):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    bridge = SelfLearningBridge(context(metadata(Capture())))
    bridge.note_delivered(MockEvent("request"), "sent")
    await started.wait()
    await bridge.close()
    assert finished.is_set()
    assert not bridge._pending


def test_sdk_shape_strict_signature_and_disabled_plugin():
    bridge = SelfLearningBridge(context(metadata(API())))
    assert (
        bridge.fetch_approved_memories(umo="a:GroupMessage:g", peer_id="u")[0]["tag"]
        == "calm"
    )
    assert bridge.snapshot()["status"] == "connected"
    bridge.context = context(metadata(API(), activated=False))
    assert bridge.fetch_approved_memories(umo="a:GroupMessage:g", peer_id="u") == []
    assert bridge.snapshot()["status"] == "missing"


def test_two_plugins_dont_mask_each_other_and_report_partial_support():
    bridge = SelfLearningBridge(
        context(metadata(object()), metadata(API(), "LivingMemory"))
    )
    assert bridge.fetch_approved_memories(umo="g", peer_id="u")
    snap = bridge.snapshot()
    assert len(snap["providers"]) == 2
    assert snap["status"] == "degraded"
    assert snap["providers"][0]["detail"] == "plugin_found_no_api"


@pytest.mark.parametrize(
    "name",
    ["self-learning", "astrbot_plugin_self_learning", "LivingMemory", "liveingmemory"],
)
def test_names_and_dict_registries(name):
    bridge = SelfLearningBridge(
        NS(loaded_plugins={name: {"star_cls": API(), "activated": True}})
    )
    assert bridge.fetch_approved_memories(umo="g", peer_id="u")


@pytest.mark.asyncio
async def test_async_is_awaited_and_sync_legacy_does_not_leak_coroutines():
    calls = []

    class AsyncAPI:
        async def get_approved_memories(self, *, umo, user_id, limit):
            calls.append((umo, user_id))
            return [{"tag": "calm"}]

    bridge = SelfLearningBridge(context(metadata(AsyncAPI())))
    assert bridge.fetch_approved_memories(umo="g", peer_id="u") == []
    assert not calls
    assert await bridge.fetch_approved_memories_async(umo="g", peer_id="u") == [
        {"tag": "calm"}
    ]
    assert calls == [("g", "u")]
    assert bridge.snapshot()["status"] == "connected"


@pytest.mark.asyncio
async def test_errors_survive_snapshots_and_success_recovers():
    class Flaky:
        broken = True

        def get_approved_memories(self, *, umo, peer_id, limit):
            if self.broken:
                raise TypeError("internal error with secret text")
            return []

    api = Flaky()
    bridge = SelfLearningBridge(context(metadata(api)))
    assert await bridge.fetch_approved_memories_async(umo="g") == []
    for _ in range(2):
        snap = bridge.snapshot()
        assert snap["status"] == "degraded"
        assert snap["detail"] == "call_error:TypeError"
        assert "secret" not in str(snap)
    api.broken = False
    assert await bridge.fetch_approved_memories_async(umo="g") == []
    assert bridge.snapshot()["status"] == "connected"


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_cancellation_propagates():
    done = asyncio.Event()

    class Slow:
        async def get_approved_memories(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                done.set()

    bridge = SelfLearningBridge(context(metadata(Slow())))
    bridge.timeout = 0.01
    assert await bridge.fetch_approved_memories_async(umo="g") == []
    assert done.is_set()
    assert "TimeoutError" in bridge.snapshot()["detail"]
    bridge.timeout = 10
    task = asyncio.create_task(bridge.fetch_approved_memories_async(umo="g"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["disable", "reload"])
async def test_inflight_result_discarded_after_disable_or_reload(action):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow:
        async def get_approved_memories(self, **kwargs):
            started.set()
            await release.wait()
            return [{"tag": "stale"}]

    bridge = SelfLearningBridge(context(metadata(Slow())))
    task = asyncio.create_task(bridge.fetch_approved_memories_async(umo="g"))
    await started.wait()
    if action == "disable":
        bridge.configure(enabled=False)
    else:
        bridge.context = context(metadata(API()))
    release.set()
    assert await task == []


def test_native_hooks_are_not_called_and_initialization_is_observed():
    def forbidden(*args):
        raise AssertionError("host owns native dispatch")

    learning = NS(inject_diversity_to_llm_request=forbidden, _hook_handler=object())
    memory = NS(handle_memory_recall=forbidden, initializer=NS(is_initialized=False))
    bridge = SelfLearningBridge(
        context(metadata(learning), metadata(memory, "LivingMemory"))
    )
    assert bridge.snapshot()["detail"] == "plugin_initializing"
    memory.initializer.is_initialized = True
    snap = bridge.snapshot()
    assert snap["detail"] == "native_hooks"
    assert snap["lamp"] == "原生钩子"
    assert all(p["mode"] == "native_hooks" for p in snap["providers"])
    assert bridge.fetch_approved_memories(umo="g") == []
    assert bridge.uses_native_hooks()


def test_unrelated_api_attribute_is_not_a_connection():
    bridge = SelfLearningBridge(context(metadata(NS(api=object()))))
    assert bridge.snapshot()["detail"] == "plugin_found_no_api"


def test_no_unscoped_retry_and_no_internal_typeerror_double_call():
    calls = []

    class Bad:
        def get_approved_memories(self, **kwargs):
            calls.append(kwargs)
            raise TypeError("internal")

    bridge = SelfLearningBridge(context(metadata(Bad())))
    assert bridge.fetch_approved_memories(umo="g") == []
    assert len(calls) == 1

    class Global:
        def get_approved_memories(self, limit):
            raise AssertionError("must not query globally")

    bridge.context = context(metadata(Global()))
    assert bridge.fetch_approved_memories(umo="g") == []


def test_scope_and_approval_filter():
    class Scoped:
        def fetch_memories(self, **kwargs):
            return [
                {"tag": "ok", "approved": True},
                {"tag": "pending"},
                {"tag": "other", "approved": True, "umo": "other"},
                {"tag": "other-user", "approved": True, "user_id": "other"},
            ]

    bridge = SelfLearningBridge(context(metadata(Scoped())))
    assert bridge.fetch_approved_memories(umo="g", peer_id="u") == [
        {"tag": "ok", "approved": True}
    ]


@pytest.mark.asyncio
async def test_slang_candidates_must_be_approved(tmp_path):
    class Slang:
        async def get_slang_candidates(self, *, umo, limit):
            return [
                {"phrase": "hello", "approved": False},
                {"phrase": "hey", "approved": True},
            ]

    bridge = SelfLearningBridge(context(metadata(Slang())))
    nb = GroupMemoryNotebook(tmp_path, bridge=bridge)
    nb.configure(slang_enabled=True)
    with pytest.raises(ValueError, match="unapproved"):
        await nb.add_slang_trial_async("g", phrase="hello")
    assert (await nb.add_slang_trial_async("g", phrase="hey"))["approved"]


@pytest.mark.asyncio
async def test_forget_while_recalling_wins_and_bad_weights_are_ignored(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Memory:
        async def get_approved_memories(self, **kwargs):
            started.set()
            await release.wait()
            return [{"tag": "calm"}, {"tag": "bad", "weight": "NaN"}]

    store = MoodMemoryStore(
        tmp_path, bridge=SelfLearningBridge(context(metadata(Memory())))
    )
    store.configure(enabled=True)
    task = asyncio.create_task(store.recall_async("g", "u"))
    await started.wait()
    store.forget("g", "u")
    release.set()
    assert await task == []
    assert (
        store.recall("other", "u", remote_rows=[{"tag": "calm", "weight": "oops"}])
        == []
    )


@pytest.mark.asyncio
async def test_request_consumes_async_tags_and_preserves_other_injection(tmp_path):
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import (
        _plugin,
        MockEvent,
    )

    plugin = _plugin({"mood_memory_enabled": True})
    companion = metadata(API())
    plugin.context.get_all_stars = lambda: [companion]
    bridge = SelfLearningBridge(plugin.context)
    plugin.selflearning = bridge
    plugin.mood_memory = MoodMemoryStore(tmp_path, bridge=bridge)
    plugin.mood_memory.configure(enabled=True)
    request = NS(
        prompt="original memory from another plugin", extra_user_content_parts=[]
    )
    await plugin.on_llm_request(MockEvent("hello"), request)
    text = str(request.prompt) + str(request.extra_user_content_parts)
    assert "calm" in text
    assert "original memory from another plugin" in text


@pytest.mark.asyncio
async def test_unchanged_config_does_not_invalidate_inflight_and_close_waits():
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Slow:
        async def get_approved_memories(self, **kwargs):
            try:
                started.set()
                await release.wait()
                return [{"tag": "calm"}]
            finally:
                finished.set()

    ctx = context(metadata(Slow()))
    bridge = SelfLearningBridge(ctx)
    task = asyncio.create_task(bridge.fetch_approved_memories_async(umo="g"))
    await started.wait()
    bridge.configure(enabled=True, context=ctx)
    release.set()
    assert await task == [{"tag": "calm"}]
    started.clear()
    release.clear()
    finished.clear()
    task = asyncio.create_task(bridge.fetch_approved_memories_async(umo="g"))
    await started.wait()
    await bridge.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert not bridge._pending


@pytest.mark.asyncio
async def test_native_owner_prevents_duplicate_direct_memory_reads(tmp_path):
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import (
        _plugin,
        MockEvent,
    )

    class Native(API):
        _hook_handler = object()

        async def inject_diversity_to_llm_request(self, event, req):
            raise AssertionError("Only the host dispatches this")

        def get_approved_memories(self, **kwargs):
            raise AssertionError("Do not duplicate native recall")

    plugin = _plugin({"mood_memory_enabled": True})
    companion = metadata(Native())
    plugin.context.get_all_stars = lambda: [companion]
    plugin.mood_memory = MoodMemoryStore(tmp_path, bridge=plugin.selflearning)
    plugin.mood_memory.configure(enabled=True)
    request = NS(prompt="native content", extra_user_content_parts=[])
    await plugin.on_llm_request(MockEvent("hello"), request)
    assert "native content" in request.prompt
    assert plugin.selflearning.snapshot()["status"] == "connected"


def test_relationship_only_api_and_umo_alias_are_supported():
    class Relationships:
        def relationship_snapshot(self, *, unified_msg_origin, user_id, limit):
            return [
                {"umo": unified_msg_origin, "user_id": user_id, "relation": "friend"}
            ]

    bridge = SelfLearningBridge(context(metadata(Relationships())))
    assert (
        bridge.fetch_relationship_hints(umo="g", peer_id="u")[0]["relation"] == "friend"
    )
    assert bridge.snapshot()["providers"][0]["capabilities"] == ["relationships"]


@pytest.mark.asyncio
async def test_reset_cancels_direct_recall_before_request_mutation(tmp_path):
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import (
        _plugin,
        MockEvent,
    )

    started = asyncio.Event()

    class Slow:
        async def get_approved_memories(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

    plugin = _plugin({"mood_memory_enabled": True})
    companion = metadata(Slow())
    plugin.context.get_all_stars = lambda: [companion]
    plugin.mood_memory = MoodMemoryStore(tmp_path, bridge=plugin.selflearning)
    plugin.mood_memory.configure(enabled=True)
    event = MockEvent("hello", group_id="reset-memory")
    key = event.unified_msg_origin
    plugin._get_or_create_runtime(key, group_id="reset-memory", umo=key, bot_id="bot")
    request = NS(prompt="original", extra_user_content_parts=[])
    task = asyncio.create_task(plugin.on_llm_request(event, request))
    await started.wait()
    await plugin._reset_session_state_async(key)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert request.prompt == "original" and request.extra_user_content_parts == []


def test_dashboard_does_not_claim_native_dispatch_for_legacy_exclusive():
    from astrbot_plugin_chat_dynamics.core.dashboard import companion_snapshot

    plugin = NS(
        handle_memory_recall=lambda *_: None, initializer=NS(is_initialized=True)
    )
    bridge = SelfLearningBridge(context(metadata(plugin, "LivingMemory")))
    host = NS(selflearning=bridge, decision_mode="legacy", pipeline_mode="exclusive")
    assert companion_snapshot(host)["detail"] == "native_hooks_bypassed"
    host.pipeline_mode = "filter"
    assert companion_snapshot(host)["detail"] == "native_hooks"


def test_registry_failure_is_not_reported_as_uninstalled():
    def broken():
        raise RuntimeError("host unavailable")

    bridge = SelfLearningBridge(NS(get_all_stars=broken))
    assert bridge.snapshot()["detail"] == "detect_error:RuntimeError"
    assert bridge.status.value == "degraded"
