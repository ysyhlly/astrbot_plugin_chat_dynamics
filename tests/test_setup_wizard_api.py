"""Tests for Setup Wizard backend APIs: group discovery, provider probes, page_nav, and preferences."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.provider.entities import DecisionBooleanAnswer
from astrbot.core.provider.provider import DecisionProvider
from astrbot_plugin_chat_dynamics import series_workspace
from astrbot_plugin_chat_dynamics.core import page_layout, setup_wizard, web_api
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.web_api import PLUGIN_NAME
from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from .test_dashboard import MockContext


class _MinimalContext:
    """Clean AstrBot Context double without pre-stubbed LLM methods."""

    def __init__(self):
        self.web_routes = []
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods=None, desc=""):
        row = (route, handler, methods, desc)
        self.web_routes.append(row)
        self.registered_web_apis.append(row)

    def get_config(self):
        return {}


def _make_plugin(config=None, context=None):
    ctx = context if context is not None else _MinimalContext()
    return ChatDynamicsPlugin(context=ctx, config=dict(config or {"enable": True}))


class _OneBotPlatform:
    def __init__(self, platform_id: str, platform_type: str, handler):
        self._meta = SimpleNamespace(id=platform_id, name=platform_type)
        self.bot = SimpleNamespace(call_action=handler)

    def meta(self):
        return self._meta


class _PlainPlatform:
    def __init__(self, platform_id: str, platform_type: str):
        self._meta = SimpleNamespace(id=platform_id, name=platform_type)

    def meta(self):
        return self._meta


class _FakeNativeDecisionProvider(DecisionProvider):
    def __init__(self, pid: str = "dec-native", model: str = "jev-test", ask_fn=None):
        super().__init__({"id": pid, "model": model}, {})
        self._pid = pid
        self._model = model
        self._ask_fn = ask_fn
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id=self._pid, model=self._model, type="decision")

    def get_model(self):
        return self._model

    async def ask(self, questions, state=None, *, request_max_retries=0):
        self.calls += 1
        if self._ask_fn is not None:
            return await self._ask_fn(questions, state, request_max_retries=request_max_retries)
        return {key: DecisionBooleanAnswer(probability=0.8) for key in questions}


class _FakeLegacySystemOneProvider:
    is_systemone_provider = True

    def __init__(self, pid: str = "sysone-legacy", model: str = "jev-legacy", eval_fn=None):
        self.provider_config = {"id": pid, "model": model}
        self._eval_fn = eval_fn
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id=self.provider_config["id"], model=self.provider_config["model"], type="systemone")

    def get_model(self):
        return self.provider_config["model"]

    async def systemone_evaluate(self, *, state, questions, timeout=None):
        self.calls += 1
        if self._eval_fn is not None:
            return await self._eval_fn(state=state, questions=questions, timeout=timeout)
        return {"ok": {"type": "noul", "noul": 0.7}}


class _FakeChatProvider:
    def __init__(self, pid: str = "chat-main", model: str = "gpt-test", text_fn=None):
        self.provider_config = {"id": pid, "model": model}
        self.model_name = model
        self._text_fn = text_fn
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id=self.provider_config["id"], model=self.model_name, type="openai")

    def get_model(self):
        return self.model_name

    async def text_chat(self, **kwargs):
        self.calls += 1
        if self._text_fn is not None:
            return await self._text_fn(**kwargs)
        return SimpleNamespace(role="assistant", completion_text="OK")


class _FakeRerankProvider:
    def __init__(self, pid: str = "rerank-1", model: str = "bge-rerank", rerank_fn=None):
        self.provider_config = {"id": pid, "model": model}
        self.model_name = model
        self._rerank_fn = rerank_fn
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id=self.provider_config["id"], model=self.model_name, type="rerank")

    async def rerank(self, **kwargs):
        self.calls += 1
        if self._rerank_fn is not None:
            return await self._rerank_fn(**kwargs)
        return [SimpleNamespace(index=0, relevance_score=0.9)]


@pytest.mark.asyncio
async def test_setup_routes_registered_and_page_nav_supports_setup(monkeypatch, offline_web_responses):
    assert "setup" in page_layout.SECTIONS
    assert (
        page_layout.section_content_path(
            "/api/v1/plugin-pages/astrbot_plugin_chat_dynamics/app/index.html?asset_token=abc",
            "setup",
        )
        == "/api/v1/plugin-pages/astrbot_plugin_chat_dynamics/app/setup/index.html?asset_token=abc"
    )

    ctx = MockContext()
    plugin = _make_plugin({"enable": True}, context=ctx)
    routes = {(row[0], tuple(row[2])) for row in ctx.web_routes}
    assert (f"/{PLUGIN_NAME}/setup/groups", ("GET",)) in routes
    assert (f"/{PLUGIN_NAME}/setup/probe", ("POST",)) in routes

    async def fake_entry(*, plugin_name, view_name, username, locale):
        assert plugin_name == PLUGIN_NAME
        assert view_name == "app"
        assert username == "admin"
        return {"content_path": "/api/v1/plugin-pages/astrbot_plugin_chat_dynamics/app/index.html?asset_token=tok"}

    plugin_req = SimpleNamespace(
        username="admin",
        headers={"accept-language": "zh-CN"},
        _request=SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    services=SimpleNamespace(
                        plugin_pages=SimpleNamespace(get_plugin_page_entry_config=fake_entry)
                    )
                )
            )
        ),
    )
    monkeypatch.setattr(web_api, "request", SimpleNamespace(username="admin", _get_current=lambda: plugin_req))
    monkeypatch.setattr(web_api, "query_value", lambda name: "setup" if name == "page" else "")

    res = await plugin._web.page_nav()
    assert res["status_code"] == 200
    assert res["data"]["page"] == "setup"
    assert res["data"]["content_path"].endswith("/app/setup/index.html?asset_token=tok")
    await plugin.terminate()


@pytest.mark.asyncio
async def test_discover_setup_groups_merges_onebot_list_dict_sessions_and_config(monkeypatch, offline_web_responses):
    async def list_handler(action):
        assert action == "get_group_list"
        return [
            {"group_id": 1001, "group_name": "测试群A", "member_count": 42},
            {"group_id": "1002", "group_name": "闲聊群B", "member_count": 15},
        ]

    async def dict_handler(action):
        assert action == "get_group_list"
        return {"status": "ok", "retcode": 0, "data": [{"group_id": "1003", "group_name": "开发群C", "member_count": 8}]}

    ctx = MockContext()
    ctx.platform_manager = SimpleNamespace(
        get_insts=lambda: [
            _OneBotPlatform("qq-1", "aiocqhttp", list_handler),
            _OneBotPlatform("qq-2", "aiocqhttp", dict_handler),
            _PlainPlatform("tg-1", "telegram"),
        ]
    )
    plugin = _make_plugin(
        {
            "enable": True,
            "takeover_groups": ["1002", "9001"],
            "exclude_groups": ["1003", "9002"],
        },
        context=ctx,
    )
    # Active group session overlapping with OneBot group 1001 + standalone session 2001 + private sessions to skip.
    plugin._sessions["qq-1:GroupMessage:1001"] = SessionRuntime(
        session_key="qq-1:GroupMessage:1001",
        group_id="1001",
        umo="qq-1:GroupMessage:1001",
        scope_kind="group",
        last_activity=1732000100.0,
    )
    plugin._sessions["qq-1:GroupMessage:2001"] = SessionRuntime(
        session_key="qq-1:GroupMessage:2001",
        group_id="2001",
        umo="qq-1:GroupMessage:2001",
        scope_kind="group",
        last_activity=1732000200.0,
    )
    plugin._sessions["qq-1:FriendMessage:777"] = SessionRuntime(
        session_key="qq-1:FriendMessage:777",
        group_id="777",
        umo="qq-1:FriendMessage:777",
        scope_kind="private",
        last_activity=1732000500.0,
    )
    plugin._sessions["qq-1:PrivateMessage:888"] = SessionRuntime(
        session_key="qq-1:PrivateMessage:888",
        group_id="888",
        umo="qq-1:PrivateMessage:888",
        scope_kind="group",
        last_activity=1732000600.0,
    )

    monkeypatch.setattr(web_api, "request", SimpleNamespace(username="admin"))
    res = await plugin._web.setup_groups()
    assert res["status_code"] == 200
    data = res["data"]
    assert data["partial_failure"] is False
    assert data["discovered_count"] == 3
    assert data["session_count"] == 2

    plat_by_id = {p["platform_id"]: p for p in data["platforms"]}
    assert plat_by_id["qq-1"] == {
        "platform_id": "qq-1",
        "platform_type": "aiocqhttp",
        "status": "ok",
        "group_count": 2,
        "error": "",
    }
    assert plat_by_id["qq-2"]["status"] == "ok"
    assert plat_by_id["qq-2"]["group_count"] == 1
    assert plat_by_id["tg-1"]["status"] == "unsupported"
    assert plat_by_id["tg-1"]["error"] == ""

    by_id = {g["group_id"]: g for g in data["groups"]}
    assert "777" not in by_id
    assert "888" not in by_id
    assert set(by_id) == {"1001", "1002", "1003", "2001", "9001", "9002"}

    assert by_id["1001"]["sources"] == ["platform", "session"]
    assert by_id["1001"]["group_name"] == "测试群A"
    assert by_id["1001"]["member_count"] == 42
    assert by_id["1001"]["last_activity"] == 1732000100.0

    assert by_id["1002"]["sources"] == ["platform", "config"]
    assert by_id["1002"]["in_takeover"] is True
    assert by_id["1002"]["in_exclude"] is False

    assert by_id["1003"]["sources"] == ["platform", "config"]
    assert by_id["1003"]["in_exclude"] is True

    assert by_id["9001"]["sources"] == ["config"]
    assert by_id["9001"]["in_takeover"] is True
    assert by_id["9002"]["sources"] == ["config"]
    assert by_id["9002"]["in_exclude"] is True

    # Verify ordering: takeover groups first (1002, 9001), then by last_activity desc (2001, 1001), then remaining.
    ordered_ids = [g["group_id"] for g in data["groups"]]
    assert set(ordered_ids[:2]) == {"1002", "9001"}
    assert ordered_ids[2:4] == ["2001", "1001"]
    await plugin.terminate()


@pytest.mark.asyncio
async def test_discover_setup_groups_handles_timeout_errors_and_500_cap():
    async def slow_handler(_action):
        await asyncio.sleep(5.0)
        return []

    async def unavailable_handler(_action):
        raise NotImplementedError("secret internal path /etc/secret sk-1234567890")

    async def broken_handler(_action):
        raise RuntimeError("Bearer secret-token-xyz traceback")

    async def large_handler(_action):
        return [{"group_id": str(i), "group_name": f"Group {i}"} for i in range(600)]

    ctx = MockContext()
    ctx.platform_manager = SimpleNamespace(
        get_insts=lambda: [
            _OneBotPlatform("slow", "aiocqhttp", slow_handler),
            _OneBotPlatform("unavail", "aiocqhttp", unavailable_handler),
            _OneBotPlatform("broken", "aiocqhttp", broken_handler),
            _OneBotPlatform("big", "aiocqhttp", large_handler),
        ]
    )
    plugin = _make_plugin({"enable": True}, context=ctx)
    data = await setup_wizard.discover_setup_groups(plugin, per_platform_timeout=0.05, total_timeout=0.2)

    assert data["partial_failure"] is True
    assert len(data["groups"]) == 500
    assert data["discovered_count"] == 600
    plat_by_id = {p["platform_id"]: p for p in data["platforms"]}
    assert plat_by_id["slow"]["status"] == "timeout"
    assert plat_by_id["slow"]["error"] == "timeout"
    assert plat_by_id["unavail"]["status"] == "error"
    assert plat_by_id["unavail"]["error"] == "action_unavailable"
    assert plat_by_id["broken"]["status"] == "error"
    assert plat_by_id["broken"]["error"] == "query_failed"
    assert "secret" not in str(data)
    assert "sk-" not in str(data)
    assert "Bearer" not in str(data)
    await plugin.terminate()


@pytest.mark.asyncio
async def test_probe_setup_providers_jev_and_default_reply(monkeypatch, offline_web_responses):
    jev_prov = _FakeNativeDecisionProvider(pid="dec-1", model="jev-v2")
    default_chat = _FakeChatProvider(pid="chat-default", model="gpt-4o-mini")
    providers = {"dec-1": jev_prov, "chat-default": default_chat}

    ctx = _MinimalContext()
    ctx.get_provider_by_id = lambda pid: providers.get(pid)
    ctx.get_current_chat_provider_id = lambda _umo="": "chat-default"
    llm_calls = []

    async def fake_llm_generate(*, chat_provider_id, prompt, system_prompt):
        llm_calls.append((chat_provider_id, prompt, system_prompt))
        return SimpleNamespace(role="assistant", completion_text="OK")

    ctx.llm_generate = fake_llm_generate

    plugin = _make_plugin({"enable": True}, context=ctx)
    monkeypatch.setattr(web_api, "request", SimpleNamespace(username="admin"))

    async def body():
        return {
            "participation_backend": "jev",
            "decision_provider": "dec-1",
            "reply_provider": "",
            "probe_discussion": False,
        }

    monkeypatch.setattr(web_api, "_json_body", body)
    res = await plugin._web.setup_probe()
    assert res["status_code"] == 200
    data = res["data"]
    assert data["ok"] is True
    assert data["results"]["decision"]["status"] == "ok"
    assert data["results"]["decision"]["code"] == "ok"
    assert data["results"]["decision"]["backend"] == "jev"
    assert data["results"]["decision"]["provider_id"] == "dec-1"
    assert data["results"]["decision"]["model"] == "jev-v2"
    assert isinstance(data["results"]["decision"]["latency_ms"], int)
    assert jev_prov.calls == 1

    assert data["results"]["reply"]["status"] == "ok"
    assert data["results"]["reply"]["using_default"] is True
    assert data["results"]["reply"]["provider_id"] == "chat-default"
    assert data["results"]["reply"]["model"] == "gpt-4o-mini"
    assert len(llm_calls) == 1
    assert data["results"]["discussion"] is None
    await plugin.terminate()


@pytest.mark.asyncio
async def test_probe_setup_providers_legacy_systemone_and_empty_jev_skipped():
    legacy = _FakeLegacySystemOneProvider(pid="sysone-1", model="jev-old")
    chat = _FakeChatProvider(pid="chat-1", model="claude-test")
    providers = {"sysone-1": legacy, "chat-1": chat}

    ctx = _MinimalContext()
    ctx.get_provider_by_id = lambda pid: providers.get(pid)
    plugin = _make_plugin({"enable": True}, context=ctx)

    # Legacy systemone_evaluate fallback when .ask is absent, and text_chat fallback when llm_generate is absent.
    res = await setup_wizard.probe_setup_providers(
        plugin,
        {
            "participation_backend": "jev",
            "decision_provider": "sysone-1",
            "reply_provider": "chat-1",
        },
    )
    assert res["ok"] is True
    assert res["results"]["decision"]["status"] == "ok"
    assert legacy.calls == 1
    assert res["results"]["reply"]["status"] == "ok"
    assert chat.calls == 1

    # Empty decision_provider in Jev mode -> skipped (not error).
    skipped = await setup_wizard.probe_setup_providers(
        plugin,
        {
            "participation_backend": "jev",
            "decision_provider": "",
            "reply_provider": "chat-1",
        },
    )
    assert skipped["ok"] is True
    assert skipped["results"]["decision"]["status"] == "skipped"
    assert skipped["results"]["decision"]["code"] == "skipped"
    await plugin.terminate()


@pytest.mark.asyncio
async def test_probe_setup_providers_llm_deduplicates_shared_chat_probe_and_discussion():
    chat = _FakeChatProvider(pid="chat-1", model="deepseek-chat")
    rerank = _FakeRerankProvider(pid="rerank-1", model="bge-reranker")
    providers = {"chat-1": chat, "rerank-1": rerank}

    ctx = _MinimalContext()
    ctx.get_provider_by_id = lambda pid: providers.get(pid)
    plugin = _make_plugin({"enable": True}, context=ctx)

    # LLM decision falls back to reply_provider "chat-1", plus discussion_llm falls back to "chat-1":
    # all three share a single text_chat call!
    res = await setup_wizard.probe_setup_providers(
        plugin,
        {
            "participation_backend": "llm",
            "decision_llm_provider": "",
            "reply_provider": "chat-1",
            "discussion_judgment_mode": "llm",
            "discussion_llm_provider": "",
            "probe_discussion": True,
        },
    )
    assert res["ok"] is True
    assert chat.calls == 1
    assert res["results"]["decision"]["status"] == "ok"
    assert res["results"]["decision"]["backend"] == "llm"
    assert res["results"]["decision"]["provider_id"] == "chat-1"
    assert res["results"]["reply"]["status"] == "ok"
    assert res["results"]["discussion"]["status"] == "ok"
    assert res["results"]["discussion"]["mode"] == "llm"

    # Rerank discussion probe
    res_rerank = await setup_wizard.probe_setup_providers(
        plugin,
        {
            "participation_backend": "llm",
            "reply_provider": "chat-1",
            "discussion_judgment_mode": "rerank",
            "discussion_reranker_provider": "rerank-1",
            "probe_discussion": True,
        },
    )
    assert res_rerank["ok"] is True
    assert rerank.calls == 1
    assert res_rerank["results"]["discussion"]["status"] == "ok"
    assert res_rerank["results"]["discussion"]["mode"] == "rerank"
    await plugin.terminate()


@pytest.mark.asyncio
async def test_probe_setup_providers_error_cases_timeout_and_secret_scrubbing():
    async def slow_chat(**_kwargs):
        await asyncio.sleep(5.0)
        return SimpleNamespace(role="assistant", completion_text="OK")

    async def leaky_ask(_questions, _state, *, request_max_retries=0):
        raise RuntimeError(
            "Upstream 401 failed with Bearer eyJhbGciOiJIUzI1NiJ9.secret and sk-proj-supersecretkey123456 "
            "api_key=my_secret_value\nTraceback (most recent call last):\n  File '/secret.py', line 1"
        )

    jev_leaky = _FakeNativeDecisionProvider(pid="dec-leaky", ask_fn=leaky_ask)
    chat_slow = _FakeChatProvider(pid="chat-slow", text_fn=slow_chat)
    providers = {"dec-leaky": jev_leaky, "chat-slow": chat_slow}

    ctx = _MinimalContext()
    ctx.get_provider_by_id = lambda pid: providers.get(pid)
    plugin = _make_plugin({"enable": True}, context=ctx)

    # Leaky error + timeout
    res = await setup_wizard.probe_setup_providers(
        plugin,
        {
            "participation_backend": "jev",
            "decision_provider": "dec-leaky",
            "reply_provider": "chat-slow",
        },
        timeout=0.05,
    )
    assert res["ok"] is False
    dec = res["results"]["decision"]
    assert dec["status"] == "error"
    assert dec["code"] == "provider_error"
    assert "eyJhbGci" not in dec["message"]
    assert "sk-proj-supersecretkey" not in dec["message"]
    assert "my_secret_value" not in dec["message"]
    assert "Traceback" not in dec["message"]
    assert len(dec["message"]) <= 160

    rep = res["results"]["reply"]
    assert rep["status"] == "error"
    assert rep["code"] == "timeout"

    # Missing provider & no default chat provider & wrong provider type (decision provider as reply)
    res_missing = await setup_wizard.probe_setup_providers(
        plugin,
        {
            "participation_backend": "jev",
            "decision_provider": "does-not-exist",
            "reply_provider": "",
        },
    )
    assert res_missing["ok"] is False
    assert res_missing["results"]["decision"]["code"] == "invalid_provider"
    assert res_missing["results"]["reply"]["code"] == "no_default_provider"
    assert res_missing["results"]["reply"]["using_default"] is True

    res_wrong_type = await setup_wizard.probe_setup_providers(
        plugin,
        {
            "participation_backend": "llm",
            "decision_llm_provider": "dec-leaky",
            "reply_provider": "dec-leaky",
        },
    )
    assert res_wrong_type["ok"] is False
    assert res_wrong_type["results"]["decision"]["code"] == "invalid_provider"
    assert res_wrong_type["results"]["reply"]["code"] == "invalid_provider"
    await plugin.terminate()


@pytest.mark.asyncio
async def test_setup_probe_validation_rate_limit_and_shutdown(monkeypatch, offline_web_responses):
    ctx = _MinimalContext()
    chat = _FakeChatProvider(pid="chat-1")
    ctx.get_provider_by_id = lambda pid: chat if pid == "chat-1" else None
    plugin = _make_plugin({"enable": True}, context=ctx)
    monkeypatch.setattr(web_api, "request", SimpleNamespace(username="admin"))

    bad_payloads = [
        {"unknown_key": "x"},
        {"participation_backend": "invalid"},
        {"discussion_judgment_mode": "invalid"},
        {"decision_provider": 123},
        {"reply_provider": "x" * 129},
        {"probe_discussion": "yes"},
    ]
    for payload in bad_payloads:
        plugin._web._rate_buckets.clear()
        monkeypatch.setattr(web_api, "_json_body", AsyncMock(return_value=payload))
        res = await plugin._web.setup_probe()
        assert res["status_code"] == 400, f"Expected 400 for {payload}"

    # Rate limit: 6/min for setup_probe
    plugin._web._rate_buckets.clear()
    monkeypatch.setattr(
        web_api,
        "_json_body",
        AsyncMock(return_value={"participation_backend": "llm", "reply_provider": "chat-1"}),
    )
    for _ in range(6):
        ok = await plugin._web.setup_probe()
        assert ok["status_code"] == 200
    limited = await plugin._web.setup_probe()
    assert limited["status_code"] == 429
    assert limited["headers"]["Retry-After"] == "60"

    # Rate limit: 20/min for setup_groups
    plugin._web._rate_buckets.clear()
    for _ in range(20):
        ok = await plugin._web.setup_groups()
        assert ok["status_code"] == 200
    limited_groups = await plugin._web.setup_groups()
    assert limited_groups["status_code"] == 429
    assert limited_groups["headers"]["Retry-After"] == "60"

    # Shutdown -> 503
    plugin._web._rate_buckets.clear()
    plugin._shutting_down = True
    assert (await plugin._web.setup_groups())["status_code"] == 503
    assert (await plugin._web.setup_probe())["status_code"] == 503
    await plugin.terminate()


@pytest.mark.asyncio
async def test_setup_probe_default_provider_fallbacks_and_error_role_rejected():
    # Fallback 1: get_using_provider()
    chat1 = _FakeChatProvider(pid="using-chat", model="using-model")
    ctx1 = _MinimalContext()
    ctx1.get_using_provider = lambda _umo="": chat1
    plugin1 = _make_plugin({"enable": True}, context=ctx1)
    res1 = await setup_wizard.probe_setup_providers(plugin1, {"participation_backend": "llm", "reply_provider": ""})
    assert res1["ok"] is True
    assert res1["results"]["reply"]["provider_id"] == "using-chat"
    assert res1["results"]["reply"]["using_default"] is True
    assert res1["results"]["decision"]["provider_id"] == "using-chat"
    assert chat1.calls == 1
    await plugin1.terminate()

    # Fallback 2: provider_manager.curr_provider_inst returning role="err" -> rejected as provider_error
    async def err_text(**_kwargs):
        return SimpleNamespace(role="err", completion_text="All chat models failed: Bearer secret-token")

    chat2 = _FakeChatProvider(pid="curr-chat", model="curr-model", text_fn=err_text)
    ctx2 = _MinimalContext()
    ctx2.provider_manager = SimpleNamespace(curr_provider_inst=chat2)
    plugin2 = _make_plugin({"enable": True}, context=ctx2)
    res2 = await setup_wizard.probe_setup_providers(plugin2, {"participation_backend": "jev", "reply_provider": ""})
    assert res2["ok"] is False
    assert res2["results"]["reply"]["status"] == "error"
    assert res2["results"]["reply"]["code"] == "provider_error"
    assert "secret-token" not in res2["results"]["reply"]["message"]
    await plugin2.terminate()


@pytest.mark.asyncio
async def test_setup_routes_registered_on_series_workspace(monkeypatch, offline_web_responses):
    ctx = MockContext()
    ctx.registered_web_apis = []
    original_register = ctx.register_web_api

    def track_register(route, handler, methods=None, desc=""):
        original_register(route, handler, methods=methods, desc=desc)
        ctx.registered_web_apis.append((route, handler, methods, desc))

    ctx.register_web_api = track_register
    plugin = _make_plugin({"enable": True}, context=ctx)
    monkeypatch.setattr(web_api, "request", SimpleNamespace(username="admin"))

    panel = SimpleNamespace(owner=plugin, identity=PLUGIN_NAME)
    workspace = series_workspace.PanelWorkspace(panel)
    groups_res = await workspace.run("GET", "setup/groups")
    assert groups_res["status_code"] == 200
    assert "groups" in groups_res["data"]
    await plugin.terminate()


def test_setup_wizard_runtime_files_do_not_import_stdlib_logging():
    repo_root = Path(__file__).resolve().parents[1]
    targets = [
        repo_root / "core" / "setup_wizard.py",
        repo_root / "core" / "page_layout.py",
        repo_root / "core" / "web_api.py",
        repo_root / "core" / "plugin_web_mixin.py",
    ]
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "logging" for alias in node.names), f"stdlib logging imported in {path}"
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "logging", f"stdlib logging imported in {path}"
