"""Focused unit tests for 2GB RAM performance & memory optimizations in chat_dynamics."""

from __future__ import annotations

import array
import asyncio
import json
import sqlite3
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core import runtime_persistence as codec
from astrbot_plugin_chat_dynamics.core import series_hub
from astrbot_plugin_chat_dynamics.core.decision_status import history_page
from astrbot_plugin_chat_dynamics.core.embedding_adapter import EmbeddingAdapter
from astrbot_plugin_chat_dynamics.core.exact_calls import ExactMemo
from astrbot_plugin_chat_dynamics.core.narrative import NarrativeCoordinator, TurnContextBundle
from astrbot_plugin_chat_dynamics.core.narrative_store import NarrativeJournal
from astrbot_plugin_chat_dynamics.core.natural_rhythm import NaturalRhythmController
from astrbot_plugin_chat_dynamics.core.persona_engine import PersonaEngine
from astrbot_plugin_chat_dynamics.core.series_telemetry import SeriesTelemetryStore
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRegistry


class _FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def wall_time(self) -> float:
        return 1_700_000_000.0 + self.now


def test_drop_session_clears_rhythm_pokes_and_narrative_state():
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin, _session_key

    plugin = _plugin()
    key = _session_key("leak_check_group")
    plugin._get_or_create_runtime(key, group_id="leak_check_group", umo=key)
    plugin._umo_by_session[key] = key

    # Populate state that previously leaked on _drop_session()
    book = plugin.natural_rhythm._book(key)
    book.recent_pings["u1"] = deque([100.0])
    plugin._poke_streaks[(key, "u1")] = [100.0]
    plugin._poke_replied_ids.add((key, "poke-1"))
    plugin.narrative._social_opportunity_at[key] = 123.0
    plugin.narrative._rpc_warn_at[key] = [123.0, 1]
    plugin.narrative._bundles[key] = TurnContextBundle("t1", json.dumps({"scope": {"umo": key}}))

    plugin._drop_session(key)

    assert key not in plugin.natural_rhythm._books
    assert (key, "u1") not in plugin._poke_streaks
    assert all(item[0] != key for item in plugin._poke_replied_ids)
    assert key not in plugin.narrative._social_opportunity_at
    assert key not in plugin.narrative._rpc_warn_at
    assert key not in plugin.narrative._bundles


def test_natural_rhythm_prunes_stale_and_excess_recent_pings():
    clock = _FakeClock(100.0)
    plugin = SimpleNamespace(
        _runtime_config=SimpleNamespace(natural_rhythm_enabled=True),
        _metrics={},
        time_service=clock,
    )
    ctrl = NaturalRhythmController(plugin)
    runtime = SimpleNamespace(session_key="g:1")

    # Insert 300 distinct users; cap is 256
    for idx in range(300):
        clock.now = 100.0 + idx
        ctrl.on_user_message_observed(
            runtime,
            msg_id=f"m{idx}",
            user_id=f"u{idx}",
            text="hi",
            directed=True,
        )
    book = ctrl._books["g:1"]
    assert len(book.recent_pings) <= ctrl.MAX_RECENT_PING_USERS
    assert "u0" not in book.recent_pings
    assert "u299" in book.recent_pings

    # Advance > 3600s and observe one ping: all stale users must be pruned.
    clock.now += 4000.0
    ctrl.on_user_message_observed(
        runtime,
        msg_id="m_fresh",
        user_id="fresh_user",
        text="ping",
        directed=True,
    )
    assert set(book.recent_pings.keys()) == {"fresh_user"}


def test_bundle_compaction_and_profile_bundle_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory_aggressive")
    plugin = SimpleNamespace(
        context=None,
        _runtime_config=SimpleNamespace(narrative_enabled=True),
        time_service=_FakeClock(),
    )
    coord = NarrativeCoordinator(plugin, tmp_path / "narr.sqlite3")
    try:
        for i in range(15):
            umo = f"aiocqhttp:GroupMessage:{i}"
            payload = {
                "scope": {"umo": umo},
                "host_history_snapshot": {
                    "conversation_id": f"c{i}",
                    "snapshot_fingerprint": "fp",
                    "history_epoch": "e1",
                    "rows": [{"role": "user", "content": "x" * 500} for _ in range(20)],
                    "source_refs": [{"index": 0, "text": "x" * 100}],
                },
            }
            b = TurnContextBundle(f"t{i}", json.dumps(payload))
            _ = b.wire()  # Populate _wire_cache on the live bundle
            coord._cache_bundle(umo, b)

        # low_memory_aggressive caps _bundles at 8
        assert len(coord._bundles) == 8
        cached_bundle = next(iter(coord._bundles.values()))
        # Cached bundle must not retain _wire_cache or heavy host_history_snapshot.rows
        assert getattr(cached_bundle, "_wire_cache", None) is None
        parsed = json.loads(cached_bundle.payload)
        assert "rows" not in parsed["host_history_snapshot"]
        assert "source_refs" not in parsed["host_history_snapshot"]
        assert parsed["host_history_snapshot"]["source_rows_count"] == 20
        assert parsed["host_history_snapshot"]["referenceable_rows_count"] == 1
    finally:
        coord.journal.close()


def test_embedding_adapter_uses_float32_array_and_hashes_long_keys():
    adapter = EmbeddingAdapter(enabled=True, cache_size=64)
    short_text = "short text"
    long_text = "long_message_" + ("x" * 300)

    short_vec = adapter.remember(short_text, [3.0, 4.0])
    long_vec = adapter.remember(long_text, [3.0, 4.0])

    assert isinstance(short_vec, tuple)
    assert short_vec[0] == pytest.approx(0.6, abs=1e-5)
    assert short_vec[1] == pytest.approx(0.8, abs=1e-5)
    assert long_vec[0] == pytest.approx(0.6, abs=1e-5)

    # Storage uses array('f') and hashes keys > 128 chars
    assert short_text in adapter._cache
    assert isinstance(adapter._cache[short_text], array.array)
    assert adapter._cache[short_text].typecode == "f"
    assert long_text not in adapter._cache
    hashed_keys = [k for k in adapter._cache if k.startswith("sha256:")]
    assert len(hashed_keys) == 1
    assert isinstance(adapter._cache[hashed_keys[0]], array.array)
    assert adapter._cache[hashed_keys[0]].typecode == "f"

    # cached() and match() work identically for long hashed keys
    assert adapter.cached(long_text) is not None
    m = adapter.match(short_text, long_text)
    assert m.backend == "neural"
    assert m.embedding_cosine == pytest.approx(1.0, abs=1e-3)


def test_series_telemetry_idle_wait_and_pragmas(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    db_path = tmp_path / "telemetry.sqlite3"
    store = SeriesTelemetryStore(db_path, flush_interval_seconds=0.3)
    try:
        # Verify SQLite PRAGMAs were applied
        with store._lock:
            assert store._conn is not None
            cache_size = store._conn.execute("PRAGMA cache_size").fetchone()[0]
            temp_store = store._conn.execute("PRAGMA temp_store").fetchone()[0]
            mmap_size = store._conn.execute("PRAGMA mmap_size").fetchone()[0]
            assert cache_size == -1024
            assert temp_store == 1  # FILE
            assert mmap_size == 0

        # Give worker thread a moment to park on _dirty_event
        time.sleep(0.05)
        assert not store._dirty_event.is_set()

        # Recording wakes worker and flushes cleanly
        store.record(
            plugin_id="astrbot_plugin_chat_dynamics",
            provider_id="p1",
            purpose="reply",
            generation_seconds=0.1,
            ok=True,
        )
        store.flush()
        summary = store.today_usage_summary()
        assert summary["calls"] == 1
    finally:
        store.close()
        assert store._flush_thread is None


def test_search_executor_respects_resource_profile(monkeypatch):
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory_aggressive")
    series_hub._shutdown_search_executor()
    try:
        ex = series_hub._get_search_executor()
        assert ex._max_workers == 1
    finally:
        series_hub._shutdown_search_executor()

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    try:
        ex2 = series_hub._get_search_executor()
        assert ex2._max_workers == 2
    finally:
        series_hub._shutdown_search_executor()


def test_narrative_journal_pragmas_sql_filter_backfill_and_auto_vacuum(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    db_path = tmp_path / "narrative-delivery.sqlite3"
    journal = NarrativeJournal(db_path)
    try:
        with journal._tx() as db:
            assert db.execute("PRAGMA cache_size").fetchone()[0] == -1024
            assert db.execute("PRAGMA temp_store").fetchone()[0] == 1
            assert db.execute("PRAGMA mmap_size").fetchone()[0] == 0

        # Test SQL umo filtering in intents() and snapshot()
        journal.add_intent({"intent_id": "i1", "scope": {"umo": "p:GroupMessage:1"}})
        journal.add_intent({"intent_id": "i2", "scope": {"umo": "p:GroupMessage:2"}})
        assert len(journal.intents(umo="p:GroupMessage:1")) == 1
        assert journal.intents(umo="p:GroupMessage:1")[0][0]["intent_id"] == "i1"
        snap = journal.snapshot("p:GroupMessage:2")
        assert [row["intent_id"] for row in snap["intents"]] == ["i2"]

        # Test faster blob_ref_backfill batching (200 rows in one maintain call)
        now = 1_700_000_000.0
        for i in range(150):
            journal.save_bundle(
                f"b{i}",
                f"t{i}",
                "p:GroupMessage:1",
                json.dumps(
                    {
                        "scope": {"umo": "p:GroupMessage:1"},
                        "character": {"fingerprint": "fp-1", "prompt": "p"},
                    }
                ),
                now - 10,
            )
        # Simulate legacy DB needing backfill
        with journal._tx(write=True) as db:
            db.execute("DELETE FROM character_blob_refs")
            db.execute("DELETE FROM maintenance WHERE name IN ('blob_ref_backfill_done', 'blob_ref_backfill_cursor')")
        journal.maintain(now, limit=40)
        with journal._tx() as db:
            done = db.execute("SELECT value FROM maintenance WHERE name='blob_ref_backfill_done'").fetchone()
            ref_cnt = db.execute("SELECT count(*) FROM character_blob_refs").fetchone()[0]
        assert done is not None and done[0] == "1"
        assert ref_cnt == 150
    finally:
        journal.close()

    # Test legacy auto_vacuum=0 migration with freelist
    legacy_path = tmp_path / "legacy-narrative.sqlite3"
    raw_conn = sqlite3.connect(legacy_path)
    raw_conn.execute("PRAGMA auto_vacuum=NONE")
    raw_conn.execute("CREATE TABLE filler (id INTEGER PRIMARY KEY, blob TEXT)")
    raw_conn.executemany("INSERT INTO filler(blob) VALUES (?)", [("x" * 4000,) for _ in range(300)])
    raw_conn.commit()
    raw_conn.execute("DELETE FROM filler")
    raw_conn.commit()
    raw_conn.close()

    legacy_journal = NarrativeJournal(legacy_path)
    try:
        assert legacy_journal.needs_auto_vacuum_migration(min_freelist_pages=10) is True
        res = legacy_journal.enable_incremental_vacuum_if_needed(min_freelist_pages=10)
        assert res["migrated"] is True
        assert res["auto_vacuum"] == 2
        assert legacy_journal.needs_auto_vacuum_migration(min_freelist_pages=10) is False
    finally:
        legacy_journal.close()


def test_resource_profile_four_tier_limits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory_aggressive")
    clock = _FakeClock()

    # SessionRegistry
    reg = SessionRegistry(clock, context=SimpleNamespace())
    assert reg.max_sessions == 48
    assert reg.max_nodes == 120

    # ExactMemo
    memo = ExactMemo(context=SimpleNamespace())
    assert memo.max_bytes == 512 * 1024

    # PersonaEngine.slots
    pe = PersonaEngine(SimpleNamespace(context=SimpleNamespace()))
    assert pe.slots._value == 2

    # runtime_persistence effective limits
    fake_plugin = SimpleNamespace(context=SimpleNamespace(), _registry=reg)
    eff_bytes, eff_sessions, eff_nodes = codec._effective_persist_limits(fake_plugin)
    assert eff_bytes == 768 * 1024
    assert eff_sessions == 48
    assert eff_nodes == 120

    # decision_status.history_page still works cleanly with PRAGMAs applied
    hist_db = tmp_path / "decision-learning.sqlite3"
    conn = sqlite3.connect(hist_db)
    conn.execute("CREATE TABLE samples (session TEXT, payload TEXT)")
    conn.execute("INSERT INTO samples VALUES ('s1', ?)", (json.dumps({"id": "1", "task_id": "t"}),))
    conn.commit()
    conn.close()
    page = history_page(hist_db, cursor=0, limit=10)
    assert len(page["records"]) == 1
    assert page["records"][0]["id"] == "1"

    # Standard profile values
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "standard")
    reg_std = SessionRegistry(clock, context=SimpleNamespace())
    assert reg_std.max_sessions == 256
    assert reg_std.max_nodes == 350
    assert ExactMemo(context=SimpleNamespace()).max_bytes == 3 * 1024 * 1024
    assert PersonaEngine(SimpleNamespace(context=SimpleNamespace())).slots._value == 4


def test_narrative_coordinator_run_wires_profile_archive_limits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory_aggressive")
    clock = _FakeClock()
    captured_kwargs = {}

    plugin = SimpleNamespace(
        context=None,
        _shutting_down=False,
        _runtime_config=SimpleNamespace(
            narrative_enabled=True,
            narrative_context_retention_days=7,
            narrative_archive_retention_days=30,
            narrative_dead_letter_ttl_days=7,
        ),
        time_service=clock,
        integrations=SimpleNamespace(narrative_provider=lambda _name: None),
    )
    coord = NarrativeCoordinator(plugin, tmp_path / "narr-run.sqlite3")
    orig_maintain = coord.journal.maintain

    def _spy_maintain(*args, **kwargs):
        captured_kwargs.update(kwargs)
        plugin._shutting_down = True
        return orig_maintain(*args, **kwargs)

    monkeypatch.setattr(coord.journal, "maintain", _spy_maintain)

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(coord, "dispatch", _noop)
    monkeypatch.setattr(coord, "dispatch_intents", _noop)
    monkeypatch.setattr(coord, "_ACTIVE_LOOP_INTERVAL_SECONDS", 0.001)

    try:
        asyncio.run(coord.run())
        assert captured_kwargs["archive_retention_days"] == 2
        assert captured_kwargs["max_archive_rows"] == 500
    finally:
        coord.journal.close()
