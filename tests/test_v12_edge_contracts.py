from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import astrbot_plugin_chat_dynamics.core.web_api as web_api
from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter, _extract_vector, _provider_id
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult, is_streaming_host_result
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key


def _streaming_finish_result(text: str):
    try:
        from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType

        return MessageEventResult().message(text).set_result_content_type(ResultContentType.STREAMING_FINISH)
    except ImportError:
        result = SimpleNamespace(
            chain=[text],
            result_content_type=SimpleNamespace(name="STREAMING_FINISH"),
        )
        result.get_plain_text = lambda: text
        return result


@pytest.mark.asyncio
async def test_decorating_hook_leaves_streaming_finish_untouched():
    plugin = _plugin()
    key = _session_key("native-stream")
    runtime = plugin._get_or_create_runtime(key, group_id="native-stream", umo=key, bot_id="bot_42")
    event = MockEvent("trigger", group_id="native-stream", message_id="in", self_id="bot_42")
    original = _streaming_finish_result("同一答案")
    event.set_result(original)

    def boom(*_args, **_kwargs):
        raise AssertionError("streaming finish must not be re-fragmented")

    plugin.pacer.shape_and_fragment = boom
    await plugin.on_decorating_result(event)
    bound = event.get_result()
    assert bound is original
    assert is_streaming_host_result(bound)
    await event.send(event.get_result())
    await plugin.after_message_sent(event)
    assert event.replies_sent == ["同一答案"]
    assert not runtime.followup_queue
    assert not runtime.active_followup_batches
    assert runtime.last_bot_node is not None
    assert runtime.last_bot_node.text == "同一答案"


@pytest.mark.asyncio
async def test_native_hook_records_first_and_tail_fragments_in_order(monkeypatch):
    plugin = _plugin()
    key = _session_key("native-order")
    runtime = plugin._get_or_create_runtime(key, group_id="native-order", umo=key, bot_id="bot_42")
    event = MockEvent("trigger", group_id="native-order", message_id="in", self_id="bot_42")
    event.set_result("native response")
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["first", "tail"]
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 0.0
    plugin.pacer.calculate_typing_delay = lambda *_args, **_kwargs: 0.0

    await plugin.on_decorating_result(event)
    assert event.get_result() == "first"
    await event.send(event.get_result())

    await plugin.after_message_sent(event)
    assert event.replies_sent[0] == "first"
    assert event.replies_sent[-1].endswith("tail")
    assert [node.text for node in runtime.dag.get_recent_nodes(limit=4)] == ["first", "tail"]
    assert runtime.last_bot_node is not None and runtime.last_bot_node.text == "tail"


@pytest.mark.asyncio
async def test_native_tail_failure_stops_batch_without_creating_failed_node(monkeypatch):
    plugin = _plugin()
    key = _session_key("native-failure")
    runtime = plugin._get_or_create_runtime(key, group_id="native-failure", umo=key, bot_id="bot_42")
    event = MockEvent("trigger", group_id="native-failure", message_id="in", self_id="bot_42")
    event.set_result("native response")
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["first", "tail"]
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 0.0
    plugin.pacer.calculate_typing_delay = lambda *_args, **_kwargs: 0.0

    async def failed_send(*_args, **_kwargs):
        return SendResult(False, error="rejected")

    monkeypatch.setattr(plugin, "_send_owned", failed_send)
    await plugin.on_decorating_result(event)
    await event.send(event.get_result())
    await plugin.after_message_sent(event)

    assert [node.text for node in runtime.dag.get_recent_nodes(limit=4)] == ["first"]
    assert runtime.last_bot_node is not None and runtime.last_bot_node.text == "first"
    assert plugin._metrics["send_failed"] >= 1


@pytest.mark.asyncio
async def test_native_fallback_ids_remain_unique_when_adapter_returns_no_id(monkeypatch):
    plugin = _plugin()
    key = _session_key("native-no-id")
    runtime = plugin._get_or_create_runtime(key, group_id="native-no-id", umo=key, bot_id="bot_42")
    event = MockEvent("trigger", group_id="native-no-id", message_id="in", self_id="bot_42")
    event.set_result("native response")
    plugin.pacer.shape_and_fragment = lambda *_args, **_kwargs: ["first", "tail"]
    plugin.pacer.calculate_inter_burst_delay = lambda *_args, **_kwargs: 0.0

    async def send_without_id(*_args, **_kwargs):
        return SendResult(True)

    monkeypatch.setattr(plugin, "_send_owned", send_without_id)
    await plugin.on_decorating_result(event)
    await event.send(event.get_result())
    await plugin.after_message_sent(event)

    nodes = runtime.dag.get_recent_nodes(limit=4)
    assert [node.text for node in nodes] == ["first", "tail"]
    assert nodes[0].msg_id != nodes[1].msg_id
    assert nodes[1].reply_to_id == nodes[0].msg_id


@pytest.mark.asyncio
async def test_llm_hooks_inject_and_shape_only_in_live_mode():
    plugin = _plugin()
    event = MockEvent("小助手你好", group_id="hook-live")
    request = SimpleNamespace(prompt="hello", extra_user_content_parts=[])
    await plugin.on_llm_request(event, request)
    assert request.prompt != "hello" or request.extra_user_content_parts

    response = SimpleNamespace(completion_text="回答内容。\n如果还有问题随时问我")
    await plugin.on_llm_response(event, response)
    assert "随时问我" not in response.completion_text

    shadow = _plugin({"shadow_mode": True})
    shadow_request = SimpleNamespace(prompt="hello", extra_user_content_parts=[])
    shadow_response = SimpleNamespace(completion_text="如果还有问题随时问我")
    await shadow.on_llm_request(event, shadow_request)
    await shadow.on_llm_response(event, shadow_response)
    assert shadow_request.prompt == "hello"
    assert shadow_response.completion_text == "如果还有问题随时问我"


@pytest.mark.asyncio
async def test_llm_hooks_ignore_an_event_from_an_old_epoch():
    plugin = _plugin()
    key = _session_key("hook-stale")
    event = MockEvent("小助手你好", group_id="hook-stale", message_id="stale", self_id="bot")
    await plugin.on_group_message(event)
    await plugin._reset_session_state_async(key)
    request = SimpleNamespace(prompt="hello", extra_user_content_parts=[])
    response = SimpleNamespace(completion_text="原始结果")
    await plugin.on_llm_request(event, request)
    await plugin.on_llm_response(event, response)
    assert request.prompt == "hello"
    assert response.completion_text == "原始结果"


def test_web_api_registration_and_body_guards(monkeypatch):
    class FailingContext:
        def register_web_api(self, *_args):
            raise RuntimeError("registration failed")

    missing = web_api.ConsoleWebAPI(SimpleNamespace(context=object()))
    missing.register()
    assert missing.registered is False

    failing = web_api.ConsoleWebAPI(SimpleNamespace(context=FailingContext()))
    failing.register()
    assert failing.registered is False


def test_web_api_registration_retries_only_failed_endpoints():
    class FailOnceContext:
        def __init__(self):
            self.calls = []
            self.failed = False

        def register_web_api(self, route, *_args):
            self.calls.append(route)
            if route.endswith("/session") and not self.failed:
                self.failed = True
                raise RuntimeError("registration failed once")

    context = FailOnceContext()
    api = web_api.ConsoleWebAPI(SimpleNamespace(context=context))

    api.register()
    assert api.registered is False
    first_attempt = list(context.calls)
    assert first_attempt.count("/astrbot_plugin_chat_dynamics/session") == 1

    api.register()
    assert api.registered is True
    second_attempt = context.calls[len(first_attempt):]
    assert second_attempt == ["/astrbot_plugin_chat_dynamics/session"]

    api.register()
    assert context.calls == first_attempt + second_attempt


@pytest.mark.asyncio
async def test_web_api_json_body_rejects_large_invalid_and_non_object_payloads(monkeypatch):
    class Request:
        content_length = 70 * 1024

    monkeypatch.setattr(web_api, "request", Request())
    assert (await web_api._json_body())["__invalid_body__"] == "body too large"

    class SmallRequest:
        content_length = None

    monkeypatch.setattr(web_api, "request", SmallRequest())
    async def invalid_json(_default):
        raise ValueError("bad json")
    monkeypatch.setattr(web_api, "request_json", invalid_json)
    assert (await web_api._json_body())["__invalid_body__"] == "invalid JSON body"

    async def non_object(_default):
        return ["not", "an", "object"]
    monkeypatch.setattr(web_api, "request_json", non_object)
    assert (await web_api._json_body())["__invalid_body__"] == "JSON body must be an object"

    async def huge_object(_default):
        return {"x": "a" * (70 * 1024)}
    monkeypatch.setattr(web_api, "request_json", huge_object)
    assert (await web_api._json_body())["__invalid_body__"] == "body too large"


@pytest.mark.asyncio
async def test_web_api_snapshot_and_session_validation_errors(monkeypatch, offline_web_responses):
    plugin = _plugin()
    api = plugin._web

    monkeypatch.setattr(web_api, "snapshot_overview", lambda _plugin: (_ for _ in ()).throw(RuntimeError()))
    overview = await api.overview()
    assert overview["status_code"] == 503

    monkeypatch.setattr(web_api, "snapshot_sessions", lambda _plugin: (_ for _ in ()).throw(RuntimeError()))
    sessions = await api.sessions()
    assert sessions["status_code"] == 503

    async def empty_body(_default):
        return {}
    monkeypatch.setattr(web_api, "request_json", empty_body)
    missing = await api.session()
    assert missing["status_code"] == 400

    async def long_body(_default):
        return {"session_key": "x" * 257}
    monkeypatch.setattr(web_api, "request_json", long_body)
    too_long = await api.session()
    assert too_long["status_code"] == 400

    key = _session_key("api-errors")
    plugin._get_or_create_runtime(key, group_id="api-errors", umo=key, bot_id="bot")
    monkeypatch.setattr(web_api, "snapshot_session_or_none", lambda *_args: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(web_api, "query_value", lambda name: key if name == "session_key" else "")
    failed_snapshot = await api.session()
    assert failed_snapshot["status_code"] == 503 if "status_code" in failed_snapshot else failed_snapshot["ok"] is True

    async def unknown_fields(_default):
        return {"session_key": key, "unexpected": True}

    monkeypatch.setattr(web_api, "query_value", lambda _name: "")
    monkeypatch.setattr(web_api, "request_json", unknown_fields)
    rejected = await api.session()
    assert rejected["status_code"] == 400


@pytest.mark.asyncio
async def test_web_api_operation_snapshot_failures_are_stable(monkeypatch, offline_web_responses):
    plugin = _plugin({"takeover_all": True})
    key = _session_key("api-operation-errors")
    plugin._get_or_create_runtime(key, group_id="api-operation-errors", umo=key, bot_id="bot")

    async def body():
        return {"session_key": key, "minutes": 15}

    monkeypatch.setattr(web_api, "_json_body", body)
    monkeypatch.setattr(web_api, "snapshot_session_or_none", lambda *_args: (_ for _ in ()).throw(RuntimeError()))
    assert (await plugin._web.cool())["status_code"] == 503

    async def reset_body():
        return {"session_key": key}

    monkeypatch.setattr(web_api, "_json_body", reset_body)
    monkeypatch.setattr(plugin, "_reset_session_state_async", lambda *_args: asyncio.sleep(0, result=None))
    assert (await plugin._web.reset())["status_code"] == 503

    monkeypatch.setattr(plugin, "preset_catalog", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert (await plugin._web.presets())["status_code"] == 503


@pytest.mark.asyncio
async def test_web_api_reset_and_preset_error_paths(monkeypatch, offline_web_responses):
    plugin = _plugin()
    api = plugin._web

    plugin._shutting_down = True
    assert (await api.presets())["status_code"] == 503
    assert (await api.apply_preset())["status_code"] == 503
    plugin._shutting_down = False

    async def invalid_body(_default):
        return {"__invalid_body__": "invalid JSON body"}
    monkeypatch.setattr(web_api, "request_json", invalid_body)
    assert (await api.reset())["status_code"] == 400

    async def non_string_name(_default):
        return {"name": 1, "confirm": True}
    monkeypatch.setattr(web_api, "request_json", non_string_name)
    assert (await api.apply_preset())["status_code"] == 400

    async def unknown_name(_default):
        return {"name": "missing", "confirm": True}
    monkeypatch.setattr(web_api, "request_json", unknown_name)
    assert (await api.apply_preset())["status_code"] == 400

    async def long_name(_default):
        return {"name": "x" * 33, "confirm": True}
    monkeypatch.setattr(web_api, "request_json", long_name)
    assert (await api.apply_preset())["status_code"] == 400


def test_embedding_payload_and_provider_resolution_edges():
    class MetaProvider:
        class Meta:
            id = "meta-id"
        meta = Meta()

    assert _provider_id(MetaProvider()) == "meta-id"
    assert _extract_vector({"vector": [3.0, 4.0]})[-1] == pytest.approx(0.8)

    class Provider:
        provider_id = "configured"
        async def get_embeddings(self, texts):
            return [[1.0, 0.0] for _ in texts]

    class Context:
        def get_provider_by_id(self, provider_id):
            return Provider() if provider_id == "configured" else None

    adapter = EmbeddingAdapter(Context(), enabled=True, provider_id="configured")
    assert adapter.resolve_provider() is not None


@pytest.mark.asyncio
async def test_embedding_async_provider_and_inflight_failure_paths():
    calls = []

    class Provider:
        async def get_embeddings(self, texts):
            calls.extend(texts)
            await asyncio.sleep(0)
            return [[0.0, 1.0] for _ in texts]

    class Context:
        def get_all_embedding_providers(self):
            return [Provider()]

    adapter = EmbeddingAdapter(Context(), enabled=True)
    first, second = await asyncio.gather(adapter.embed("same"), adapter.embed("same"))
    assert first == second and calls == ["same"]

    class BrokenProvider:
        async def get_embedding(self, _text):
            raise RuntimeError("provider down")

    broken = EmbeddingAdapter(SimpleNamespace(get_all_embedding_providers=lambda: [BrokenProvider()]), enabled=True)
    assert await broken.embed("broken") is None
    assert broken.last_backend == "hashed"
