"""Cancellation receipts, pre-provider retry boundaries and verifiable storage."""
import asyncio
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.member_boundary import BoundaryCancelled
from astrbot_plugin_chat_dynamics.core.native_memory_bridge import _NativeMemoryValidationProvider
from astrbot_plugin_chat_dynamics.core.narrative_store import NarrativeJournal
from astrbot_plugin_chat_dynamics.core.outcome_recorder import read_outcome
from astrbot_plugin_chat_dynamics.core.reply_failure import ModelReplyFailure
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent, At


@pytest.mark.asyncio
async def test_superseded_generation_has_terminal_reason(jev_plugin, caplog):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending():
        entered.set()
        await release.wait()

    bridge.before_reply = pending
    event = MockEvent("处理这个任务", message_id="superseded", components=[At("bot_42")])
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await asyncio.wait_for(entered.wait(), 2)
        runtime = p._sessions[event.unified_msg_origin]
        runtime.user_revisions[event.sender_id] = runtime.user_revisions.get(event.sender_id, 0) + 1
        release.set()
        await drain(p)
        assert not event.replies_sent
        outcome = read_outcome(runtime.dag.get_node(event.message_id))
        assert outcome["suppression_reason"] == "member_revision_changed"
        assert outcome["final_outcome"] != "in_flight"
        assert "generation_unclosed" not in caplog.text
        assert not runtime.owned_turn_tasks and runtime.model_admission._value == 9
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_started,reason,retry", [
    (False, "provider_reply_failed", True),
    (True, "provider_reply_failed", False),
    (False, "provider_content_blocked", False),
])
async def test_memory_retry_only_before_provider_start(jev_plugin, provider_started, reason, retry):
    p, original_bridge = jev_plugin
    bridge = AstrBotAgentBridge(p.context)
    p.persona_engine.bridge = bridge
    event = MockEvent("测试", message_id="memory-retry")
    calls = []

    async def generate(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            event._cd_native_memory_source_invalidated_v1 = True
            event._cd_memory_provider_started_v1 = provider_started
            raise ModelReplyFailure(reason)
        assert event._cd_retry_without_memory_v1
        return "recovered"

    bridge.generate = generate
    try:
        call = p.persona_engine.generate(event, (event,), {}, original_bridge.persona, "test-provider")
        if retry:
            assert await call == "recovered"
            assert len(calls) == 2
            assert 0 < calls[1]["reply_timeout"] <= calls[0]["reply_timeout"]
            assert not hasattr(event, "_cd_retry_without_memory_v1")
        else:
            with pytest.raises(ModelReplyFailure):
                await call
            assert len(calls) == 1
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_memory_validation_marks_start_only_after_validation():
    event = NS()
    state = {"valid": False, "calls": 0}

    async def validate():
        return state["valid"]

    async def chat():
        state["calls"] += 1
        return "ok"

    provider = _NativeMemoryValidationProvider(NS(text_chat=chat), validate, event)
    with pytest.raises(BoundaryCancelled):
        await provider.text_chat()
    assert not getattr(event, "_cd_memory_provider_started_v1", False)
    assert state["calls"] == 0
    state["valid"] = True
    assert await provider.text_chat() == "ok"
    assert event._cd_memory_provider_started_v1 and state["calls"] == 1


def archive_module():
    path = Path(__file__).parents[1] / "scripts" / "archive_storage.py"
    spec = importlib.util.spec_from_file_location("cd_archive_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_roundtrip_includes_committed_wal_and_keeps_source(tmp_path):
    tool = archive_module()
    source = tmp_path / "live.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE receipts (message_id TEXT)")
        db.execute("INSERT INTO receipts VALUES ('delivered')")
        db.commit()
        output = tmp_path / "archives"
        info = tool.archive_database(source, output)
        assert info["source_deleted"] is False and source.exists()
        manifest = next(output.glob("*.json"))
        restored = tmp_path / "restored.sqlite3"
        tool.restore_archive(manifest, restored)
        with closing(sqlite3.connect(restored)) as copy:
            assert copy.execute("SELECT * FROM receipts").fetchall() == [("delivered",)]
        with pytest.raises(FileExistsError):
            tool.restore_archive(manifest, source)
        (output / info["archive"]).write_bytes(b"corrupted")
        with pytest.raises(ValueError):
            tool.restore_archive(manifest, tmp_path / "bad.sqlite3")
        assert not (tmp_path / "bad.sqlite3").exists()


def test_storage_status_exposes_sizes_without_message_content(tmp_path):
    journal = NarrativeJournal(tmp_path / "narrative.sqlite3")
    (tmp_path / "decision-learning.sqlite3").write_bytes(b"legacy")
    status = journal.storage_status()
    assert status["database_bytes"] > 0 and status["legacy_database_bytes"] == 6
    assert status["free_bytes"] > 0
    assert status["retention_policy"] == "compact_online_keep_verified_archive_and_receipts"
