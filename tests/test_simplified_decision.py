"""Native decision-only runtime, retired handler cleanup and read-only history."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core import web_api as web
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.decision_status import DecisionStatusWebAPI, history_page
from astrbot_plugin_chat_dynamics.core.native_decision import NativeDecisionUnavailable


def test_retired_config_cannot_reactivate_workers_or_override_thresholds():
    cfg, _ = parse_runtime_config({
        "decision_backend": "kev", "decision_mode": "legacy",
        "decision_learning_mode": "active", "learning_policy_mode": "active",
        "vibe_llm_enabled": True, "wts_topic_weight": .49,
        "strong_addressivity_threshold": .75,
    })
    assert cfg.decision_backend == "jev" and cfg.decision_mode == "persona_model"
    assert not cfg.vibe_llm_enabled and cfg.wts_topic_weight == .12
    assert cfg.strong_addressivity_threshold == .75
    assert not hasattr(cfg, "learning_policy_mode")
    assert not hasattr(cfg, "decision_learning_mode")


@pytest.mark.asyncio
async def test_missing_native_router_never_calls_legacy_endpoint():
    client = NativeDecisionUnavailable()
    client.configure(base_url="https://example.invalid", api_key="SECRET", enabled=True)
    assert await client.evaluate(state={}, questions={}) is None
    assert not client.snapshot()["configured"]
    assert "SECRET" not in str(client.snapshot())


def test_reload_removes_all_old_learning_handlers_and_registers_two_routes():
    old = [(f"/{web.PLUGIN_NAME}/learning/{route}", None, ["POST"], "old")
           for route in ("jobs/create", "models/promote", "samples/delete", "stats")]
    kept = ("/other_plugin/learning/stats", None, ["GET"], "keep")
    context = NS(registered_web_apis=old + [kept], register_web_api=Mock())
    api = DecisionStatusWebAPI(NS(context=context))
    api.register()
    api.register()
    assert context.registered_web_apis == [kept]
    assert context.register_web_api.call_count == 2
    assert {call.args[0] for call in context.register_web_api.call_args_list} == {
        f"/{web.PLUGIN_NAME}/decision/status", f"/{web.PLUGIN_NAME}/decision/history"}


def test_history_is_read_only_paginated_and_hidden_by_default(tmp_path):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE samples (session TEXT, payload TEXT)")
        db.executemany("INSERT INTO samples VALUES (?,?)", [("anon_test", json.dumps({"id": str(i), "state": "fictional private text"})) for i in range(3)])
    before = hashlib.sha256(path.read_bytes()).digest()
    first = history_page(path, limit=2)
    assert len(first["records"]) == 2 and first["next_cursor"] == "2"
    assert "fictional private text" not in str(first)
    final = history_page(path, cursor=2, limit=2, show_content=True)
    assert final["records"][0]["id"] == "2" and final["next_cursor"] is None
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_history_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    assert history_page(path)["records"] == []
    assert not path.exists()


@pytest.mark.parametrize("cursor,limit", [(-1, 5), (True, 5), (2**63, 5), (0, True), (0, 1001)])
def test_history_rejects_invalid_pages(tmp_path, cursor, limit):
    with pytest.raises(ValueError):
        history_page(tmp_path / "missing.sqlite3", cursor=cursor, limit=limit)


@pytest.mark.asyncio
async def test_status_uses_only_native_counters_and_never_reads_history(monkeypatch, offline_web_responses):
    monkeypatch.setattr(web, "request", NS(username="admin"))
    cfg, _ = parse_runtime_config({"decision_provider": "native/jev"})
    host = NS(_runtime_config=cfg, jev=NS(snapshot=lambda: {"model": "jev", "calls": 3, "api_key": "SECRET"}))
    result = await DecisionStatusWebAPI(host).status()
    assert result["data"]["service"] == {"model": "jev", "calls": 3}
    assert "SECRET" not in str(result)
