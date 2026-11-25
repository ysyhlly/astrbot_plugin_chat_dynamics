"""Pytest configuration and environment fixtures for astrbot_plugin_chat_dynamics."""

from __future__ import annotations

import sys
import types
import logging
import importlib.util
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_host_request_hooks(monkeypatch):
    """Unit doubles have no running host registry; real dispatch is tested in integration/."""
    try:
        from astrbot.core.pipeline import context_utils
    except ImportError:
        return

    async def no_registered_hooks(*_args, **_kwargs):
        return False

    monkeypatch.setattr(context_utils, "call_event_hook", no_registered_hooks)


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
        from dataclasses import dataclass
        try:
            from astrbot.core.provider import entities as _ent
            if not hasattr(_ent, "DecisionBooleanAnswer"):
                @dataclass
                class DecisionBooleanQuestion:
                    instructions: str = ""

                @dataclass
                class DecisionChoiceQuestion:
                    criteria: dict
                    instructions: str = ""

                @dataclass
                class DecisionScoreQuestion:
                    criteria: list
                    instructions: str = ""

                @dataclass
                class DecisionBooleanAnswer:
                    probability: float

                @dataclass
                class DecisionChoiceAnswer:
                    choice: str
                    probability: float
                    probabilities: dict
                    confidence: float

                @dataclass
                class DecisionScoreAnswer:
                    score: float
                    legend: dict
                    probabilities: dict
                    confidence: float

                for _cls in (
                    DecisionBooleanQuestion,
                    DecisionChoiceQuestion,
                    DecisionScoreQuestion,
                    DecisionBooleanAnswer,
                    DecisionChoiceAnswer,
                    DecisionScoreAnswer,
                ):
                    setattr(_ent, _cls.__name__, _cls)
            if hasattr(_ent, "ProviderType") and not hasattr(_ent.ProviderType, "DECISION"):
                setattr(_ent.ProviderType, "DECISION", "decision")
        except Exception:
            pass
        try:
            from astrbot.core.provider import provider as _prov
            if not hasattr(_prov, "DecisionProvider"):
                _base = getattr(_prov, "Provider", object)

                class DecisionProvider(_base):
                    def __init__(self, provider_config=None, provider_settings=None):
                        self.provider_config = provider_config or {}
                        self.provider_settings = provider_settings or {}

                _prov.DecisionProvider = DecisionProvider
        except Exception:
            pass
        return

    from dataclasses import dataclass

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")
    components = types.ModuleType("astrbot.api.message_components")
    core_mod = types.ModuleType("astrbot.core")
    core_mod.__path__ = []
    provider_pkg = types.ModuleType("astrbot.core.provider")
    provider_pkg.__path__ = []
    provider_entities = types.ModuleType("astrbot.core.provider.entities")
    provider_mod = types.ModuleType("astrbot.core.provider.provider")
    provider_register = types.ModuleType("astrbot.core.provider.register")
    config_pkg = types.ModuleType("astrbot.core.config")
    config_pkg.__path__ = []
    config_default = types.ModuleType("astrbot.core.config.default")
    utils_pkg = types.ModuleType("astrbot.core.utils")
    utils_pkg.__path__ = []
    llm_metadata_mod = types.ModuleType("astrbot.core.utils.llm_metadata")
    dashboard_pkg = types.ModuleType("astrbot.dashboard")
    dashboard_pkg.__path__ = []
    dashboard_services_pkg = types.ModuleType("astrbot.dashboard.services")
    dashboard_services_pkg.__path__ = []
    dashboard_config_service = types.ModuleType("astrbot.dashboard.services.config_service")

    @dataclass
    class DecisionBooleanQuestion:
        instructions: str = ""

    @dataclass
    class DecisionChoiceQuestion:
        criteria: dict
        instructions: str = ""

    @dataclass
    class DecisionScoreQuestion:
        criteria: list
        instructions: str = ""

    @dataclass
    class DecisionBooleanAnswer:
        probability: float

    @dataclass
    class DecisionChoiceAnswer:
        choice: str
        probability: float
        probabilities: dict
        confidence: float

    @dataclass
    class DecisionScoreAnswer:
        score: float
        legend: dict
        probabilities: dict
        confidence: float

    @dataclass
    class TokenUsage:
        input_other: int = 0
        input_cached: int = 0
        output: int = 0

    @dataclass
    class LLMResponse:
        role: str = "assistant"
        completion_text: str = ""
        usage: TokenUsage | None = None

    class DecisionError(RuntimeError):
        pass

    class ProviderType:
        CHAT_COMPLETION = "chat_completion"
        EMBEDDING = "embedding"
        RERANK = "rerank"
        DECISION = "decision"

    class Provider:
        def __init__(self, provider_config=None, provider_settings=None):
            self.provider_config = provider_config or {}
            self.provider_settings = provider_settings or {}

    class DecisionProvider(Provider):
        pass

    class ProviderConfigService:
        async def list_provider_source_models(self, source_id):
            return {"models": [], "provider_source_id": source_id, "model_metadata": {}}

    for _cls in (
        DecisionBooleanQuestion,
        DecisionChoiceQuestion,
        DecisionScoreQuestion,
        DecisionBooleanAnswer,
        DecisionChoiceAnswer,
        DecisionScoreAnswer,
        TokenUsage,
        LLMResponse,
        DecisionError,
        ProviderType,
    ):
        setattr(provider_entities, _cls.__name__, _cls)
    provider_mod.Provider = Provider
    provider_mod.DecisionProvider = DecisionProvider
    provider_register.provider_cls_map = {}
    provider_register.provider_registry = []
    provider_register.llm_tools = []
    provider_register.register_provider_adapter = lambda *a, **kw: (lambda cls: cls)
    config_default.CONFIG_METADATA_2 = {"provider_group": {"metadata": {"provider": {"config_template": {}}}}}
    llm_metadata_mod.LLM_METADATAS = {}
    dashboard_config_service.ProviderConfigService = ProviderConfigService

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def message(self, text):
            self.chain.append(str(text))
            return self

        def get_plain_text(self, *_args):
            # References and media components do not contribute plain text.
            return "".join(item if isinstance(item, str) else getattr(item, "text", "") for item in self.chain)

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
        on_astrbot_loaded=decorator,
        on_plugin_loaded=decorator,
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
        "astrbot.core": core_mod,
        "astrbot.core.provider": provider_pkg,
        "astrbot.core.provider.entities": provider_entities,
        "astrbot.core.provider.provider": provider_mod,
        "astrbot.core.provider.register": provider_register,
        "astrbot.core.config": config_pkg,
        "astrbot.core.config.default": config_default,
        "astrbot.core.utils": utils_pkg,
        "astrbot.core.utils.llm_metadata": llm_metadata_mod,
        "astrbot.dashboard": dashboard_pkg,
        "astrbot.dashboard.services": dashboard_services_pkg,
        "astrbot.dashboard.services.config_service": dashboard_config_service,
    })


_install_astrbot_test_double()
