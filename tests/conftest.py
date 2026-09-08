"""Pytest configuration and environment fixtures for astrbot_plugin_chat_dynamics."""

from __future__ import annotations

import sys
import types
import logging
import importlib.util
import pytest
from pathlib import Path


@pytest.fixture
def offline_web_responses(monkeypatch):
    """Route-unit tests exercise payloads; real HTTP serialization lives in integration/."""
    from astrbot_plugin_chat_dynamics.core import web_api

    def response(data=None, *, status_code=200, headers=None):
        return {**(data or {}), "status_code": status_code, "headers": headers or {}}

    def error(message, *, status_code=400, data=None, headers=None):
        return response({"ok": False, "status": "error", "error": message, "message": message, "data": data},
                        status_code=status_code, headers=headers)

    monkeypatch.setattr(web_api, "json_response", response)
    monkeypatch.setattr(web_api, "error_response", error)

# Import the plugin as `astrbot_plugin_chat_dynamics` via its parent directory.
# Putting PLUGIN_DIR first would shadow v1.3.3 with the nested leftover copy.
TEST_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TEST_DIR.parent
WORKSPACE_ROOT = PLUGIN_DIR.parent

_sys_paths = [path for path in sys.path if path not in {str(PLUGIN_DIR), str(WORKSPACE_ROOT)}]
sys.path[:] = [str(WORKSPACE_ROOT), str(PLUGIN_DIR), *_sys_paths]


def _install_astrbot_test_double() -> None:
    """Install explicit SDK doubles for unit tests when AstrBot is unavailable."""
    spec = importlib.util.find_spec("astrbot")
    api_spec = importlib.util.find_spec("astrbot.api") if spec is not None else None
    # A bare namespace path named astrbot (without astrbot.api) is not a real SDK.
    if spec is not None and api_spec is not None:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")
    components = types.ModuleType("astrbot.api.message_components")

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def message(self, text):
            self.chain.append(str(text))
            return self

        def get_plain_text(self, *_args):
            return "".join(item if isinstance(item, str) else getattr(item, "text", str(item)) for item in self.chain)

    class Reply:
        def __init__(self, id):
            self.id = id

    class EventMessageType:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP_MESSAGE"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"

    class PermissionType:
        ADMIN = "ADMIN"
        MEMBER = "MEMBER"

    def decorator(*_args, **_kwargs):
        return lambda target: target

    filter_double = types.SimpleNamespace(
        EventMessageType=EventMessageType,
        PermissionType=PermissionType,
        event_message_type=decorator,
        command=decorator,
        permission_type=decorator,
        on_llm_request=decorator,
        on_llm_response=decorator,
        on_decorating_result=decorator,
        after_message_sent=decorator,
    )

    class Star:
        def __init__(self, context=None):
            self.context = context
            self._kv = {}

        async def put_kv_data(self, key, value):
            self._kv[key] = value

        async def get_kv_data(self, key, default=None):
            return self._kv.get(key, default)

        async def delete_kv_data(self, key):
            self._kv.pop(key, None)

    class Context:
        pass

    class Request:
        args = {}
        query = {}
        username = "pytest"

        async def json(self, default=None):
            return default or {}

    def register(*_args, **_kwargs):
        return lambda target: target

    def json_response(data=None, *, status_code=200, headers=None):
        payload = dict(data) if isinstance(data, dict) else {"data": data}
        if status_code != 200:
            payload["status_code"] = status_code
        return payload

    def error_response(message, *, status_code=400, data=None, headers=None):
        return json_response(
            {"status": "error", "message": message, "data": data, "ok": False, "error": message},
            status_code=status_code,
            headers=headers,
        )

    api.logger = logging.getLogger("astrbot-test")
    event.AstrMessageEvent = object
    event.EventMessageType = EventMessageType
    event.MessageChain = MessageChain
    event.filter = filter_double
    star.Context = Context
    star.Star = Star
    star.register = register
    web.json_response = json_response
    web.error_response = error_response
    web.request = Request()
    components.Reply = Reply

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.web": web,
        "astrbot.api.message_components": components,
    })


_install_astrbot_test_double()
