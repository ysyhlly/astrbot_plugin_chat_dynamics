"""Removal must retain manual data, refuse old navigation and ignore stale flags."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core import web_api
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.tests.test_topic_annotations import fixture_plugin, label
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations

ROOT = Path(__file__).resolve().parents[1]


def test_old_enabled_flags_cannot_reenable_removed_feature():
    config, _warnings = parse_runtime_config({"annotation_draft_enabled": True,
                                   "annotation_draft_auto_enabled": True})
    assert not hasattr(config, "annotation_draft_enabled")
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert not any(key.startswith("annotation_draft_") for key in schema)


def test_register_keeps_manual_endpoints_only():
    register = Mock()
    registered = [(f"/{web_api.PLUGIN_NAME}/annotation_draft", Mock(), ["POST"], "old"),
                  (f"/{web_api.PLUGIN_NAME}/annotation_drafts", Mock(), ["GET"], "old"),
                  (f"/{web_api.PLUGIN_NAME}/annotation_drafts", Mock(), ["POST"], "old"),
                  ("/another_plugin/annotation_drafts", Mock(), ["GET"], "keep")]
    context = SimpleNamespace(register_web_api=register, registered_web_apis=registered)
    api = web_api.ConsoleWebAPI(SimpleNamespace(context=context))
    api.register()
    paths = [call.args[0] for call in register.call_args_list]
    assert len([path for path in paths if path.endswith("/topic_annotations")]) == 2
    assert not any("annotation_draft" in path for path in paths)
    assert len(registered) == 1 and registered[0][0] == "/another_plugin/annotation_drafts"


@pytest.mark.asyncio
async def test_removed_page_cannot_get_signed_navigation(monkeypatch, offline_web_responses):
    monkeypatch.setattr(web_api, "query_value", lambda name: "drafts" if name == "page" else "")
    api = web_api.ConsoleWebAPI(SimpleNamespace(_shutting_down=False))
    assert (await api.page_nav())["status_code"] == 400


@pytest.mark.asyncio
async def test_old_drafts_are_never_read_and_human_label_remains_manual():
    plugin, kv = fixture_plugin()
    store = TopicAnnotations(plugin)
    old_key = "annotation_drafts_v1_" + store.digest("a")
    kv[old_key] = {"drafts": {"m": {"expected_reply": True}}}
    old_get = plugin.get_kv_data

    async def get(key, default):
        assert key != old_key
        return await old_get(key, default)

    plugin.get_kv_data = get
    result = await store.save(label(expected_reply=True))
    assert result["record"]["label_source"] == "human"
    assert "accepted_from" not in result["record"]
    payload = await TopicAnnotations(plugin).read("a")
    assert len(payload["records"]) == 1
    assert "drafts" not in payload
    await store.reconcile()
    assert kv[old_key]["drafts"]  # Existing KV data is left intact.


@pytest.mark.asyncio
async def test_legacy_approved_label_keeps_history_and_provenance():
    plugin, kv = fixture_plugin()
    store = TopicAnnotations(plugin)
    kv[store.state_key("a")] = {
        "labels": [{"msg_id": "old", "predicted_topic": "t", "expected_topic": "t",
                    "error_type": "correct", "accepted_from": "ai"}],
        "projection_pending": True,
    }
    kv["annotation_drafts_index_v1"] = ["a"]
    assert (await store.reconcile())["repaired"] == 1
    assert kv[store.key("a")][0]["accepted_from"] == "ai"
    payload = await store.read("a")
    assert payload["metrics"]["ai_assisted"] == 1
    assert "历史记录" in payload["metrics"]["sample_note"]
