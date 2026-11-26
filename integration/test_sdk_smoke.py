"""Smoke tests that import and exercise the real AstrBot SDK.

This directory intentionally sits outside ``tests/`` so its test doubles in
``tests/conftest.py`` cannot satisfy these imports.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


try:
    astrbot = importlib.import_module("astrbot")
except ImportError:
    if os.environ.get("CHAT_DYNAMICS_REQUIRE_REAL_SDK") == "1":
        raise RuntimeError("real AstrBot SDK is required for this integration job")
    pytest.skip("real AstrBot SDK is not installed", allow_module_level=True)
if getattr(astrbot, "__spec__", None) is None:
    if os.environ.get("CHAT_DYNAMICS_REQUIRE_REAL_SDK") == "1":
        raise RuntimeError("tests/conftest.py SDK double is not a real AstrBot installation")
    pytest.skip("tests/conftest.py SDK double is not a real AstrBot installation", allow_module_level=True)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLUGIN_DIR.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from astrbot_plugin_chat_dynamics.core.platform_bridge import (  # noqa: E402
    build_plain_chain,
    chain_plain_text,
    send_plain,
)
from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin  # noqa: E402


def test_real_reply_component_preserves_quote_author():
    from types import SimpleNamespace
    from astrbot.api.message_components import At, Reply
    from astrbot_plugin_chat_dynamics.core.platform_bridge import parse_group_event

    event = SimpleNamespace(
        message_str="请继续解释", message_id="new-message",
        get_group_id=lambda: "group", get_sender_id=lambda: "user",
        get_self_id=lambda: "42",
        get_messages=lambda: [Reply(id="old-message", sender_id=42), At(qq="42")],
    )
    parsed = parse_group_event(event)
    assert parsed.reply_to_id == "old-message"
    assert parsed.reply_sender_id == "42"
    assert parsed.mentions == ["42"]


class SmokeContext:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any, list[str], str]] = []
        self.sent: list[tuple[str, str]] = []
        self.generate_kwargs: dict[str, Any] = {}

    def register_web_api(self, route, handler, methods, desc) -> None:
        self.routes.append((route, handler, methods, desc))

    async def get_current_chat_provider_id(self, _umo=None) -> str:
        return "smoke-provider"

    async def llm_generate(self, **kwargs):
        self.generate_kwargs = kwargs
        return type("Response", (), {"completion_text": "smoke response"})()

    async def tool_loop_agent(self, **kwargs):
        self.generate_kwargs = kwargs
        return type("Response", (), {"completion_text": "smoke response"})()

    async def send_message(self, target, chain):
        self.sent.append((str(target), chain_plain_text(chain)))
        return None


class SmokeEvent:
    message_str = "小助手稍等"
    message_id = "smoke-message"
    unified_msg_origin = "smoke:GroupMessage:room"
    is_at_or_wake_command = False

    def __init__(self) -> None:
        self.stopped = False
        self.call_llm = False
        self.sent: list[str] = []
        self.message_obj = type("MessageObject", (), {"message_id": self.message_id, "message": []})()

    def get_group_id(self) -> str:
        return "room"

    def get_sender_id(self) -> str:
        return "user"

    def get_self_id(self) -> str:
        return "bot"

    def get_messages(self) -> list[Any]:
        return []

    def get_message_outline(self) -> str:
        return self.message_str

    def stop_event(self) -> None:
        self.stopped = True

    def should_call_llm(self, call_llm: bool) -> None:
        self.call_llm = bool(call_llm)

    async def send(self, chain):
        self.sent.append(chain_plain_text(chain))
        return None


class MediaSmokeEvent(SmokeEvent):
    def __init__(self, components: list[Any]) -> None:
        super().__init__()
        self.message_str = ""
        self.message_obj.message = components

    def get_messages(self) -> list[Any]:
        return list(self.message_obj.message)


async def _decode_web_response(response: Any) -> tuple[int, dict[str, Any]]:
    status_code = 200
    if isinstance(response, tuple):
        if len(response) >= 2 and isinstance(response[1], int):
            status_code = response[1]
        response = response[0]
    else:
        raw_status = getattr(response, "status_code", None)
        if isinstance(raw_status, int):
            status_code = raw_status
    if isinstance(response, dict):
        return status_code, response
    get_json = getattr(response, "get_json", None)
    if callable(get_json):
        payload = await get_json()
        return status_code, dict(payload)
    body = getattr(response, "body", b"")
    return status_code, dict(json.loads(bytes(body).decode("utf-8")))


async def _call_web_handler(
    handler: Any,
    *,
    method: str = "GET",
    path: str = "/",
    query: str = "",
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    raw_body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
    if importlib.util.find_spec("astrbot.api.web") is None:
        from quart import Quart

        app = Quart(__name__)
        url = f"{path}?{query}" if query else path
        headers = {"content-type": "application/json"} if json_body is not None else {}
        async with app.test_request_context(url, method=method, data=raw_body, headers=headers):
            response = await handler()
        return await _decode_web_response(response)

    from astrbot.api.web import PluginRequest, bind_request_context
    from starlette.requests import Request

    headers_list: list[tuple[bytes, bytes]] = [
        (b"host", b"127.0.0.1:6185"),
        (b"origin", b"http://127.0.0.1:6185"),
    ]
    if json_body is not None:
        headers_list.append((b"content-type", b"application/json"))
        headers_list.append((b"content-length", str(len(raw_body)).encode("ascii")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "headers": headers_list,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 6185),
    }

    sent = False

    async def _receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    starlette_req = Request(scope, _receive)
    plugin_req = PluginRequest(starlette_req, plugin_name="astrbot_plugin_chat_dynamics")
    with bind_request_context(plugin_req):
        response = await handler()
    return await _decode_web_response(response)


async def _overview_payload(plugin: ChatDynamicsPlugin) -> dict[str, Any]:
    _status, payload = await _call_web_handler(plugin.web_api_overview, method="GET", path="/overview")
    return payload


@pytest.mark.asyncio
async def test_real_sdk_plugin_contract_smoke(monkeypatch):
    context = SmokeContext()
    plugin = ChatDynamicsPlugin(
        context=context,
        config={"enable": True, "takeover_all": True, "vibe_llm_enabled": False},
    )

    await plugin.initialize()

    async def _allow_admin(*_a, **_kw):
        return True

    async def _deny_admin(*_a, **_kw):
        return False

    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", _allow_admin)
    assert {(route.split("/series_workspace/")[1], tuple(methods))
            for route, _, methods, _ in context.routes if "/series_workspace/" in route} == {
        ("open/<target>", ("GET",)),
        ("panel/<target>/<path:operation>", ("GET", "POST", "PUT", "PATCH", "DELETE")),
    }
    assert {(route.split("/setup/", 1)[1], tuple(methods))
            for route, _, methods, _ in context.routes if "/setup/" in route} == {
        ("groups", ("GET",)),
        ("probe", ("POST",)),
    }
    routes_by_tail = {
        route.rsplit("/", 1)[-1]: (handler, tuple(methods))
        for route, handler, methods, _ in context.routes
        if "/decision/" not in route
        and "/series_workspace/" not in route
        and "/setup/" not in route
    }
    assert set(routes_by_tail) == {
        "overview",
        "sessions",
        "session",
        "cool",
        "reset",
        "presets",
        "apply",
        "config",
        "notebook",
        "read_air",
        "page_nav",
        "ui_preferences",
        "providers",
        "replay",
        "topic_annotations",
        "series_presence",
        "series_health",
        "series_preflight",
        "series_matrix",
        "series_matrix_toggle",
        "series_matrix_batch",
        "series_search",
        "series_telemetry",
        "series_templates",
        "narrative",
        "integrations",
    }
    assert routes_by_tail["series_matrix"][1] == ("GET",)
    assert routes_by_tail["series_matrix_toggle"][1] == ("POST",)
    assert routes_by_tail["series_matrix_batch"][1] == ("POST",)
    assert routes_by_tail["series_search"][1] == ("GET",)
    assert routes_by_tail["series_telemetry"][1] == ("GET",)
    template_methods = {
        tuple(methods)
        for route, _, methods, _ in context.routes
        if route.rsplit("/", 1)[-1] == "series_templates"
    }
    assert template_methods == {("GET",), ("POST",), ("DELETE",)}
    assert not any("/learning/" in route for route, *_ in context.routes)
    assert {route.split("/decision/", 1)[1] for route, *_ in context.routes if "/decision/" in route} == {"status", "history"}
    overview = await _overview_payload(plugin)
    assert overview["ok"] is True
    assert overview["status"] == "ok"
    assert overview["data"]["enabled"] is True

    event = SmokeEvent()
    await plugin.on_group_message(event)
    assert event.stopped is False
    assert event.call_llm is True
    assert event.unified_msg_origin in plugin._sessions
    assert overview["data"]["pipeline_mode"] == "filter"

    # Verify the three v2 series routes under the real SDK request/response contract.
    matrix_handler = routes_by_tail["series_matrix"][0]
    toggle_handler = routes_by_tail["series_matrix_toggle"][0]
    search_handler = routes_by_tail["series_search"][0]

    # 1) series_matrix: parameter guard + normal aggregation payload
    bad_matrix_status, bad_matrix = await _call_web_handler(
        matrix_handler, method="GET", path="/series_matrix", query=f"persona={'x' * 129}"
    )
    assert bad_matrix_status == 400
    assert bad_matrix["status"] == "error"

    matrix_status, matrix_res = await _call_web_handler(
        matrix_handler, method="GET", path="/series_matrix"
    )
    assert matrix_status == 200
    assert matrix_res["ok"] is True
    assert matrix_res["status"] == "ok"
    assert len(matrix_res["data"]["plugins"]) == 8
    assert matrix_res["data"]["version"]
    room_row = next(g for g in matrix_res["data"]["groups"] if g["umo"] == event.unified_msg_origin)
    cd_cell = room_row["cells"]["astrbot_plugin_chat_dynamics"]
    assert cd_cell["enabled"] is True
    assert cd_cell["supported"] is True
    assert cd_cell["version"]

    # 2) series_search: parameter guard + federated search result shape
    bad_search_status, bad_search = await _call_web_handler(
        search_handler, method="GET", path="/series_search", query="q=room&limit=999"
    )
    assert bad_search_status == 400
    assert bad_search["status"] == "error"

    search_status, search_res = await _call_web_handler(
        search_handler, method="GET", path="/series_search", query="q=room&limit=10"
    )
    assert search_status == 200
    assert search_res["ok"] is True
    assert search_res["data"]["query"] == "room"
    assert search_res["data"]["timed_out_plugins"] == []
    assert isinstance(search_res["data"]["elapsed_ms"], int)
    assert any(
        item["plugin_id"] == "astrbot_plugin_chat_dynamics" and "room" in item["subtitle"]
        for item in search_res["data"]["items"]
    )

    # 3) series_matrix_toggle: admin gate (403) + field validation (400) + conflict (409) + valid toggle (200)
    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", _deny_admin)
    unauth_status, unauth_res = await _call_web_handler(
        toggle_handler,
        method="POST",
        path="/series_matrix_toggle",
        json_body={
            "umo": event.unified_msg_origin,
            "plugin_id": "astrbot_plugin_chat_dynamics",
            "enabled": False,
            "expected_version": cd_cell["version"],
        },
    )
    assert unauth_status == 403
    assert unauth_res["status"] == "error"

    async def _allow_admin():
        return True

    monkeypatch.setattr(plugin._web, "_narrative_admin_allowed", _allow_admin)

    bad_toggle_status, bad_toggle = await _call_web_handler(
        toggle_handler,
        method="POST",
        path="/series_matrix_toggle",
        json_body={"umo": event.unified_msg_origin},
    )
    assert bad_toggle_status == 400
    assert bad_toggle["status"] == "error"

    conflict_status, conflict_res = await _call_web_handler(
        toggle_handler,
        method="POST",
        path="/series_matrix_toggle",
        json_body={
            "umo": event.unified_msg_origin,
            "plugin_id": "astrbot_plugin_chat_dynamics",
            "enabled": False,
            "expected_version": "stale-version",
        },
    )
    assert conflict_status == 409
    assert conflict_res["status"] == "error"

    ok_toggle_status, ok_toggle = await _call_web_handler(
        toggle_handler,
        method="POST",
        path="/series_matrix_toggle",
        json_body={
            "umo": event.unified_msg_origin,
            "plugin_id": "astrbot_plugin_chat_dynamics",
            "enabled": False,
            "expected_version": cd_cell["version"],
        },
    )
    assert ok_toggle_status == 200
    assert ok_toggle["ok"] is True
    assert ok_toggle["data"]["ok"] is True
    assert ok_toggle["data"]["enabled"] is False
    assert ok_toggle["data"]["version"]
    assert ok_toggle["data"]["matrix_version"]
    assert plugin.is_group_takeover_enabled("room") is False

    # 4) series_telemetry: admin-gated window query & live queue shape
    telemetry_handler = routes_by_tail["series_telemetry"][0]
    bad_tel_status, _ = await _call_web_handler(
        telemetry_handler, method="GET", path="/series_telemetry", query="window=999d"
    )
    assert bad_tel_status == 400
    tel_status, tel_res = await _call_web_handler(
        telemetry_handler, method="GET", path="/series_telemetry", query="window=24h"
    )
    assert tel_status == 200
    assert tel_res["ok"] is True
    assert tel_res["data"]["window"] == "24h"
    assert "summary" in tel_res["data"]
    assert "live_queue" in tel_res["data"]

    # 5) series_templates: GET built-ins + POST custom + DELETE custom
    tpl_handlers = {
        tuple(methods)[0]: handler
        for route, handler, methods, _ in context.routes
        if route.rsplit("/", 1)[-1] == "series_templates"
    }
    tpl_get_status, tpl_get = await _call_web_handler(
        tpl_handlers["GET"], method="GET", path="/series_templates"
    )
    assert tpl_get_status == 200
    assert {t["id"] for t in tpl_get["data"]["templates"] if t["builtin"]} == {
        "full_companion",
        "light_chat",
        "observer_archive",
        "core_only",
    }
    tpl_post_status, tpl_post = await _call_web_handler(
        tpl_handlers["POST"],
        method="POST",
        path="/series_templates",
        json_body={
            "id": "smoke_custom",
            "name": "冒烟自定义",
            "description": "smoke",
            "states": {"astrbot_plugin_chat_dynamics": True},
        },
    )
    assert tpl_post_status == 200
    assert tpl_post["data"]["template"]["id"] == "smoke_custom"

    # 6) series_matrix_batch: apply template to room
    batch_handler = routes_by_tail["series_matrix_batch"][0]
    batch_status, batch_res = await _call_web_handler(
        batch_handler,
        method="POST",
        path="/series_matrix_batch",
        json_body={
            "umo": event.unified_msg_origin,
            "template_id": "core_only",
        },
    )
    assert batch_status == 200
    assert batch_res["data"]["ok"] is True
    assert batch_res["data"]["template_id"] == "core_only"
    assert plugin.is_group_takeover_enabled("room") is True

    tpl_del_status, tpl_del = await _call_web_handler(
        tpl_handlers["DELETE"],
        method="DELETE",
        path="/series_templates",
        query="id=smoke_custom",
    )
    assert tpl_del_status == 200
    assert tpl_del["data"]["deleted"] is True

    generated = await plugin.llm.generate(
        prompt="hello",
        umo=event.unified_msg_origin,
        system_prompt="system",
    )
    assert generated == "smoke response"
    assert context.generate_kwargs == {
        "chat_provider_id": "smoke-provider",
        "prompt": "hello",
        "system_prompt": "system",
    }

    chain = build_plain_chain("hello")
    assert chain_plain_text(chain) == "hello"
    result = await send_plain(event, context, event.unified_msg_origin, "hello")
    assert result.success is True
    assert event.sent == ["hello"]

    await plugin.terminate()
    assert plugin.debounce._is_closed is True
    assert not plugin._background_tasks
    assert not hasattr(plugin, "_vibe_llm_tasks")
    assert plugin.jev._closed is True
    assert not plugin._sessions


def test_real_sdk_media_components_are_classified_without_text_side_effects():
    import astrbot.api.message_components as message_components
    from astrbot_plugin_chat_dynamics.core.platform_bridge import parse_group_event

    names = ("Image", "Face", "Sticker", "Emoji", "Record", "Video", "File", "Forward", "Node", "Poke", "Json")
    available = []
    for name in names:
        component_type = getattr(message_components, name, None)
        if component_type is None:
            continue
        available.append(name)
        component = component_type.__new__(component_type)
        parsed = parse_group_event(MediaSmokeEvent([component]))
        if name == "Poke":
            assert parsed.has_media is False, name
            assert parsed.has_poke is True, name
            continue
        assert parsed.has_media is True, name
        assert parsed.media_only is True, name
    assert len(available) >= 6, "real SDK media component surface changed unexpectedly"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["filter", "exclusive"])
@pytest.mark.parametrize("trigger", ["at", "reply"])
async def test_owned_turn_blocks_real_host_default_agent_after_result_clear(mode, trigger):
    from types import SimpleNamespace
    from astrbot.api.message_components import At, Reply
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.pipeline.process_stage.stage import ProcessStage

    class HostEvent(SmokeEvent):
        stop_event = AstrMessageEvent.stop_event
        is_stopped = AstrMessageEvent.is_stopped
        set_result = AstrMessageEvent.set_result
        get_result = AstrMessageEvent.get_result
        clear_result = AstrMessageEvent.clear_result
        should_call_llm = AstrMessageEvent.should_call_llm

        def get_extra(self, key, default=None):
            return default

        def get_messages(self):
            return self.message_obj.message

    event = HostEvent()
    event._result = None
    event._force_stopped = False
    event._has_send_oper = False
    event.is_at_or_wake_command = True
    event.message_obj.message = [At(qq="bot") if trigger == "at" else Reply(id="bot-message")]
    plugin = ChatDynamicsPlugin(context=SmokeContext(), config={
        "enable": True, "takeover_all": True, "pipeline_mode": mode,
        "debounce_base_cooldown": 10, "vibe_llm_enabled": False,
    })
    calls = []

    class AgentStage:
        async def process(self, incoming):
            calls.append(incoming)
            yield

    stage = ProcessStage()
    stage.ctx = SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
    stage.agent_sub_stage = AgentStage()
    try:
        await plugin.on_group_message(event)
        assert plugin.debounce.get_pending_count(event.unified_msg_origin) == 1
        event.clear_result()
        async for _ in stage.process(event):
            pass
        assert calls == []

        # Control: the real host stage starts a duplicate request without the
        # suppression flag, even when exclusive stop state was set at ingress.
        event.should_call_llm(False)
        async for _ in stage.process(event):
            pass
        assert calls == [event]
    finally:
        await plugin.terminate()
