"""Durable outgoing attempts and idempotent cross-plugin event delivery."""

from __future__ import annotations

import json
import sqlite3
import uuid
import gzip
import hashlib
import re
import shutil
import time
from contextlib import closing
from pathlib import Path


class NarrativeJournal:
    """Keep platform uncertainty separate from retryable plugin notifications."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
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
                CREATE TABLE IF NOT EXISTS bundles (
                    bundle_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                    umo TEXT NOT NULL, payload TEXT NOT NULL, captured_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS context_archive (
                    bundle_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS maintenance (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS delivered_message_lookup ON events(
                    json_extract(payload,'$.scope.umo'),json_extract(payload,'$.data.message_id'))
                    WHERE json_extract(payload,'$.type')='delivery.success';
            """)
            # A crashed process cannot prove whether a platform accepted a send.
            db.execute("UPDATE attempts SET status='uncertain' WHERE status='sending'")
            db.execute("UPDATE intents SET status='uncertain' WHERE status='sending'")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def ensure_indexes(self):
        """Build additive indexes in the maintenance worker, including legacy databases."""
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE INDEX IF NOT EXISTS pending_event_created ON events(created_at) WHERE recipients!='[]';
                CREATE INDEX IF NOT EXISTS bundle_scope_capture ON bundles(umo,captured_at DESC);
                CREATE INDEX IF NOT EXISTS attempt_scope_started ON attempts(umo,started_at);
                CREATE INDEX IF NOT EXISTS bundle_turn_capture ON bundles(turn_id,captured_at);
            """)

    def enqueue(self, event: dict, recipients: tuple[str, ...], now: float):
        """Persist a notification once, without reopening completed deliveries.

        Args:
            event: Source-attributed event envelope.
            recipients: Named public API providers to notify.
            now: Civil timestamp.
        """
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                       (event["event_id"], json.dumps(event, ensure_ascii=False),
                        json.dumps(list(recipients)), now))

    def pending(self, limit=100):
        with closing(self._connect()) as db:
            return [(json.loads(row["payload"]), json.loads(row["recipients"]))
                    for row in db.execute("SELECT * FROM events WHERE recipients!='[]' ORDER BY created_at LIMIT ?",
                                          (limit,))]

    def pending_for(self, recipient, limit=100):
        """Read an independent recipient queue, keeping original event order."""
        with closing(self._connect()) as db:
            return [json.loads(row["payload"]) for row in db.execute(
                "SELECT payload FROM events WHERE recipients!='[]' AND EXISTS "
                "(SELECT 1 FROM json_each(recipients) WHERE value=?) ORDER BY created_at LIMIT ?",
                (recipient, limit))]

    def pending_recipients(self):
        with closing(self._connect()) as db:
            return [row[0] for row in db.execute(
                "SELECT DISTINCT j.value FROM events e, json_each(e.recipients) j WHERE e.recipients!='[]'")]

    def save_bundle(self, bundle_id: str, turn_id: str, umo: str, payload: str, captured: float):
        from .narrative_projection import reply_material
        data = json.loads(payload)
        summary = dict(reply_material(data), archive_id=bundle_id,
                       character_fingerprint=data.get('character', {}).get('fingerprint'),
                       completed=data.get('completed', {}), availability=data.get('availability', {}))
        raw = payload.encode('utf-8')
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO context_archive VALUES (?,?,?)",
                       (bundle_id, hashlib.sha256(raw).hexdigest(), gzip.compress(raw, compresslevel=3)))
            db.execute("INSERT OR IGNORE INTO bundles VALUES (?,?,?,?,?)",
                       (bundle_id, turn_id, umo, json.dumps(summary, ensure_ascii=False), captured))

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
        event = {"schema_version": 1, "event_id": "diagnostic:" + str(bundle_id),
                 "type": "decision.diagnostic", "occurred_at": now, "received_at": now,
                 "scope": scope, "actor_id": "bot", "text": "", "source_ids": [str(bundle_id)],
                 "fact_kind": "diagnostic", "data": data}
        self.enqueue(event, (), now)

    def full_bundle(self, bundle_id):
        """Read complete local evidence, checking the archive before returning it."""
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM context_archive WHERE bundle_id=?', (bundle_id,)).fetchone()
            if row:
                raw = gzip.decompress(row['payload'])
                if hashlib.sha256(raw).hexdigest() != row['sha256']:
                    raise ValueError('context archive checksum mismatch')
                return json.loads(raw)
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
        return {
            "database_bytes": size(self.path),
            "wal_bytes": size(Path(str(self.path) + "-wal")),
            "legacy_database_bytes": size(self.path.parent / "decision-learning.sqlite3"),
            "free_bytes": free,
            "low_space": free < 5 * 1024 ** 3,
            "retention_policy": "compact_online_keep_verified_archive_and_receipts",
        }

    def maintain(self, now, retention_days=7, limit=40):
        """Compress legacy bundles in bounded transactions; never discard receipts.

        After the configured window, only the indexed trace stays online. Full
        contexts stay in the checksum-verified compressed local archive. Event
        IDs, pending notifications, uncertain attempts and intents are retained.
        """
        from .narrative_projection import reply_material
        with closing(self._connect()) as db, db:
            rows = db.execute('SELECT * FROM bundles WHERE bundle_id NOT IN '
                              '(SELECT bundle_id FROM context_archive) LIMIT ?', (limit,)).fetchall()
            for row in rows:
                raw = row['payload'].encode('utf-8')
                data = json.loads(raw)
                compact = dict(reply_material(data), archive_id=row['bundle_id'],
                               character_fingerprint=data.get('character', {}).get('fingerprint'),
                               availability=data.get('availability', {}), completed=data.get('completed', {}))
                db.execute('INSERT OR IGNORE INTO context_archive VALUES (?,?,?)',
                           (row['bundle_id'], hashlib.sha256(raw).hexdigest(), gzip.compress(raw)))
                db.execute('UPDATE bundles SET payload=? WHERE bundle_id=?',
                           (json.dumps(compact, ensure_ascii=False), row['bundle_id']))
            old = db.execute("SELECT bundle_id,payload FROM bundles WHERE captured_at<? "
                             "AND json_extract(payload,'$.archive_only') IS NULL LIMIT ?",
                             (now - max(1, retention_days) * 86400, limit)).fetchall()
            for row in old:
                brief = json.loads(row['payload'])
                brief = {k:v for k,v in brief.items() if k in
                         {'archive_id','source_scope','character_fingerprint','availability','completed'}}
                brief['archive_only'] = True
                db.execute('UPDATE bundles SET payload=? WHERE bundle_id=?',
                           (json.dumps(brief, ensure_ascii=False), row['bundle_id']))
        return {'compressed': len(rows), 'archived': len(old)}

    def backfill_portraits(self, limit=100):
        """Queue public replay once per source; no companion database access."""
        with closing(self._connect()) as db, db:
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
        with closing(self._connect()) as db, db:
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
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,'sending',?,NULL,NULL)",
                       (attempt, scope["umo"], turn_id, intent_id, segment, now))
            if intent_id:
                db.execute("UPDATE intents SET status='sending',attempted_at=? WHERE intent_id=?", (now, intent_id))
        return attempt

    def finish(self, attempt: str, status: str, now: float, message_id=None, event=None, recipients=()):
        with closing(self._connect()) as db, db:
            prior = db.execute('SELECT status FROM attempts WHERE attempt_id=?', (attempt,)).fetchone()
            if prior is not None and prior['status'] == 'success' and status != 'success':
                return
            db.execute("UPDATE attempts SET status=?,finished_at=?,message_id=? WHERE attempt_id=?",
                       (status, now, message_id, attempt))
            if event is not None:
                db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)", (event["event_id"],
                           json.dumps(event, ensure_ascii=False), json.dumps(list(recipients)), now))

    def add_intent(self, intent: dict):
        with closing(self._connect()) as db, db:
            changed = db.execute("INSERT OR IGNORE INTO intents VALUES (?,?,'pending',NULL)",
                                 (intent["intent_id"], json.dumps(intent, ensure_ascii=False))).rowcount
        return bool(changed)

    def intents(self):
        with closing(self._connect()) as db:
            return [(json.loads(row["payload"]), row["status"]) for row in db.execute("SELECT * FROM intents")]

    def intent_status(self, intent_id: str, status: str):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE intents SET status=? WHERE intent_id=?", (status, intent_id))

    def reminder_scope(self, umo, reminder_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.scope.umo')=? "
                             "AND json_extract(payload,'$.data.reminder_id')=? "
                             "AND json_extract(payload,'$.data.effect_kind')='reminder.created' LIMIT 1",
                             (umo, reminder_id)).fetchone()
            return json.loads(row[0])['scope'] if row else None

    def delivered_reply(self, scope, message_id):
        if not message_id:
            return None
        with closing(self._connect()) as db:
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
        with closing(self._connect()) as db:
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
        with closing(self._connect()) as db:
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
        with closing(self._connect()) as db:
            rows = db.execute("SELECT started_at FROM attempts WHERE umo=? AND intent_id IS NOT NULL "
                              "AND status IN ('success','uncertain','sending') AND started_at>=? ORDER BY started_at DESC",
                              (umo, day_start)).fetchall()
        return len(rows), (rows[0][0] if rows else None)

    def snapshot(self, umo: str):
        with closing(self._connect()) as db:
            attempts = [dict(row) for row in db.execute("SELECT * FROM attempts WHERE umo=? ORDER BY started_at DESC LIMIT 80", (umo,))]
            pending = db.execute("SELECT count(*) FROM events WHERE recipients!='[]' AND json_extract(payload,'$.scope.umo')=?", (umo,)).fetchone()[0]
            contexts = [dict(row) for row in db.execute(
                "SELECT bundle_id,turn_id,captured_at,length(payload) AS online_characters FROM bundles WHERE umo=? ORDER BY captured_at DESC LIMIT 10", (umo,))]
        return {"receipts": attempts, "pending_notifications": pending,
                "contexts": contexts,
                "intents": [dict(payload, delivery_status=status) for payload, status in self.intents()
                            if payload.get("scope", {}).get("umo") == umo]}
