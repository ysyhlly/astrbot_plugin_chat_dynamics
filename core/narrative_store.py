"""Durable outgoing attempts and idempotent cross-plugin event delivery."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
import gzip
import hashlib
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path


def _load_series_panels():
    try:
        from .. import series_panels as _sp

        return _sp
    except Exception:
        try:
            import series_panels as _sp

            return _sp
        except Exception:
            return None


class NarrativeJournal:
    """Keep platform uncertainty separate from retryable plugin notifications."""

    DEFAULT_ARCHIVE_RETENTION_DAYS = 30
    DEFAULT_MAX_ARCHIVE_ROWS = 50000
    DEFAULT_DEAD_LETTER_TTL_DAYS = 7
    DEFAULT_REMINDER_RETENTION_DAYS = 90
    DEFAULT_VACUUM_PAGES = 256
    DEFAULT_CHECKPOINT_INTERVAL_SECONDS = 300.0
    DEFAULT_TRUNCATE_INTERVAL_SECONDS = 3600.0
    DEFAULT_WAL_PASSIVE_THRESHOLD_BYTES = 8 * 1024 * 1024
    DEFAULT_WAL_TRUNCATE_THRESHOLD_BYTES = 32 * 1024 * 1024

    def __init__(self, path: Path, *, context=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._context = context
        self._conn_lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._last_checkpoint_at: float = 0.0
        self._last_truncate_at: float = 0.0
        with self._conn_lock:
            db = self._shared_connection()
            # Must be set before first table creation on new DBs; harmless no-op on existing DBs until VACUUM.
            try:
                db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            except sqlite3.DatabaseError:
                pass
            db.execute("PRAGMA journal_mode=WAL")
        with self._tx(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    recipients TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY, umo TEXT NOT NULL,
                    turn_id TEXT NOT NULL, intent_id TEXT, segment INTEGER NOT NULL,
                    status TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, message_id TEXT);
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    status TEXT NOT NULL, attempted_at REAL);
                CREATE TABLE IF NOT EXISTS intent_retries (
                    intent_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL,
                    next_retry REAL NOT NULL, error_code TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bundles (
                    bundle_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                    umo TEXT NOT NULL, payload TEXT NOT NULL, captured_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS context_archive (
                    bundle_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS character_blobs (
                    fingerprint TEXT PRIMARY KEY, payload BLOB NOT NULL, last_used_at REAL);
                CREATE TABLE IF NOT EXISTS character_blob_refs (
                    bundle_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS maintenance (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bundle_compaction_queue (
                    bundle_id TEXT PRIMARY KEY, captured_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS bundle_compaction_due ON bundle_compaction_queue(captured_at);
                CREATE INDEX IF NOT EXISTS character_blob_refs_fp ON character_blob_refs(fingerprint);
                CREATE INDEX IF NOT EXISTS delivered_message_lookup ON events(
                    json_extract(payload,'$.scope.umo'),json_extract(payload,'$.data.message_id'))
                    WHERE json_extract(payload,'$.type')='delivery.success';
            """)
            # Additive column migration for legacy databases created before last_used_at existed.
            blob_cols = {row[1] for row in db.execute("PRAGMA table_info(character_blobs)").fetchall()}
            if "last_used_at" not in blob_cols:
                db.execute("ALTER TABLE character_blobs ADD COLUMN last_used_at REAL")
            # Fresh/empty archive DBs do not need legacy character_blob_refs backfill.
            if db.execute("SELECT 1 FROM context_archive LIMIT 1").fetchone() is None:
                db.execute(
                    "INSERT OR IGNORE INTO maintenance(name,value) VALUES('blob_ref_backfill_done','1')"
                )
            # A crashed process cannot prove whether a platform accepted a send.
            db.execute("UPDATE attempts SET status='uncertain' WHERE status='sending'")
            db.execute("UPDATE intents SET status='uncertain' WHERE status='sending'")

    def _apply_pragmas(self, db: sqlite3.Connection) -> None:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        sp = _load_series_panels()
        if sp is not None:
            try:
                sp.apply_series_sqlite_pragmas(db, self._context)
            except Exception:
                pass

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        self._apply_pragmas(db)
        return db

    def _shared_connection(self) -> sqlite3.Connection:
        if self._db is None:
            db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            db.row_factory = sqlite3.Row
            self._apply_pragmas(db)
            self._db = db
        return self._db

    @contextmanager
    def _tx(self, *, write: bool = False, immediate: bool = False):
        with self._conn_lock:
            db = self._shared_connection()
            if immediate:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            elif write:
                with db:
                    yield db
            else:
                yield db

    def close(self):
        with self._conn_lock:
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None

    def ensure_indexes(self):
        """Build additive indexes in the maintenance worker, including legacy databases."""
        with self._tx(write=True) as db:
            db.executescript("""
                CREATE INDEX IF NOT EXISTS pending_event_created ON events(created_at) WHERE recipients!='[]';
                CREATE INDEX IF NOT EXISTS pending_event_scope ON events(json_extract(payload,'$.scope.umo')) WHERE recipients!='[]';
                CREATE INDEX IF NOT EXISTS events_done_created ON events(created_at) WHERE recipients='[]';
                CREATE INDEX IF NOT EXISTS events_done_non_reminder_created ON events(created_at)
                    WHERE recipients='[]' AND COALESCE(json_extract(payload,'$.data.effect_kind'),'')!='reminder.created';
                CREATE INDEX IF NOT EXISTS delivered_scope_created ON events(
                    json_extract(payload,'$.scope.umo'), created_at DESC)
                    WHERE json_extract(payload,'$.type')='delivery.success';
                CREATE INDEX IF NOT EXISTS reminder_created_lookup ON events(
                    json_extract(payload,'$.scope.umo'),
                    json_extract(payload,'$.data.reminder_id'),
                    created_at)
                    WHERE json_extract(payload,'$.data.effect_kind')='reminder.created';
                CREATE INDEX IF NOT EXISTS reminder_created_at ON events(created_at)
                    WHERE json_extract(payload,'$.data.effect_kind')='reminder.created';
                CREATE INDEX IF NOT EXISTS bundle_scope_capture ON bundles(umo,captured_at DESC);
                CREATE INDEX IF NOT EXISTS bundle_turn_capture ON bundles(turn_id,captured_at);
                CREATE INDEX IF NOT EXISTS bundle_archive_only_captured ON bundles(captured_at)
                    WHERE json_extract(payload,'$.archive_only')=1;
                CREATE INDEX IF NOT EXISTS attempt_scope_started ON attempts(umo,started_at);
                CREATE INDEX IF NOT EXISTS attempt_status_ts ON attempts(status, COALESCE(finished_at, started_at));
                CREATE INDEX IF NOT EXISTS intent_status_attempted ON intents(status, COALESCE(attempted_at, 0));
                CREATE INDEX IF NOT EXISTS intent_scope_umo ON intents(json_extract(payload,'$.scope.umo'));
                CREATE INDEX IF NOT EXISTS character_blob_refs_fp ON character_blob_refs(fingerprint);
            """)

    def has_maintenance_backlog(self) -> bool:
        """Fast O(1) check whether historical rows or reclaimable storage remain."""
        wal_bytes = 0
        try:
            wal_bytes = Path(str(self.path) + "-wal").stat().st_size
        except FileNotFoundError:
            wal_bytes = 0
        if wal_bytes >= self.DEFAULT_WAL_PASSIVE_THRESHOLD_BYTES:
            return True
        with self._tx() as db:
            for table in (
                "bundles",
                "context_archive",
                "character_blobs",
                "bundle_compaction_queue",
                "events",
                "attempts",
                "intents",
                "intent_retries",
            ):
                if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                    return True
            try:
                av = int(db.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
                fl = int(db.execute("PRAGMA freelist_count").fetchone()[0] or 0)
                if av == 2 and fl > 0:
                    return True
            except sqlite3.DatabaseError:
                pass
        return False

    def enqueue(self, event: dict, recipients: tuple[str, ...], now: float):
        """Persist a notification once, without reopening completed deliveries.

        Args:
            event: Source-attributed event envelope.
            recipients: Named public API providers to notify.
            now: Civil timestamp.
        """
        with self._tx(write=True) as db:
            db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                       (event["event_id"], json.dumps(event, ensure_ascii=False),
                        json.dumps(list(recipients)), now))

    def pending(self, limit=100):
        with self._tx() as db:
            return [(json.loads(row["payload"]), json.loads(row["recipients"]))
                    for row in db.execute("SELECT * FROM events WHERE recipients!='[]' ORDER BY created_at LIMIT ?",
                                          (limit,))]

    def pending_for(self, recipient, limit=100):
        """Read an independent recipient queue, keeping original event order."""
        with self._tx() as db:
            return [json.loads(row["payload"]) for row in db.execute(
                "SELECT payload FROM events WHERE recipients!='[]' AND EXISTS "
                "(SELECT 1 FROM json_each(recipients) WHERE value=?) ORDER BY created_at LIMIT ?",
                (recipient, limit))]

    def pending_recipients(self):
        with self._tx() as db:
            return [row[0] for row in db.execute(
                "SELECT DISTINCT j.value FROM events e, json_each(e.recipients) j WHERE e.recipients!='[]'")]

    def _dedupe_character_blob(
        self,
        db: sqlite3.Connection,
        data: dict,
        *,
        bundle_id: str = "",
        used_at: float | None = None,
    ) -> bytes:
        """Store character payload once in character_blobs and return archived bundle bytes."""
        character = data.get("character")
        if isinstance(character, dict) and character:
            char_bytes = json.dumps(
                character, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            content_hash = hashlib.sha256(char_bytes).hexdigest()
            raw_fp = str(character.get("fingerprint") or "").strip()
            blob_key = raw_fp or content_hash
            ts = float(used_at) if used_at is not None else time.time()
            existing = db.execute(
                "SELECT payload FROM character_blobs WHERE fingerprint=?", (blob_key,)
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO character_blobs(fingerprint, payload, last_used_at) VALUES (?,?,?) "
                    "ON CONFLICT(fingerprint) DO UPDATE SET last_used_at=MAX(COALESCE(last_used_at,0), excluded.last_used_at)",
                    (blob_key, gzip.compress(char_bytes, compresslevel=3), ts),
                )
            else:
                try:
                    existing_bytes = gzip.decompress(existing["payload"])
                except Exception:
                    existing_bytes = b""
                if existing_bytes != char_bytes:
                    blob_key = f"{raw_fp}:{content_hash[:16]}" if raw_fp else content_hash
                    db.execute(
                        "INSERT INTO character_blobs(fingerprint, payload, last_used_at) VALUES (?,?,?) "
                        "ON CONFLICT(fingerprint) DO UPDATE SET last_used_at=MAX(COALESCE(last_used_at,0), excluded.last_used_at)",
                        (blob_key, gzip.compress(char_bytes, compresslevel=3), ts),
                    )
                else:
                    db.execute(
                        "UPDATE character_blobs SET last_used_at=MAX(COALESCE(last_used_at,0), ?) WHERE fingerprint=?",
                        (ts, blob_key),
                    )
            if bundle_id:
                db.execute(
                    "INSERT OR REPLACE INTO character_blob_refs(bundle_id, fingerprint) VALUES (?,?)",
                    (bundle_id, blob_key),
                )
            deduped = {k: v for k, v in data.items() if k != "character"}
            deduped["character_blob_ref"] = blob_key
            return json.dumps(deduped, ensure_ascii=False).encode("utf-8")
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def save_bundle(self, bundle_id: str, turn_id: str, umo: str, payload: str, captured: float):
        from .narrative_projection import reply_material
        data = json.loads(payload)
        summary = dict(reply_material(data), archive_id=bundle_id,
                       character_fingerprint=data.get('character', {}).get('fingerprint'),
                       completed=data.get('completed', {}), availability=data.get('availability', {}))
        with self._tx(write=True) as db:
            archived_raw = self._dedupe_character_blob(db, data, bundle_id=bundle_id, used_at=captured)
            db.execute("INSERT OR IGNORE INTO context_archive VALUES (?,?,?)",
                       (bundle_id, hashlib.sha256(archived_raw).hexdigest(),
                        gzip.compress(archived_raw, compresslevel=3)))
            inserted = db.execute("INSERT OR IGNORE INTO bundles VALUES (?,?,?,?,?)",
                                  (bundle_id, turn_id, umo, json.dumps(summary, ensure_ascii=False), captured))
            if inserted.rowcount:
                db.execute("INSERT OR IGNORE INTO bundle_compaction_queue VALUES (?,?)", (bundle_id, captured))

    def record_turn_diagnostic(self, *, scope, turn_id, bundle_id, character_fingerprint,
                               projection, plan, material_count, material_char_count, now):
        """Write an idempotent, body-free provider projection receipt."""
        actions = {"ignore", "acknowledge", "clarify", "reply", "close"}
        states = {"observing", "casual", "focused", "supportive", "playful", "disengaging"}
        lengths = {"brief", "normal", "detailed"}
        bypass_codes = {"empty_round", "round_roles_unsupported", "incomplete_round",
                        "content_shape_unknown", "message_fields_unknown", "message_extra_content_unknown",
                        "content_part_unknown", "content_part_not_text", "think_fields_unknown",
                        "user_think_part_unsupported", "signed_chat_transport_unconfirmed",
                        "assistant_text_missing", "user_text_missing", "raw_to_context_identity_mismatch",
                        "source_message_type_unsupported"}
        safe_plan = {}
        if isinstance(plan, dict):
            if plan.get("action") in actions:
                safe_plan["action"] = plan["action"]
            if plan.get("state") in states:
                safe_plan["state"] = plan["state"]
            if plan.get("length") in lengths:
                safe_plan["length"] = plan["length"]
            reason = plan.get("reason_code")
            if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", reason):
                safe_plan["reason_code"] = reason
            if (plan.get("response_goal_source") == "program" and
                    isinstance(plan.get("response_goal"), str) and len(plan["response_goal"]) <= 600):
                safe_plan["response_goal"] = plan["response_goal"]
                safe_plan["response_goal_source"] = "program"
            else:
                goal_hash = plan.get("response_goal_sha256")
                goal_length = plan.get("response_goal_length")
                if isinstance(goal_hash, str) and re.fullmatch(r"[0-9a-f]{64}", goal_hash):
                    safe_plan["response_goal_sha256"] = goal_hash
                if type(goal_length) is int and 0 <= goal_length <= 600:
                    safe_plan["response_goal_length"] = goal_length
                safe_plan["response_goal_source"] = "unverified"
        reasons = (projection or {}).get("bypass_reasons", {})
        if isinstance(reasons, dict):
            safe_reasons = {str(key): max(0, int(value)) for key, value in list(reasons.items())[:16]
                            if key in bypass_codes and type(value) is int}
        else:
            safe_reasons = {}
        boundary_reason = str((projection or {}).get("boundary_reason", ""))
        boundary_codes = {"", "contexts_not_list", "runner_history_unavailable", "request_identity_lost",
                          "raw_message_shape_unknown", "sanitizer_unavailable", "sanitized_prefix_not_list",
                          "sanitized_message_not_list", "sanitizer_alignment_failed", "provider_prefix_mismatch",
                          "boundary_unresolved", "boundary_missing", "boundary_descriptor_invalid",
                          "boundary_spans_invalid", "boundary_spans_incomplete", "source_message_type_unsupported"}
        if boundary_reason not in boundary_codes and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", boundary_reason):
            boundary_reason = "unknown_boundary_error"
        provider_hash = (projection or {}).get("provider_input_sha256")
        if not isinstance(provider_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", provider_hash):
            provider_hash = ""
        source_hash = (projection or {}).get("source_ids_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            source_hash = ""
        memory_status = (projection or {}).get("memory_source_status", {})
        if not isinstance(memory_status, dict):
            memory_status = {}
        status_values = {"ready", "empty", "pending", "partial", "deferred", "invalid", "unavailable",
                         "timeout", "running", "complete", "failed"}
        safe_memory_status = {key: value for key, value in memory_status.items()
                              if key in {"livingmemory_evidence_status", "livingmemory_evidence_stale", "livingmemory_recall_status",
                                         "self_learning_relationship_restore_status", "self_learning_partial"}
                              and (value is True or value in status_values)}
        stage_counts = memory_status.get("livingmemory_projection_stage_counts")
        if isinstance(stage_counts, dict):
            safe_stage_counts = {}
            for key in ("raw_recall_view_atoms", "canonical_merged_atoms", "projected_lm_context_items"):
                value = stage_counts.get(key)
                if value is None:
                    safe_stage_counts[key] = None
                elif type(value) is int and 0 <= value <= 100000:
                    safe_stage_counts[key] = value
            if safe_stage_counts:
                safe_memory_status["livingmemory_projection_stage_counts"] = safe_stage_counts
        member_filter = memory_status.get("member_boundary_filter")
        if isinstance(member_filter, dict):
            safe_memory_status["member_boundary_filter"] = {
                "state": member_filter.get("state") if member_filter.get("state") in {"filtered", "bypass"} else "unknown",
                "reason": member_filter.get("reason") if isinstance(member_filter.get("reason"), str) and
                          re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", member_filter["reason"]) else "unknown",
                "removed": member_filter.get("removed") if type(member_filter.get("removed")) is int and
                          0 <= member_filter["removed"] <= 100000 else 0,
            }
        lm_recall = memory_status.get("livingmemory_recall_diagnostics")
        if isinstance(lm_recall, dict):
            safe_lm_recall = {}
            if type(lm_recall.get("baseline_preserved")) is bool:
                safe_lm_recall["baseline_preserved"] = lm_recall["baseline_preserved"]
            elapsed = lm_recall.get("elapsed_ms")
            if type(elapsed) is int and 0 <= elapsed <= 120000:
                safe_lm_recall["elapsed_ms"] = elapsed
            inflight = lm_recall.get("inflight_count")
            if type(inflight) is int and 0 <= inflight <= 1024:
                safe_lm_recall["inflight_count"] = inflight
            fast_path = lm_recall.get("local_reply_fast_path")
            if isinstance(fast_path, bool):
                safe_lm_recall["local_reply_fast_path"] = fast_path
            route_names = {"scope_auth", "history_legacy", "history_managed", "narrative_events",
                           "narrative_sources", "candidate_recheck", "evidence_view",
                           "ready_baseline", "semantic_legacy", "semantic_managed", "revision_peek",
                           "scope_memory_policy", "scope_session", "scope_plugin", "scope_config", "baseline_recheck"}
            route_states = {"timeout", "error", "busy", "closed", "unavailable"}
            route_elapsed = lm_recall.get("route_elapsed_ms")
            if isinstance(route_elapsed, dict):
                kept = {}
                for key, value in route_elapsed.items():
                    if key in route_names and type(value) is int and 0 <= value <= 120000:
                        kept[key] = value
                        if len(kept) >= 20:
                            break
                if kept:
                    safe_lm_recall["route_elapsed_ms"] = kept
            route_failures = lm_recall.get("route_failures")
            if isinstance(route_failures, dict):
                kept = {}
                for key, value in route_failures.items():
                    if key in route_names and isinstance(value, str) and value in route_states:
                        kept[key] = value
                        if len(kept) >= 20:
                            break
                if kept:
                    safe_lm_recall["route_failures"] = kept
            if safe_lm_recall:
                safe_memory_status["livingmemory_recall_diagnostics"] = safe_lm_recall
        data = {
            "turn_id": str(turn_id)[:80], "bundle_id": str(bundle_id)[:80],
            "character_fingerprint": str(character_fingerprint or "")[:128],
            "projection_version": str((projection or {}).get("version", ""))[:80],
            "boundary_state": str((projection or {}).get("boundary_state", "bypass"))[:40],
            "boundary_reason": boundary_reason,
            "converted_rounds": max(0, int((projection or {}).get("converted_rounds", 0))),
            "bypassed_rounds": max(0, int((projection or {}).get("bypassed_rounds", 0))),
            "bypass_reasons": safe_reasons,
            "material_count": min(1000000, max(0, int(material_count))),
            "material_char_count": min(100000000, max(0, int(material_char_count))),
            "provider_input_sha256": provider_hash,
            "provider_context_count": min(100000, max(0, int((projection or {}).get("provider_context_count", 0))))
                if type((projection or {}).get("provider_context_count", 0)) is int else 0,
            "lm_kept_context_items": min(100000, max(0, int((projection or {}).get("lm_kept_context_items", 0))))
                if type((projection or {}).get("lm_kept_context_items")) is int else None,
            "lm_kept_atoms": min(100000, max(0, int((projection or {}).get("lm_kept_atoms", 0))))
                if type((projection or {}).get("lm_kept_atoms")) is int else None,
            "lm_kept_characters": min(100000000, max(0, int((projection or {}).get("lm_kept_characters", 0))))
                if type((projection or {}).get("lm_kept_characters")) is int else None,
            "lm_measurement_stage": (projection or {}).get("lm_measurement_stage")
                if isinstance((projection or {}).get("lm_measurement_stage"), str)
                and (projection or {}).get("lm_measurement_stage") in {
                    "provider_kwargs_after_boundaries", "unknown", "unavailable"
                } else "unavailable",
            "lm_measurement_unknown_reason": (projection or {}).get("lm_measurement_unknown_reason")
                if isinstance((projection or {}).get("lm_measurement_unknown_reason"), str)
                and (projection or {}).get("lm_measurement_unknown_reason") in {
                    "none", "provenance_unavailable", "provenance_invalid", "context_shape_unknown",
                    "unsupported_part_type", "controlled_json_malformed", "fingerprint_mismatch",
                    "provenance_counts_invalid", "controlled_payload_unrecognized",
                } else "provenance_unavailable",
            "lm_measurement_part_counts": {
                key: min(10000, max(0, int(value)))
                for key, value in ((projection or {}).get("lm_measurement_part_counts") or {}).items()
                if key in {"text", "think", "image_url", "audio_url", "other_known_nontext", "unrecognized"}
                and type(value) is int and 0 <= value <= 10000
            } if isinstance((projection or {}).get("lm_measurement_part_counts"), dict) else {},
            "lm_measurement_prefix_count": min(10000, max(0, int((projection or {}).get("lm_measurement_prefix_count"))))
                if type((projection or {}).get("lm_measurement_prefix_count")) is int
                and 0 <= (projection or {}).get("lm_measurement_prefix_count") <= 10000 else 0,
            "lm_measurement_json_span_count": min(10000, max(0, int((projection or {}).get("lm_measurement_json_span_count"))))
                if type((projection or {}).get("lm_measurement_json_span_count")) is int
                and 0 <= (projection or {}).get("lm_measurement_json_span_count") <= 10000 else 0,
            "lm_measurement_hash_match_count": min(10000, max(0, int((projection or {}).get("lm_measurement_hash_match_count"))))
                if type((projection or {}).get("lm_measurement_hash_match_count")) is int
                and 0 <= (projection or {}).get("lm_measurement_hash_match_count") <= 10000 else 0,
            "source_ids_sha256": source_hash,
            "source_id_count": min(100000, max(0, int((projection or {}).get("source_id_count", 0))))
                if type((projection or {}).get("source_id_count", 0)) is int else 0,
            "text_ref_extracted": min(100000, max(0, int((projection or {}).get("text_ref_extracted", 0))))
                if type((projection or {}).get("text_ref_extracted", 0)) is int else 0,
            "text_part_ref_extracted": min(100000, max(0, int((projection or {}).get("text_part_ref_extracted", 0))))
                if type((projection or {}).get("text_part_ref_extracted", 0)) is int else 0,
            "mixed_part_ref_extracted": min(100000, max(0, int((projection or {}).get("mixed_part_ref_extracted", 0))))
                if type((projection or {}).get("mixed_part_ref_extracted", 0)) is int else 0,
            "round_preserved": min(100000, max(0, int((projection or {}).get("round_preserved", 0))))
                if type((projection or {}).get("round_preserved", 0)) is int else 0,
            "round_converted": min(100000, max(0, int((projection or {}).get("round_converted", 0))))
                if type((projection or {}).get("round_converted", 0)) is int else 0,
            "memory_source_status": safe_memory_status,
            "plan": safe_plan,
        }
        adaptation_stage=(projection or {}).get('reply_adaptation_stage')
        adaptation_counts=(projection or {}).get('reply_adaptation_counts')
        if isinstance(adaptation_stage,str) and adaptation_stage in {'provider_kwargs_after_boundaries','unavailable','not_present'}:
            data['reply_adaptation_stage']=adaptation_stage
        if isinstance(adaptation_counts,dict):
            safe={k:v for k,v in adaptation_counts.items() if k in {'profiles','memory_refs','dialogue','rhythm'}
                  and type(v) is int and 0<=v<=100}
            if type(adaptation_counts.get('personality_locked')) is bool:
                safe['personality_locked']=adaptation_counts['personality_locked']
            data['reply_adaptation_counts']=safe
        event = {"schema_version": 1, "event_id": "diagnostic:" + str(bundle_id),
                 "type": "decision.diagnostic", "occurred_at": now, "received_at": now,
                 "scope": scope, "actor_id": "bot", "text": "", "source_ids": [str(bundle_id)],
                 "fact_kind": "diagnostic", "data": data}
        self.enqueue(event, (), now)

    def full_bundle(self, bundle_id):
        """Read complete local evidence, checking the archive before returning it."""
        with self._tx() as db:
            row = db.execute('SELECT * FROM context_archive WHERE bundle_id=?', (bundle_id,)).fetchone()
            if row:
                raw = gzip.decompress(row['payload'])
                if hashlib.sha256(raw).hexdigest() != row['sha256']:
                    raise ValueError('context archive checksum mismatch')
                data = json.loads(raw)
                if isinstance(data, dict) and 'character_blob_ref' in data and 'character' not in data:
                    ref = str(data.pop('character_blob_ref') or '')
                    if ref:
                        blob = db.execute(
                            'SELECT payload FROM character_blobs WHERE fingerprint=?', (ref,)
                        ).fetchone()
                        if blob is not None:
                            data['character'] = json.loads(gzip.decompress(blob['payload']))
                return data
            row = db.execute('SELECT payload FROM bundles WHERE bundle_id=?', (bundle_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def storage_status(self):
        """Cheap capacity facts; do not scan message bodies or databases for a UI refresh."""
        def size(path):
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0
        free = shutil.disk_usage(self.path.parent).free
        auto_vacuum_mode = 0
        freelist_pages = 0
        page_size = 4096
        with self._tx() as db:
            try:
                auto_vacuum_mode = int(db.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
                freelist_pages = int(db.execute("PRAGMA freelist_count").fetchone()[0] or 0)
                page_size = int(db.execute("PRAGMA page_size").fetchone()[0] or 4096)
            except sqlite3.DatabaseError:
                pass
        return {
            "database_bytes": size(self.path),
            "wal_bytes": size(Path(str(self.path) + "-wal")),
            "legacy_database_bytes": size(self.path.parent / "decision-learning.sqlite3"),
            "free_bytes": free,
            "low_space": free < 5 * 1024 ** 3,
            "auto_vacuum_mode": auto_vacuum_mode,
            "auto_vacuum_incremental": auto_vacuum_mode == 2,
            "freelist_pages": freelist_pages,
            "reclaimable_bytes": freelist_pages * page_size,
            "retention_policy": "compact_online_keep_verified_archive_and_receipts",
        }

    def maintain(
        self,
        now,
        retention_days=7,
        limit=40,
        *,
        archive_retention_days=DEFAULT_ARCHIVE_RETENTION_DAYS,
        max_archive_rows=DEFAULT_MAX_ARCHIVE_ROWS,
        dead_letter_ttl_days=DEFAULT_DEAD_LETTER_TTL_DAYS,
        reminder_retention_days=DEFAULT_REMINDER_RETENTION_DAYS,
        uncertain_ttl_days=None,
        vacuum_pages=DEFAULT_VACUUM_PAGES,
        checkpoint_interval_seconds=DEFAULT_CHECKPOINT_INTERVAL_SECONDS,
        truncate_interval_seconds=DEFAULT_TRUNCATE_INTERVAL_SECONDS,
        wal_passive_threshold_bytes=DEFAULT_WAL_PASSIVE_THRESHOLD_BYTES,
        wal_truncate_threshold_bytes=DEFAULT_WAL_TRUNCATE_THRESHOLD_BYTES,
    ):
        """Compress legacy bundles in bounded transactions and prune expired terminal logs."""
        from .narrative_projection import reply_material
        cutoff = now - max(1, retention_days) * 86400
        archive_cutoff = now - max(1, archive_retention_days) * 86400
        dead_letter_cutoff = now - max(1, dead_letter_ttl_days) * 86400
        reminder_cutoff = now - max(1, int(reminder_retention_days)) * 86400
        effective_uncertain_days = max(1, int(retention_days if uncertain_ttl_days is None else uncertain_ttl_days))
        uncertain_cutoff = now - effective_uncertain_days * 86400
        batch_limit = max(1, int(limit)) * 5
        pruned_archives = 0
        pruned_bundles = 0
        pruned_character_blobs = 0
        pruned_reminders = 0
        pruned_uncertain_attempts = 0
        pruned_uncertain_intents = 0
        expired_recipients = 0
        with self._tx(write=True) as db:
            cursor = db.execute("SELECT value FROM maintenance WHERE name='bundle_backfill_cursor'").fetchone()
            # Inspect a bounded range even when every legacy row already has its archive.
            # New bundles write their complete archive in the same transaction.
            window = db.execute(
                "SELECT b.rowid AS seq,b.bundle_id,b.captured_at,a.bundle_id AS archived, "
                "json_extract(b.payload,'$.archive_only') AS compacted FROM bundles AS b "
                "LEFT JOIN context_archive AS a ON a.bundle_id=b.bundle_id "
                "WHERE b.rowid>? ORDER BY b.rowid LIMIT ?",
                (int(cursor[0]) if cursor else 0, min(256, max(1, limit) * 4)),
            ).fetchall()
            compressed = 0
            last_seq = None
            for candidate in window:
                if compressed >= limit:
                    break
                last_seq = candidate['seq']
                if candidate['archived'] is not None:
                    if not candidate['compacted']:
                        db.execute("INSERT OR IGNORE INTO bundle_compaction_queue VALUES (?,?)",
                                   (candidate['bundle_id'], candidate['captured_at']))
                    continue
                row = db.execute("SELECT bundle_id,payload FROM bundles WHERE rowid=?", (last_seq,)).fetchone()
                raw = row['payload'].encode('utf-8')
                data = json.loads(raw)
                compact = dict(reply_material(data), archive_id=row['bundle_id'],
                               character_fingerprint=data.get('character', {}).get('fingerprint'),
                               availability=data.get('availability', {}), completed=data.get('completed', {}))
                archived_raw = self._dedupe_character_blob(
                    db, data, bundle_id=row['bundle_id'], used_at=candidate['captured_at']
                )
                db.execute('INSERT OR IGNORE INTO context_archive VALUES (?,?,?)',
                           (row['bundle_id'], hashlib.sha256(archived_raw).hexdigest(),
                            gzip.compress(archived_raw, compresslevel=3)))
                db.execute('UPDATE bundles SET payload=? WHERE bundle_id=?',
                           (json.dumps(compact, ensure_ascii=False), row['bundle_id']))
                db.execute("INSERT OR IGNORE INTO bundle_compaction_queue VALUES (?,?)",
                           (candidate['bundle_id'], candidate['captured_at']))
                compressed += 1
            if last_seq is not None:
                db.execute("INSERT OR REPLACE INTO maintenance(name,value) VALUES('bundle_backfill_cursor',?)",
                           (str(last_seq),))
            old = db.execute("SELECT b.bundle_id,b.payload FROM bundle_compaction_queue AS q "
                             "JOIN bundles AS b ON b.bundle_id=q.bundle_id "
                             "JOIN context_archive AS a ON a.bundle_id=b.bundle_id "
                             "WHERE q.captured_at<? ORDER BY q.captured_at LIMIT ?",
                             (cutoff, limit)).fetchall()
            for row in old:
                brief = json.loads(row['payload'])
                brief = {k:v for k,v in brief.items() if k in
                         {'archive_id','source_scope','character_fingerprint','availability','completed'}}
                brief['archive_only'] = True
                db.execute('UPDATE bundles SET payload=? WHERE bundle_id=?',
                           (json.dumps(brief, ensure_ascii=False), row['bundle_id']))
                db.execute("DELETE FROM bundle_compaction_queue WHERE bundle_id=?", (row['bundle_id'],))
            # Prune expired archive_only bundles and their context_archive rows by TTL (uses partial index).
            expired_rows = db.execute(
                "SELECT bundle_id FROM bundles WHERE captured_at<? "
                "AND json_extract(payload,'$.archive_only')=1 "
                "ORDER BY captured_at ASC LIMIT ?",
                (archive_cutoff, batch_limit),
            ).fetchall()
            if expired_rows:
                expired_ids = [(r['bundle_id'],) for r in expired_rows]
                pruned_archives += db.executemany(
                    "DELETE FROM context_archive WHERE bundle_id=?", expired_ids
                ).rowcount
                db.executemany("DELETE FROM character_blob_refs WHERE bundle_id=?", expired_ids)
                db.executemany("DELETE FROM bundle_compaction_queue WHERE bundle_id=?", expired_ids)
                pruned_bundles += db.executemany("DELETE FROM bundles WHERE bundle_id=?", expired_ids).rowcount
            # Enforce optional hard row cap on context_archive (oldest first).
            if max_archive_rows and max_archive_rows > 0:
                archive_total = int(db.execute("SELECT count(*) FROM context_archive").fetchone()[0] or 0)
                excess = archive_total - int(max_archive_rows)
                if excess > 0:
                    cap_batch = min(excess, batch_limit)
                    capped = db.execute(
                        "SELECT a.bundle_id, COALESCE(json_extract(b.payload,'$.archive_only'),0) AS is_arch "
                        "FROM context_archive AS a LEFT JOIN bundles AS b ON b.bundle_id=a.bundle_id "
                        "ORDER BY COALESCE(b.captured_at, 0) ASC, a.rowid ASC LIMIT ?",
                        (cap_batch,),
                    ).fetchall()
                    if capped:
                        cap_ids = [(r['bundle_id'],) for r in capped]
                        pruned_archives += db.executemany(
                            "DELETE FROM context_archive WHERE bundle_id=?", cap_ids
                        ).rowcount
                        db.executemany("DELETE FROM character_blob_refs WHERE bundle_id=?", cap_ids)
                        arch_only_ids = [(r['bundle_id'],) for r in capped if r['is_arch']]
                        if arch_only_ids:
                            db.executemany("DELETE FROM bundle_compaction_queue WHERE bundle_id=?", arch_only_ids)
                            pruned_bundles += db.executemany(
                                "DELETE FROM bundles WHERE bundle_id=?", arch_only_ids
                            ).rowcount
            # Backfill legacy character_blob_refs in bounded slices, then prune orphaned character_blobs.
            ref_done_row = db.execute(
                "SELECT value FROM maintenance WHERE name='blob_ref_backfill_done'"
            ).fetchone()
            ref_done = bool(ref_done_row and ref_done_row[0] == "1")
            if not ref_done:
                if db.execute("SELECT 1 FROM context_archive LIMIT 1").fetchone() is None:
                    db.execute(
                        "INSERT OR REPLACE INTO maintenance(name,value) VALUES('blob_ref_backfill_done','1')"
                    )
                    ref_done = True
                elif db.execute("SELECT 1 FROM character_blobs LIMIT 1").fetchone() is not None:
                    ref_cur_row = db.execute(
                        "SELECT value FROM maintenance WHERE name='blob_ref_backfill_cursor'"
                    ).fetchone()
                    ref_cursor = int(ref_cur_row[0]) if ref_cur_row else 0
                    ref_batch = min(200, max(int(limit), 200))
                    arch_rows = db.execute(
                        "SELECT a.rowid AS seq, a.bundle_id, a.payload, b.captured_at "
                        "FROM context_archive AS a LEFT JOIN bundles AS b ON b.bundle_id=a.bundle_id "
                        "WHERE a.rowid>? ORDER BY a.rowid LIMIT ?",
                        (ref_cursor, ref_batch),
                    ).fetchall()
                    for arow in arch_rows:
                        try:
                            parsed = json.loads(gzip.decompress(arow["payload"]))
                        except Exception:
                            parsed = None
                        if isinstance(parsed, dict) and parsed.get("character_blob_ref"):
                            fp = str(parsed["character_blob_ref"])
                            db.execute(
                                "INSERT OR REPLACE INTO character_blob_refs(bundle_id, fingerprint) VALUES (?,?)",
                                (arow["bundle_id"], fp),
                            )
                            if arow["captured_at"] is not None:
                                db.execute(
                                    "UPDATE character_blobs SET last_used_at=MAX(COALESCE(last_used_at,0), ?) "
                                    "WHERE fingerprint=?",
                                    (float(arow["captured_at"]), fp),
                                )
                    if arch_rows:
                        db.execute(
                            "INSERT OR REPLACE INTO maintenance(name,value) VALUES('blob_ref_backfill_cursor',?)",
                            (str(arch_rows[-1]["seq"]),),
                        )
                    if len(arch_rows) < ref_batch:
                        db.execute(
                            "INSERT OR REPLACE INTO maintenance(name,value) VALUES('blob_ref_backfill_done','1')"
                        )
                        ref_done = True
            if ref_done:
                # Heal any dangling refs whose archive row was removed externally.
                db.execute(
                    "DELETE FROM character_blob_refs WHERE bundle_id IN ("
                    "SELECT r.bundle_id FROM character_blob_refs AS r "
                    "WHERE NOT EXISTS (SELECT 1 FROM context_archive AS a WHERE a.bundle_id=r.bundle_id) "
                    "LIMIT ?)",
                    (batch_limit,),
                )
                pruned_character_blobs = db.execute(
                    "DELETE FROM character_blobs WHERE fingerprint IN ("
                    "SELECT cb.fingerprint FROM character_blobs AS cb "
                    "WHERE NOT EXISTS (SELECT 1 FROM character_blob_refs AS r WHERE r.fingerprint=cb.fingerprint) "
                    "LIMIT ?)",
                    (batch_limit,),
                ).rowcount
            # Expire stale unacked/dead-letter recipients so events pruning can never stall permanently.
            dead_events = db.execute(
                "SELECT rowid FROM events WHERE recipients!='[]' AND created_at<? ORDER BY created_at ASC LIMIT ?",
                (dead_letter_cutoff, batch_limit),
            ).fetchall()
            if dead_events:
                dead_rowids = [(r['rowid'],) for r in dead_events]
                expired_recipients = db.executemany(
                    "UPDATE events SET recipients='[]' WHERE rowid=?", dead_rowids
                ).rowcount
            # Prune expired completed non-reminder events via partial covering index (no rowid cursor drift).
            expired_done_events = db.execute(
                "SELECT rowid FROM events "
                "WHERE recipients='[]' "
                "AND COALESCE(json_extract(payload,'$.data.effect_kind'),'')!='reminder.created' "
                "AND created_at<? ORDER BY created_at ASC LIMIT ?",
                (cutoff, batch_limit),
            ).fetchall()
            pruned_events = (
                db.executemany(
                    "DELETE FROM events WHERE rowid=?",
                    [(r["rowid"],) for r in expired_done_events],
                ).rowcount
                if expired_done_events
                else 0
            )
            # Long-term retention for reminder.created events (uses reminder_created_at partial index).
            expired_reminders = db.execute(
                "SELECT rowid FROM events "
                "WHERE recipients='[]' "
                "AND json_extract(payload,'$.data.effect_kind')='reminder.created' "
                "AND created_at<? ORDER BY created_at ASC LIMIT ?",
                (reminder_cutoff, batch_limit),
            ).fetchall()
            if expired_reminders:
                rem_rowids = [(r["rowid"],) for r in expired_reminders]
                pruned_reminders = db.executemany(
                    "DELETE FROM events WHERE rowid=?", rem_rowids
                ).rowcount
                pruned_events += pruned_reminders
            # Prune terminal and expired uncertain attempts (uses attempt_status_ts index).
            pruned_attempts = db.execute(
                "DELETE FROM attempts WHERE attempt_id IN ("
                "SELECT attempt_id FROM attempts WHERE status IN ('success','failed','cancelled') "
                "AND COALESCE(finished_at, started_at)<? LIMIT ?)",
                (cutoff, batch_limit),
            ).rowcount
            pruned_uncertain_attempts = db.execute(
                "DELETE FROM attempts WHERE attempt_id IN ("
                "SELECT attempt_id FROM attempts WHERE status='uncertain' "
                "AND COALESCE(finished_at, started_at)<? LIMIT ?)",
                (uncertain_cutoff, batch_limit),
            ).rowcount
            pruned_attempts += pruned_uncertain_attempts
            # Stamp untimed uncertain intents once so they expire cleanly after uncertain_ttl_days.
            untimed_uncertain = db.execute(
                "SELECT intent_id, payload FROM intents "
                "WHERE status='uncertain' AND COALESCE(attempted_at, 0)=0 LIMIT ?",
                (batch_limit,),
            ).fetchall()
            for urow in untimed_uncertain:
                stamped = float(now)
                try:
                    upayload = json.loads(urow["payload"])
                    if isinstance(upayload, dict):
                        for key in ("attempted_at", "created_at", "occurred_at", "due_at"):
                            val = upayload.get(key)
                            if isinstance(val, (int, float)) and val > 0:
                                stamped = float(val)
                                break
                except Exception:
                    pass
                db.execute(
                    "UPDATE intents SET attempted_at=? WHERE intent_id=? AND attempted_at IS NULL",
                    (stamped, urow["intent_id"]),
                )
            # Prune terminal and expired uncertain intents (uses intent_status_attempted index).
            pruned_intents = db.execute(
                "DELETE FROM intents WHERE intent_id IN ("
                "SELECT intent_id FROM intents WHERE status IN ('sent','failed','cancelled','expired') "
                "AND COALESCE(attempted_at, 0)<? LIMIT ?)",
                (cutoff, batch_limit),
            ).rowcount
            pruned_uncertain_intents = db.execute(
                "DELETE FROM intents WHERE intent_id IN ("
                "SELECT intent_id FROM intents WHERE status='uncertain' "
                "AND COALESCE(attempted_at, 0)>0 AND COALESCE(attempted_at, 0)<? LIMIT ?)",
                (uncertain_cutoff, batch_limit),
            ).rowcount
            pruned_intents += pruned_uncertain_intents
            db.execute(
                "DELETE FROM intent_retries WHERE NOT EXISTS "
                "(SELECT 1 FROM intents AS i WHERE i.intent_id=intent_retries.intent_id)"
            )
        # Run bounded incremental vacuum and non-blocking WAL checkpoint outside the write tx.
        pruned_total = (
            pruned_archives
            + pruned_bundles
            + pruned_character_blobs
            + pruned_events
            + pruned_attempts
            + pruned_intents
        )
        vacuumed = False
        checkpointed = False
        checkpoint_mode = "none"
        auto_vacuum_mode = 0
        now_mono = time.monotonic()
        try:
            wal_bytes = Path(str(self.path) + "-wal").stat().st_size
        except FileNotFoundError:
            wal_bytes = 0
        interval_due = (
            self._last_checkpoint_at <= 0.0
            or (now_mono - self._last_checkpoint_at) >= float(checkpoint_interval_seconds or 0.0)
        )
        wal_passive_due = wal_bytes >= int(wal_passive_threshold_bytes or 0) > 0
        wal_truncate_due = (
            wal_bytes >= int(wal_truncate_threshold_bytes or 0) > 0
            and (
                self._last_truncate_at <= 0.0
                or (now_mono - self._last_truncate_at) >= float(truncate_interval_seconds or 0.0)
            )
        )
        should_checkpoint = interval_due or wal_passive_due or wal_truncate_due
        if pruned_total > 0 or should_checkpoint:
            with self._tx(write=False) as db:
                try:
                    auto_vacuum_mode = int(db.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
                except sqlite3.DatabaseError:
                    auto_vacuum_mode = 0
                if auto_vacuum_mode == 2 and vacuum_pages and int(vacuum_pages) > 0:
                    try:
                        freelist = int(db.execute("PRAGMA freelist_count").fetchone()[0] or 0)
                    except sqlite3.DatabaseError:
                        freelist = 0
                    if freelist > 0 or pruned_total > 0:
                        pages_to_reclaim = (
                            min(int(vacuum_pages), freelist) if freelist > 0 else int(vacuum_pages)
                        )
                        try:
                            db.execute(f"PRAGMA incremental_vacuum({pages_to_reclaim})")
                            vacuumed = True
                        except sqlite3.DatabaseError:
                            pass
                if should_checkpoint:
                    if wal_truncate_due:
                        try:
                            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            self._last_checkpoint_at = now_mono
                            self._last_truncate_at = now_mono
                            checkpointed = True
                            checkpoint_mode = "TRUNCATE"
                        except sqlite3.DatabaseError:
                            pass
                    else:
                        try:
                            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                            self._last_checkpoint_at = now_mono
                            checkpointed = True
                            checkpoint_mode = "PASSIVE"
                        except sqlite3.DatabaseError:
                            pass
        return {
            'compressed': compressed,
            'archived': len(old),
            'pruned_archives': pruned_archives,
            'pruned_bundles': pruned_bundles,
            'pruned_character_blobs': pruned_character_blobs,
            'expired_recipients': expired_recipients,
            'pruned_events': pruned_events,
            'pruned_reminders': pruned_reminders,
            'pruned_attempts': pruned_attempts,
            'pruned_uncertain_attempts': pruned_uncertain_attempts,
            'pruned_intents': pruned_intents,
            'pruned_uncertain_intents': pruned_uncertain_intents,
            'vacuumed': vacuumed,
            'auto_vacuum_mode': auto_vacuum_mode,
            'checkpointed': checkpointed,
            'checkpoint_mode': checkpoint_mode,
        }

    def backfill_portraits(self, limit=100):
        """Queue public replay once per source; no companion database access."""
        with self._tx(write=True) as db:
            last = db.execute("SELECT value FROM maintenance WHERE name='portraits_cursor'").fetchone()
            cursor = int(last[0]) if last else 0
            rows = db.execute("SELECT rowid,payload,recipients FROM events WHERE rowid>? ORDER BY rowid LIMIT ?",
                              (cursor, limit)).fetchall()
            for row in rows:
                event = json.loads(row['payload'])
                if event.get('type') == 'message.received':
                    recipients = json.loads(row['recipients'])
                    if 'portrayal' not in recipients:
                        recipients.append('portrayal')
                        db.execute('UPDATE events SET recipients=? WHERE rowid=?',
                                   (json.dumps(recipients), row['rowid']))
            if rows:
                db.execute("INSERT INTO maintenance VALUES('portraits_cursor',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                           (str(rows[-1]['rowid']),))

    def ack(self, event_id: str, recipient: str):
        with self._tx(write=True) as db:
            row = db.execute("SELECT recipients FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row:
                remaining = [name for name in json.loads(row[0]) if name != recipient]
                db.execute("UPDATE events SET recipients=? WHERE event_id=?", (json.dumps(remaining), event_id))

    def begin(self, scope: dict, turn_id: str, segment: int, now: float, intent_id=None):
        """Write the sending state before invoking the platform.

        Args:
            scope: Resolved role and conversation scope.
            turn_id: Immutable interaction identifier.
            segment: Outgoing fragment index.
            now: Civil timestamp.
            intent_id: Optional proactive intention identifier.

        Returns:
            The new attempt identifier.
        """
        attempt = uuid.uuid4().hex
        with self._tx(write=True) as db:
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,'sending',?,NULL,NULL)",
                       (attempt, scope["umo"], turn_id, intent_id, segment, now))
            if intent_id:
                db.execute("UPDATE intents SET status='sending',attempted_at=? WHERE intent_id=?", (now, intent_id))
        return attempt

    def finish(self, attempt: str, status: str, now: float, message_id=None, event=None, recipients=()):
        with self._tx(write=True) as db:
            prior = db.execute('SELECT status FROM attempts WHERE attempt_id=?', (attempt,)).fetchone()
            if prior is not None and prior['status'] == 'success' and status != 'success':
                return
            db.execute("UPDATE attempts SET status=?,finished_at=?,message_id=? WHERE attempt_id=?",
                       (status, now, message_id, attempt))
            if event is not None:
                db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)", (event["event_id"],
                           json.dumps(event, ensure_ascii=False), json.dumps(list(recipients)), now))

    def add_intent(self, intent: dict):
        with self._tx(write=True) as db:
            changed = db.execute("INSERT OR IGNORE INTO intents VALUES (?,?,'pending',NULL)",
                                 (intent["intent_id"], json.dumps(intent, ensure_ascii=False))).rowcount
        return bool(changed)

    def needs_auto_vacuum_migration(self, *, min_freelist_pages: int = 4096) -> bool:
        """Return True when legacy DB uses auto_vacuum!=2 with large freelist and >=2x free disk."""
        try:
            db_size = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            db_size = 0
        try:
            free_bytes = shutil.disk_usage(self.path.parent).free
        except OSError:
            return False
        if free_bytes < max(db_size * 2, 1):
            return False
        with self._tx() as db:
            try:
                av = int(db.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
                freelist = int(db.execute("PRAGMA freelist_count").fetchone()[0] or 0)
            except sqlite3.DatabaseError:
                return False
        return av != 2 and freelist > int(min_freelist_pages)

    def enable_incremental_vacuum_if_needed(self, *, min_freelist_pages: int = 4096) -> dict:
        """One-time safe VACUUM migration to auto_vacuum=INCREMENTAL when freelist & disk allow."""
        try:
            db_size = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            db_size = 0
        try:
            free_bytes = shutil.disk_usage(self.path.parent).free
        except OSError:
            return {"migrated": False, "reason": "disk_usage_unavailable"}
        with self._conn_lock:
            db = self._shared_connection()
            try:
                av = int(db.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
                freelist = int(db.execute("PRAGMA freelist_count").fetchone()[0] or 0)
            except sqlite3.DatabaseError as exc:
                return {"migrated": False, "reason": f"pragma_error:{type(exc).__name__}"}
            if av == 2:
                return {"migrated": False, "reason": "already_incremental", "auto_vacuum": av}
            if freelist <= int(min_freelist_pages):
                return {
                    "migrated": False,
                    "reason": "freelist_below_threshold",
                    "auto_vacuum": av,
                    "freelist_pages": freelist,
                }
            if free_bytes < max(db_size * 2, 1):
                return {
                    "migrated": False,
                    "reason": "insufficient_disk_space",
                    "auto_vacuum": av,
                    "freelist_pages": freelist,
                    "free_bytes": free_bytes,
                    "db_bytes": db_size,
                }
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA auto_vacuum=INCREMENTAL")
                db.execute("VACUUM")
                new_av = int(db.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
                new_size = self.path.stat().st_size if self.path.exists() else 0
            except sqlite3.DatabaseError as exc:
                return {"migrated": False, "reason": f"vacuum_error:{type(exc).__name__}"}
            return {
                "migrated": new_av == 2,
                "auto_vacuum": new_av,
                "freelist_before": freelist,
                "db_bytes_before": db_size,
                "db_bytes_after": new_size,
            }

    def intents(self, *, active_only: bool = False, umo: str | None = None):
        clauses = []
        params = []
        if active_only:
            clauses.append("status IN ('pending','sending','uncertain','failed')")
        if umo:
            clauses.append("json_extract(payload,'$.scope.umo')=?")
            params.append(umo)
        sql = "SELECT payload, status FROM intents"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._tx() as db:
            return [(json.loads(row["payload"]), row["status"]) for row in db.execute(sql, params)]

    def intent_status(self, intent_id: str, status: str):
        with self._tx(write=True) as db:
            db.execute("UPDATE intents SET status=? WHERE intent_id=?", (status, intent_id))

    def intent_retry_ready(self, intent_id: str, now: float) -> bool:
        with self._tx() as db:
            row = db.execute('SELECT next_retry FROM intent_retries WHERE intent_id=?', (intent_id,)).fetchone()
            return row is None or row[0] <= now

    def record_intent_failure(self, intent_id: str, now: float, error_code: str):
        """Retry only pre-send failures; never reopen sent/claimed uncertainty."""
        with self._tx(write=True, immediate=True) as db:
            current = db.execute('SELECT status FROM intents WHERE intent_id=?', (intent_id,)).fetchone()
            if current is None or current[0] != 'pending':
                return None
            row = db.execute('SELECT attempts FROM intent_retries WHERE intent_id=?', (intent_id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            delay = (60, 120, 300, 900, 1800, 3600)[min(attempts - 1, 5)]
            # Fixed exception class names only, never provider/user error text.
            code = error_code if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', error_code) else 'GenerationError'
            db.execute('INSERT INTO intent_retries VALUES(?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET '
                       'attempts=excluded.attempts,next_retry=excluded.next_retry,error_code=excluded.error_code',
                       (intent_id, attempts, now + delay, code))
            exhausted = attempts >= 6
            if exhausted:
                db.execute("UPDATE intents SET status='failed' WHERE intent_id=? AND status='pending'", (intent_id,))
            return {'attempts': attempts, 'next_retry': now + delay, 'exhausted': exhausted}

    def reminder_scope(self, umo, reminder_id):
        with self._tx() as db:
            row = db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.scope.umo')=? "
                             "AND json_extract(payload,'$.data.reminder_id')=? "
                             "AND json_extract(payload,'$.data.effect_kind')='reminder.created' LIMIT 1",
                             (umo, reminder_id)).fetchone()
            return json.loads(row[0])['scope'] if row else None

    def delivered_reply(self, scope, message_id):
        if not message_id:
            return None
        with self._tx() as db:
            rows = db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.type')='delivery.success' "
                "AND json_extract(payload,'$.scope.umo')=? AND json_extract(payload,'$.data.message_id')=? "
                "ORDER BY created_at DESC LIMIT 4", (scope['umo'], str(message_id)))
            for row in rows:
                event = json.loads(row[0])
                if all(event['scope'].get(k) == scope.get(k) for k in ('platform','umo','persona_id','instance_id')):
                    return event
        return None

    def failure_context(self, scope, speaker_id, now):
        """Bound confirmed system notices to the same scope and recipient."""
        from .reply_failure import unavailable_reply
        notices={unavailable_reply('provider_content_blocked'),unavailable_reply('provider_reply_failed')}
        with self._tx() as db:
            rows=db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.type')='delivery.success' "
                "AND json_extract(payload,'$.scope.umo')=? AND created_at>=? ORDER BY created_at DESC LIMIT 40",
                (scope['umo'],now-21600)).fetchall()
        result=[]
        for row in rows:
            event=json.loads(row[0])
            data=event.get('data',{})
            if not all(event.get('scope',{}).get(k)==scope.get(k) for k in ('platform','umo','persona_id','instance_id')):
                continue
            if data.get('recipient_user_id') != speaker_id:
                continue
            if data.get('message_origin')!='system_fallback' and event.get('text') not in notices:
                continue
            result.append({'fact_kind':'receipt','source_id':event['event_id'],'occurred_at':event['occurred_at'],
                'text':'此前该用户收到了一条回复生成未完成的系统提示；这不证明角色拒绝交流、手滑误发或用户做错事。仅解释对应提示时使用，不在无关话题复述。',
                'failure_code':data.get('generation_failure','generation_unavailable')})
            if len(result)==2:
                break
        return result

    def expression_context(self, scope):
        """Observed syntax of confirmed replies in this exact character scope."""
        with self._tx() as db:
            rows = db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.type')='delivery.success' "
                "AND json_extract(payload,'$.scope.umo')=? ORDER BY created_at DESC LIMIT 20", (scope['umo'],)).fetchall()
        events=[]
        for row in rows:
            event=json.loads(row[0])
            if all(event.get('scope',{}).get(k)==scope.get(k) for k in ('platform','umo','persona_id','instance_id')) and event.get('text'):
                events.append(event)
            if len(events)==6:
                break
        count=sum(str(e['text']).lstrip().startswith(('……','...')) for e in events)
        result={'fact_kind':'observation','recent_replies':len(events),'ellipsis_prefixes':count,
                'source_ids':[e['event_id'] for e in events]}
        from .rest_behavior import rest_announcements
        result['rest_announcement_ids'] = rest_announcements(events, time.time())
        if count >= 4:
            result['guidance']='最近多轮用了相同的省略号起句；这是过去表达，不是本轮要求。先回应具体内容，可以直接开口，不必复制旧起句。'
        return result

    def quota(self, umo: str, day_start: float):
        with self._tx() as db:
            rows = db.execute("SELECT started_at FROM attempts WHERE umo=? AND intent_id IS NOT NULL "
                              "AND status IN ('success','uncertain','sending') AND started_at>=? ORDER BY started_at DESC",
                              (umo, day_start)).fetchall()
        return len(rows), (rows[0][0] if rows else None)

    def snapshot(self, umo: str):
        with self._tx() as db:
            attempts = [dict(row) for row in db.execute("SELECT * FROM attempts WHERE umo=? ORDER BY started_at DESC LIMIT 80", (umo,))]
            pending = db.execute("SELECT count(*) FROM events WHERE recipients!='[]' AND json_extract(payload,'$.scope.umo')=?", (umo,)).fetchone()[0]
            contexts = [dict(row) for row in db.execute(
                "SELECT bundle_id,turn_id,captured_at,length(payload) AS online_characters FROM bundles WHERE umo=? ORDER BY captured_at DESC LIMIT 10", (umo,))]
        return {"receipts": attempts, "pending_notifications": pending,
                "contexts": contexts,
                "intents": [dict(payload, delivery_status=status) for payload, status in self.intents(umo=umo)]}
