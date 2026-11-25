"""Batch 2 P0-3 regression tests for narrative-delivery.sqlite3 indexes, WAL checkpoint, and cleanup."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.narrative import NarrativeCoordinator
from astrbot_plugin_chat_dynamics.core.narrative_store import NarrativeJournal


def _bundle_payload(fp: str, seq: int = 0) -> str:
    return json.dumps(
        {
            "scope": {"umo": "qq:GroupMessage:100", "persona_id": "p", "instance_id": "i", "platform": "qq"},
            "character": {"fingerprint": fp, "core_prompt": f"prompt-{fp}-" + ("x" * 120)},
            "context_items": [],
            "seq": seq,
        },
        ensure_ascii=False,
    )


def test_narrative_journal_indexes_eliminate_full_table_scans(tmp_path: Path):
    db_path = tmp_path / "narrative-delivery.sqlite3"
    j = NarrativeJournal(db_path)
    j.ensure_indexes()
    try:
        # Seed representative rows so SQLite's planner evaluates non-empty tables.
        j.save_bundle("b1", "t1", "u1", _bundle_payload("fp1", 1), captured=10.0)
        j.enqueue(
            {
                "event_id": "e-deliv",
                "type": "delivery.success",
                "scope": {"umo": "u1", "platform": "qq", "persona_id": "p", "instance_id": "i"},
                "data": {"message_id": "m1"},
                "text": "hi",
            },
            (),
            100.0,
        )
        j.enqueue(
            {
                "event_id": "e-rem",
                "type": "execution.success",
                "scope": {"umo": "u1"},
                "data": {"effect_kind": "reminder.created", "reminder_id": "r1"},
            },
            (),
            100.0,
        )
        j.enqueue({"event_id": "e-pend", "type": "message.received", "scope": {"umo": "u1"}}, ("memory",), 100.0)
        j.begin({"umo": "u1"}, "t1", 0, 100.0)
        j.add_intent({"intent_id": "i1", "scope": {"umo": "u1"}})

        checks = [
            (
                "SELECT bundle_id FROM bundles WHERE captured_at<? "
                "AND json_extract(payload,'$.archive_only')=1 ORDER BY captured_at ASC LIMIT 20",
                (500.0,),
                "bundle_archive_only_captured",
            ),
            (
                "SELECT rowid FROM events WHERE recipients='[]' "
                "AND COALESCE(json_extract(payload,'$.data.effect_kind'),'')!='reminder.created' "
                "AND created_at<? ORDER BY created_at ASC LIMIT 20",
                (500.0,),
                "events_done_non_reminder_created",
            ),
            (
                "SELECT count(*) FROM events WHERE recipients!='[]' AND json_extract(payload,'$.scope.umo')=?",
                ("u1",),
                "pending_event_scope",
            ),
            (
                "SELECT payload FROM events WHERE json_extract(payload,'$.type')='delivery.success' "
                "AND json_extract(payload,'$.scope.umo')=? AND created_at>=? ORDER BY created_at DESC LIMIT 40",
                ("u1", 10.0),
                "delivered_scope_created",
            ),
            (
                "SELECT payload FROM events WHERE json_extract(payload,'$.type')='delivery.success' "
                "AND json_extract(payload,'$.scope.umo')=? ORDER BY created_at DESC LIMIT 20",
                ("u1",),
                "delivered_scope_created",
            ),
            (
                "SELECT payload FROM events WHERE json_extract(payload,'$.scope.umo')=? "
                "AND json_extract(payload,'$.data.reminder_id')=? "
                "AND json_extract(payload,'$.data.effect_kind')='reminder.created' LIMIT 1",
                ("u1", "r1"),
                "reminder_created_lookup",
            ),
            (
                "SELECT rowid FROM events WHERE recipients='[]' "
                "AND json_extract(payload,'$.data.effect_kind')='reminder.created' "
                "AND created_at<? ORDER BY created_at ASC LIMIT 20",
                (500.0,),
                "reminder_created_at",
            ),
            (
                "SELECT attempt_id FROM attempts WHERE status IN ('success','failed','cancelled') "
                "AND COALESCE(finished_at, started_at)<? LIMIT 20",
                (500.0,),
                "attempt_status_ts",
            ),
            (
                "SELECT attempt_id FROM attempts WHERE status='uncertain' "
                "AND COALESCE(finished_at, started_at)<? LIMIT 20",
                (500.0,),
                "attempt_status_ts",
            ),
            (
                "SELECT payload, status FROM intents WHERE status IN ('pending','sending','uncertain','failed')",
                (),
                "intent_status_attempted",
            ),
            (
                "SELECT intent_id FROM intents WHERE status='uncertain' "
                "AND COALESCE(attempted_at, 0)>0 AND COALESCE(attempted_at, 0)<? LIMIT 20",
                (500.0,),
                "intent_status_attempted",
            ),
        ]
        with j._tx() as db:
            for sql, params, expected_idx in checks:
                details = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]
                joined = " | ".join(details)
                assert expected_idx in joined, f"Expected index {expected_idx} for query: {sql}\nPlan: {joined}"
                assert "USE TEMP B-TREE" not in joined, f"Unexpected temp B-tree sort for {sql}\nPlan: {joined}"
    finally:
        j.close()


def test_maintain_avoids_per_tick_truncate_checkpoint_and_prefers_passive(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "narrative-delivery.sqlite3"
    j = NarrativeJournal(db_path)
    j.ensure_indexes()
    try:
        clock = {"t": 1000.0}
        monkeypatch.setattr("astrbot_plugin_chat_dynamics.core.narrative_store.time.monotonic", lambda: clock["t"])

        for i in range(4):
            j.enqueue({"event_id": f"ev-{i}", "type": "message.received"}, (), now=10.0 + i)

        # First maintain -> initial PASSIVE checkpoint (never TRUNCATE by default)
        m1 = j.maintain(
            10 * 86400,
            retention_days=7,
            checkpoint_interval_seconds=300.0,
            truncate_interval_seconds=3600.0,
        )
        assert m1["pruned_events"] == 4
        assert m1["checkpointed"] is True
        assert m1["checkpoint_mode"] == "PASSIVE"

        # 30 seconds later, even with pruned_total > 0, checkpoint MUST be skipped (no 30s lock stall)
        clock["t"] += 30.0
        j.enqueue({"event_id": "ev-second", "type": "message.received"}, (), now=10.0)
        m2 = j.maintain(
            10 * 86400,
            retention_days=7,
            checkpoint_interval_seconds=300.0,
            truncate_interval_seconds=3600.0,
        )
        assert m2["pruned_events"] == 1
        assert m2["checkpointed"] is False
        assert m2["checkpoint_mode"] == "none"

        # After 300s interval -> PASSIVE checkpoint runs again
        clock["t"] += 301.0
        m3 = j.maintain(
            10 * 86400,
            retention_days=7,
            checkpoint_interval_seconds=300.0,
            truncate_interval_seconds=3600.0,
        )
        assert m3["checkpointed"] is True
        assert m3["checkpoint_mode"] == "PASSIVE"

        # When WAL exceeds truncate threshold AND truncate interval elapsed -> TRUNCATE runs once
        clock["t"] += 30.0
        j.enqueue({"event_id": "ev-wal", "type": "message.received"}, (), now=10 * 86400)
        wal_path = Path(str(db_path) + "-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0
        m4 = j.maintain(
            10 * 86400,
            retention_days=7,
            checkpoint_interval_seconds=300.0,
            truncate_interval_seconds=3600.0,
            wal_passive_threshold_bytes=1,
            wal_truncate_threshold_bytes=1,
        )
        assert m4["checkpointed"] is True
        assert m4["checkpoint_mode"] == "TRUNCATE"

        # Immediate next tick within truncate_interval_seconds -> downgrades to PASSIVE (not TRUNCATE)
        clock["t"] += 30.0
        j.enqueue({"event_id": "ev-wal-2", "type": "message.received"}, (), now=10 * 86400)
        m5 = j.maintain(
            10 * 86400,
            retention_days=7,
            checkpoint_interval_seconds=300.0,
            truncate_interval_seconds=3600.0,
            wal_passive_threshold_bytes=1,
            wal_truncate_threshold_bytes=1,
        )
        assert m5["checkpointed"] is True
        assert m5["checkpoint_mode"] == "PASSIVE"
    finally:
        j.close()


def test_character_blobs_orphan_pruning_new_and_legacy(tmp_path: Path):
    # 1) New DB: shared blob survives while referenced, pruned once all refs expire
    db_path = tmp_path / "new.sqlite3"
    j = NarrativeJournal(db_path)
    j.ensure_indexes()
    try:
        j.save_bundle("b-old-shared", "t1", "u1", _bundle_payload("fp-shared", 1), captured=100.0)
        j.save_bundle("b-old-only", "t2", "u1", _bundle_payload("fp-orphan", 2), captured=100.0)
        j.save_bundle("b-fresh-shared", "t3", "u1", _bundle_payload("fp-shared", 3), captured=35 * 86400)

        # Day 10: compact old bundles to archive_only
        j.maintain(10 * 86400, retention_days=7, archive_retention_days=30)
        # Day 41: old bundles (>30d) expire; fp-orphan is pruned, fp-shared stays for b-fresh-shared
        m = j.maintain(41 * 86400, retention_days=7, archive_retention_days=30)
        assert m["pruned_archives"] == 2
        assert m["pruned_character_blobs"] == 1
        with j._tx() as db:
            fps = [r[0] for r in db.execute("SELECT fingerprint FROM character_blobs").fetchall()]
        assert fps == ["fp-shared"]
        assert j.full_bundle("b-fresh-shared")["character"]["fingerprint"] == "fp-shared"

        # Day 70: compact & expire b-fresh-shared too -> fp-shared is now pruned
        j.maintain(45 * 86400, retention_days=7, archive_retention_days=30)
        m_final = j.maintain(70 * 86400, retention_days=7, archive_retention_days=30)
        assert m_final["pruned_archives"] == 1
        assert m_final["pruned_character_blobs"] == 1
        with j._tx() as db:
            assert db.execute("SELECT count(*) FROM character_blobs").fetchone()[0] == 0
    finally:
        j.close()

    # 2) Legacy DB created without character_blob_refs table and without last_used_at column
    legacy_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, recipients TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, umo TEXT NOT NULL, turn_id TEXT NOT NULL, intent_id TEXT, segment INTEGER NOT NULL, status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, message_id TEXT);
        CREATE TABLE intents (intent_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, attempted_at REAL);
        CREATE TABLE intent_retries (intent_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, next_retry REAL NOT NULL, error_code TEXT NOT NULL);
        CREATE TABLE bundles (bundle_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, umo TEXT NOT NULL, payload TEXT NOT NULL, captured_at REAL NOT NULL);
        CREATE TABLE context_archive (bundle_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, payload BLOB NOT NULL);
        CREATE TABLE character_blobs (fingerprint TEXT PRIMARY KEY, payload BLOB NOT NULL);
        CREATE TABLE maintenance (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE bundle_compaction_queue (bundle_id TEXT PRIMARY KEY, captured_at REAL NOT NULL);
        """
    )
    live_char = json.dumps({"fingerprint": "fp-live", "core_prompt": "keep"}).encode("utf-8")
    dead_char = json.dumps({"fingerprint": "fp-dead", "core_prompt": "orphan"}).encode("utf-8")
    live_arch = json.dumps({"character_blob_ref": "fp-live", "ok": True}).encode("utf-8")
    conn.execute("INSERT INTO character_blobs VALUES (?,?)", ("fp-live", gzip.compress(live_char)))
    conn.execute("INSERT INTO character_blobs VALUES (?,?)", ("fp-dead", gzip.compress(dead_char)))
    conn.execute(
        "INSERT INTO context_archive VALUES (?,?,?)",
        ("b-live", hashlib.sha256(live_arch).hexdigest(), gzip.compress(live_arch)),
    )
    conn.execute(
        "INSERT INTO bundles VALUES (?,?,?,?,?)",
        ("b-live", "t1", "u1", json.dumps({"archive_id": "b-live"}), 5000.0),
    )
    conn.commit()
    conn.close()

    jl = NarrativeJournal(legacy_path)
    jl.ensure_indexes()
    try:
        res = jl.maintain(6000.0, retention_days=7, archive_retention_days=30)
        assert res["pruned_character_blobs"] == 1
        with jl._tx() as db:
            fps = [r[0] for r in db.execute("SELECT fingerprint FROM character_blobs").fetchall()]
        assert fps == ["fp-live"]
        assert jl.full_bundle("b-live")["character"]["core_prompt"] == "keep"
    finally:
        jl.close()


def test_uncertain_attempts_and_intents_expire_after_ttl(tmp_path: Path):
    db_path = tmp_path / "uncertain.sqlite3"
    j = NarrativeJournal(db_path)
    j.ensure_indexes()
    j.add_intent({"intent_id": "int-timed", "scope": {"umo": "u1"}})
    j.add_intent({"intent_id": "int-untimed", "scope": {"umo": "u1"}, "created_at": 100.0})
    j.begin({"umo": "u1"}, "t1", 0, 100.0, intent_id="int-timed")
    j.intent_status("int-untimed", "uncertain")
    with j._tx(write=True) as db:
        db.execute("INSERT INTO intent_retries VALUES ('int-timed', 2, 200.0, 'TimeoutError')")
    j.close()

    # Reopen -> sending attempt/intent transition to 'uncertain'
    j2 = NarrativeJournal(db_path)
    j2.ensure_indexes()
    try:
        # Before TTL (day 3 < 7d): preserved
        m_early = j2.maintain(3 * 86400, retention_days=7)
        assert m_early["pruned_uncertain_attempts"] == 0
        assert m_early["pruned_uncertain_intents"] == 0

        # After TTL (day 9 > 7d): expired uncertain attempts, intents, and orphan retries pruned
        m_late = j2.maintain(9 * 86400, retention_days=7)
        assert m_late["pruned_uncertain_attempts"] == 1
        assert m_late["pruned_uncertain_intents"] == 2
        with j2._tx() as db:
            assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM intents").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM intent_retries").fetchone()[0] == 0
    finally:
        j2.close()


def test_reminder_created_events_preserved_short_term_and_pruned_after_retention(tmp_path: Path):
    db_path = tmp_path / "reminders.sqlite3"
    j = NarrativeJournal(db_path)
    j.ensure_indexes()
    try:
        rem_scope = {"umo": "u1", "platform": "qq", "persona_id": "p", "instance_id": "i"}
        j.enqueue(
            {
                "event_id": "rem-1",
                "type": "execution.success",
                "scope": rem_scope,
                "data": {"effect_kind": "reminder.created", "reminder_id": "r-42"},
            },
            (),
            100.0,
        )
        j.enqueue({"event_id": "normal-1", "type": "message.received", "scope": rem_scope}, (), 100.0)

        # Day 15: normal event pruned (>7d), reminder.created preserved (<90d)
        m1 = j.maintain(15 * 86400, retention_days=7, reminder_retention_days=90)
        assert m1["pruned_events"] == 1
        assert m1["pruned_reminders"] == 0
        assert j.reminder_scope("u1", "r-42") == rem_scope

        # Day 95: reminder.created exceeds 90d retention and is pruned cleanly
        m2 = j.maintain(95 * 86400, retention_days=7, reminder_retention_days=90)
        assert m2["pruned_reminders"] == 1
        assert m2["pruned_events"] == 1
        assert j.reminder_scope("u1", "r-42") is None
    finally:
        j.close()


def test_auto_vacuum_detection_on_legacy_none_vs_incremental_db(tmp_path: Path):
    # Legacy DB with auto_vacuum = NONE (0)
    legacy_path = tmp_path / "legacy-none.sqlite3"
    conn = sqlite3.connect(legacy_path)
    conn.execute("PRAGMA auto_vacuum=NONE")
    conn.execute("CREATE TABLE seed (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    j_legacy = NarrativeJournal(legacy_path)
    j_legacy.ensure_indexes()
    try:
        status = j_legacy.storage_status()
        assert status["auto_vacuum_mode"] == 0
        assert status["auto_vacuum_incremental"] is False
        j_legacy.enqueue({"event_id": "e1", "type": "message.received"}, (), 10.0)
        res = j_legacy.maintain(10 * 86400, retention_days=7)
        assert res["auto_vacuum_mode"] == 0
        assert res["vacuumed"] is False
    finally:
        j_legacy.close()

    # Fresh DB with auto_vacuum = INCREMENTAL (2)
    fresh_path = tmp_path / "fresh-inc.sqlite3"
    j_fresh = NarrativeJournal(fresh_path)
    j_fresh.ensure_indexes()
    try:
        status = j_fresh.storage_status()
        assert status["auto_vacuum_mode"] == 2
        assert status["auto_vacuum_incremental"] is True
        j_fresh.enqueue({"event_id": "e1", "type": "message.received"}, (), 10.0)
        res = j_fresh.maintain(10 * 86400, retention_days=7)
        assert res["auto_vacuum_mode"] == 2
        assert res["vacuumed"] is True
    finally:
        j_fresh.close()


@pytest.mark.asyncio
async def test_coordinator_maintains_existing_db_even_when_narrative_disabled(tmp_path: Path, monkeypatch):
    def _make_plugin(wall: float):
        return SimpleNamespace(
            _shutting_down=False,
            _runtime_config=SimpleNamespace(
                narrative_enabled=False,
                narrative_context_retention_days=7,
                narrative_archive_retention_days=30,
                narrative_dead_letter_ttl_days=7,
            ),
            time_service=SimpleNamespace(wall_time=lambda: wall),
            integrations=SimpleNamespace(narrative_provider=lambda _name: None),
            context=SimpleNamespace(get_all_stars=lambda: []),
        )

    # 1) Empty DB + narrative_enabled=False -> exits immediately without looping
    empty_coord = NarrativeCoordinator(_make_plugin(100 * 86400), tmp_path / "empty.sqlite3")
    empty_coord._DISABLED_MAINTENANCE_INTERVAL_SECONDS = 0.01
    try:
        assert empty_coord.journal.has_maintenance_backlog() is False
        await asyncio.wait_for(empty_coord.run(), timeout=2.0)
    finally:
        empty_coord.journal.close()

    # 2) Existing DB with expired rows + narrative_enabled=False -> still runs maintain() and drains backlog
    hist_path = tmp_path / "historical.sqlite3"
    seed = NarrativeJournal(hist_path)
    seed.ensure_indexes()
    seed.enqueue({"event_id": "old-ev", "type": "message.received"}, (), 10.0)
    seed.close()

    plugin = _make_plugin(100 * 86400)
    coord = NarrativeCoordinator(plugin, hist_path)
    coord._DISABLED_MAINTENANCE_INTERVAL_SECONDS = 0.01
    dispatch_intents_mock = AsyncMock()
    monkeypatch.setattr(coord, "dispatch_intents", dispatch_intents_mock)
    try:
        assert coord.journal.has_maintenance_backlog() is True
        await asyncio.wait_for(coord.run(), timeout=3.0)
        with coord.journal._tx() as db:
            assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        dispatch_intents_mock.assert_not_awaited()
    finally:
        coord.journal.close()
