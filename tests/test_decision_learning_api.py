"""Management bridge rejects malformed operations and never leaks backend errors."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_chat_dynamics.core import web_api as web
from astrbot_plugin_chat_dynamics.core.decision_learning_api import DecisionLearningWebAPI


@pytest.fixture
def api(monkeypatch, offline_web_responses):
    monkeypatch.setattr(web, "request", NS(username="admin"))
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value={}))
    runtime = NS(snapshot=Mock(return_value={"mode": "off"}), management=AsyncMock(return_value={"ok": True}))
    return DecisionLearningWebAPI(NS(decision_learning=runtime, _shutting_down=False))


@pytest.mark.asyncio
async def test_snapshot_supports_runtime_coroutine(api):
    api.plugin.decision_learning.snapshot = AsyncMock(return_value={"mode": "shadow"})
    assert (await api.stats())["data"] == {"mode": "shadow"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action,body", [
    ("samples/delete", {}), ("samples/delete", {"session_key": []}),
    ("jobs/cancel", {}), ("jobs/create", {"options": []}),
    ("jobs/create", {"model_id": "m", "seed": True}),
    ("jobs/create", {"model_id": "m", "seed": 0}),
    ("jobs/create", {"model_id": "m", "seed": 2147483649}),
    ("samples/export", {"limit": True}), ("samples/export", {"limit": 1001}),
    ("models/promote", {"model_id": "m", "path": "/etc"}),
])
async def test_bad_input_never_reaches_runtime(api, monkeypatch, action, body):
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value=body))
    assert (await api.dispatch(action))["status_code"] == 400
    api.plugin.decision_learning.management.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_delete_passes_exact_session(api, monkeypatch):
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value={"session_key": "platform:group:42"}))
    assert (await api.dispatch("samples/delete"))["status_code"] == 200
    api.plugin.decision_learning.management.assert_awaited_once_with("samples/delete", {"session_key": "platform:group:42"})


@pytest.mark.asyncio
async def test_shutdown_and_missing_runtime_fail_closed(api):
    api.plugin._shutting_down = True
    assert (await api.stats())["status_code"] == 503
    api.plugin._shutting_down = False
    api.plugin.decision_learning = None
    assert (await api.dispatch("jobs/create"))["status_code"] == 503


@pytest.mark.asyncio
async def test_backend_exception_is_sanitized(api, monkeypatch):
    monkeypatch.setattr(web, "request_json", AsyncMock(return_value={"dataset": "export.jsonl", "model_id": "candidate"}))
    api.plugin.decision_learning.management.side_effect = RuntimeError("SECRET TOKEN")
    result = await api.dispatch("jobs/create")
    assert result["status_code"] == 503
    assert "SECRET" not in str(result)


def test_registration_uses_host_authenticated_dispatcher_and_is_idempotent(api):
    register = Mock()
    api.plugin.context = NS(register_web_api=register)
    api.register()
    api.register()
    assert api.registered
    assert register.call_count == 12
    assert any(call.args[0].endswith("/models/compare_jev") for call in register.call_args_list)
    for call in register.call_args_list:
        assert call.args[0].startswith("/astrbot_plugin_chat_dynamics/learning/")
        assert call.args[2] in (["GET"], ["POST"])


@pytest.mark.asyncio
async def test_unknown_action_and_rate_limit(api, monkeypatch):
    assert (await api.dispatch("execute"))["status_code"] == 404
    monkeypatch.setattr(api, "_rate_limit", lambda *args: {"status_code": 429})
    assert (await api.dispatch("jobs/create"))["status_code"] == 429
    api.plugin.decision_learning.management.assert_not_awaited()
