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


async def _overview_payload(plugin: ChatDynamicsPlugin) -> dict[str, Any]:
    if importlib.util.find_spec("astrbot.api.web") is None:
        from quart import Quart

        app = Quart(__name__)
        async with app.app_context():
            response = await plugin.web_api_overview()
    else:
        response = await plugin.web_api_overview()

    if isinstance(response, tuple):
        response = response[0]
    if isinstance(response, dict):
        return response
    get_json = getattr(response, "get_json", None)
    if callable(get_json):
        payload = await get_json()
        return dict(payload)
    body = getattr(response, "body", b"")
    return dict(json.loads(bytes(body).decode("utf-8")))


@pytest.mark.asyncio
async def test_real_sdk_plugin_contract_smoke():
    context = SmokeContext()
    plugin = ChatDynamicsPlugin(
        context=context,
        config={"enable": True, "takeover_all": True, "vibe_llm_enabled": False},
    )

    await plugin.initialize()
    assert {route.rsplit("/", 1)[-1] for route, *_ in context.routes} == {
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
    }
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
    assert not plugin._vibe_llm_tasks
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
