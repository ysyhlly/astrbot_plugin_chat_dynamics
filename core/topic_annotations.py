"""Human labels for offline routing evaluation; never mutate live routing."""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter
from copy import deepcopy

from .routing_trace import build_routing_trace

ERROR_TYPES = {"correct", "topic_merge", "topic_split", "wrong_assignment", "premature_assignment", "reopen_miss", "unknown"}

RECIPIENT_ERROR_TYPES = {"correct", "missed_bot", "false_bot", "wrong_recipient", "missing_recipient", "subject_confusion", "unknown"}
REQUIRED_FIELDS = {"session_key", "msg_id", "expected_topic", "error_type"}
# Drafts live in their own key: a proposal a human has not accepted must not be
# readable as a label by anything downstream (the learning plugin, the export,
# the metrics below).
# Drafts outlive the in-memory message graph, so the sessions that own them
# are indexed here: without the index a restart hides drafts on sessions that
# have not seen new traffic, and nobody can even dismiss them.
DRAFT_KEY_PREFIX = "annotation_drafts_v1_"
DRAFT_INDEX_KEY = "annotation_drafts_index_v1"
MAX_INDEXED_SESSIONS = 200
RECIPIENT_FIELDS = {"recipient_correct", "bot_targeted", "recipient_ids", "subject_ids", "expected_reply", "recipient_error_type"}


class TopicAnnotations:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()

    @staticmethod
    def digest(session):
        """Stable per-session key that does not carry the group id itself.

        The record stores this so an export stays groupable after it leaves the
        plugin: without it a learner cannot split train from validation by
        conversation, and every row collapses into one "unknown" session.
        """
        return hashlib.sha256(session.encode()).hexdigest()

    @classmethod
    def key(cls, session):
        return "topic_annotations_v1_" + cls.digest(session)

    @classmethod
    def draft_key(cls, session):
        return DRAFT_KEY_PREFIX + cls.digest(session)

    async def read_drafts(self, session) -> dict:
        """Model-drafted labels nobody has accepted yet."""
        raw = await self.plugin.get_kv_data(self.draft_key(session), {})
        if not isinstance(raw, dict):
            return {}
        drafts = raw.get("drafts")
        return {
            "draft_schema_version": raw.get("draft_schema_version"),
            "generated_at": raw.get("generated_at"),
            "provider_id": str(raw.get("provider_id") or ""),
            "model": str(raw.get("model") or ""),
            "drafts": {str(mid): draft for mid, draft in drafts.items()
                       if isinstance(draft, dict)} if isinstance(drafts, dict) else {},
        }

    async def _index_session(self, session, *, remove: bool = False) -> None:
        """Remember (or forget) a session that has drafts. Caller holds the lock."""
        rows = await self.plugin.get_kv_data(DRAFT_INDEX_KEY, [])
        rows = [str(row) for row in rows if isinstance(row, str)] if isinstance(rows, list) else []
        rows = [row for row in rows if row != session]
        if not remove:
            rows.insert(0, session)
        await self.plugin.put_kv_data(DRAFT_INDEX_KEY, rows[:MAX_INDEXED_SESSIONS])

    async def known_sessions(self) -> list:
        """Sessions with stored drafts, newest first, whether or not they are live."""
        rows = await self.plugin.get_kv_data(DRAFT_INDEX_KEY, [])
        return [str(row) for row in rows if isinstance(row, str)] if isinstance(rows, list) else []

    async def save_drafts(self, session, payload) -> dict:
        raw = payload.get("drafts")
        record = {
            "draft_schema_version": payload.get("draft_schema_version") or 1,
            "generated_at": time.time(),
            "provider_id": str(payload.get("provider_id") or "")[:64],
            "model": str(payload.get("model") or "")[:64],
            "drafts": {str(mid): draft for mid, draft in (raw or {}).items()
                       if isinstance(mid, str) and isinstance(draft, dict)},
        }
        async with self.lock:
            await self.plugin.put_kv_data(self.draft_key(session), record)
            await self._index_session(session)
        return record

    async def clear_drafts(self, session) -> None:
        async with self.lock:
            await self.plugin.put_kv_data(self.draft_key(session), {})
            await self._index_session(session, remove=True)

    async def remove_drafts(self, session, msg_ids) -> int:
        """Drop drafts a reviewer dismissed or accepted; returns the count removed."""
        wanted = {str(mid) for mid in msg_ids if str(mid).strip()}
        if not wanted:
            return 0
        async with self.lock:
            raw = await self.plugin.get_kv_data(self.draft_key(session), {})
            if not isinstance(raw, dict):
                return 0
            drafts = raw.get("drafts")
            if not isinstance(drafts, dict):
                return 0
            removed = 0
            for mid in wanted:
                if drafts.pop(mid, None) is not None:
                    removed += 1
            raw["drafts"] = drafts
            await self.plugin.put_kv_data(self.draft_key(session), raw)
            if not drafts:
                await self._index_session(session, remove=True)
            return removed

    async def read(self, session):
        rows = await self.plugin.get_kv_data(self.key(session), [])
        rows = deepcopy(rows) if isinstance(rows, list) else []
        # A stored row is only trusted while it still carries the fields this
        # schema requires: one legacy or truncated row used to break both the
        # read and every later save (save() reads first), with no log.
        rows = [
            row for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("error_type"), str)
            and isinstance(row.get("predicted_topic"), str)
            and isinstance(row.get("expected_topic"), str)
            and isinstance(row.get("msg_id"), str)
        ]
        counts = Counter(row["error_type"] for row in rows)
        matrix = Counter((row["predicted_topic"], row["expected_topic"]) for row in rows)
        recipient_rows = [row for row in rows if RECIPIENT_FIELDS.intersection(row)]
        recipient_counts = Counter(row["recipient_error_type"] for row in recipient_rows if "recipient_error_type" in row)
        drafts = await self.read_drafts(session)
        assisted = sum(1 for row in rows if row.get("accepted_from") == "ai")
        # Labels are a selected sample, not an unbiased estimate of accuracy.
        return {"records": rows, "metrics": {"total": len(rows), "ai_assisted": assisted,
                "error_counts": dict(counts),
                "confusion": [{"predicted": a, "expected": b, "count": n} for (a, b), n in sorted(matrix.items())],
                "sample_note": ("仅统计人工标注样本，不代表真实准确率；其中 %d 条采纳了模型草稿" % assisted)},
                "drafts": drafts.get("drafts") or {},
                "drafts_generated_at": drafts.get("generated_at"),
                "recipient_metrics": {"total": len(recipient_rows), "error_counts": dict(recipient_counts),
                    "correct": sum(row.get("recipient_correct") is True for row in recipient_rows),
                    "incorrect": sum(row.get("recipient_correct") is False for row in recipient_rows),
                    "expected_reply": sum(row.get("expected_reply") is True for row in recipient_rows),
                    "sample_note": "仅统计人工标注样本，不代表真实准确率"}}

    async def save(self, body):
        if not isinstance(body, dict) or not REQUIRED_FIELDS <= set(body) or set(body) - REQUIRED_FIELDS - RECIPIENT_FIELDS:
            raise ValueError("invalid annotation fields")
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in (body[key] for key in REQUIRED_FIELDS)):
            raise ValueError("invalid annotation value")
        for key in ("recipient_correct", "bot_targeted", "expected_reply"):
            if key in body and type(body[key]) is not bool:
                raise ValueError("invalid recipient boolean")
        for key in ("recipient_ids", "subject_ids"):
            if key in body and (not isinstance(body[key], list) or len(body[key]) > 64
                    or any(not isinstance(value, str) or not value or len(value) > 256 for value in body[key])
                    or len(set(body[key])) != len(body[key])):
                raise ValueError("invalid recipient identities")
        if "recipient_error_type" in body and (not isinstance(body["recipient_error_type"], str)
                or body["recipient_error_type"] not in RECIPIENT_ERROR_TYPES):
            raise ValueError("invalid recipient error type")
        session, mid = body["session_key"], body["msg_id"]
        error = body["error_type"]
        if error not in ERROR_TYPES:
            raise ValueError("invalid error type")
        dag = getattr(self.plugin, "dags", {}).get(session)
        node = dag.nodes.get(mid) if dag else None
        if node is None:
            raise ValueError("message no longer available; refresh replay")
        routing = node.metadata.get("routing", {})
        predicted = str(routing.get("topic_id") or "UNKNOWN")
        expected = body["expected_topic"]
        known = {str(n.metadata.get("routing", {}).get("topic_id")) for n in dag.nodes.values()}
        runtime = getattr(self.plugin, "_sessions", {}).get(session)
        state = getattr(runtime, "routing_state", None)
        known.update(getattr(state, "topics", {}).keys())
        known.update(getattr(state, "archived_topics", {}).keys())
        known.update(getattr(getattr(state, "archive", None), "entries", {}).keys())
        if expected not in known | {"NEW", "UNKNOWN", "CORRECT"}:
            raise ValueError("unknown target topic")
        if expected == "CORRECT":
            expected, error = predicted, "correct"
        elif error == "correct" and expected != predicted:
            raise ValueError("correct label must match prediction")
        record = {"annotation_schema_version": 2, "msg_id": mid, "predicted_topic": predicted, "expected_topic": expected,
                  "error_type": error, "annotated_at": time.time(),
                  "session_hash": self.digest(session),
                  "routing": {key: deepcopy(routing[key]) for key in ("topic_confidence", "ambiguous", "topic_ambiguous", "topic_status", "candidates", "topic_candidates", "topic_candidate_evidence", "boundary_score", "evidence") if key in routing}}
        record.update({key: deepcopy(body[key]) for key in RECIPIENT_FIELDS if key in body})
        trace = node.metadata.get("routing_trace", node.metadata.get("decision_trace", {}))
        trace = trace if isinstance(trace, dict) else {}
        # The outcome is read from the node rather than from the frozen trace:
        # the trace was snapshotted at decision time, and the gate, the generator
        # and the platform adapter all ran after that. `build_routing_trace`
        # re-attaches it so the rebuilt snapshot describes the whole turn.
        record["decision_trace"] = build_routing_trace(
            routing=routing, identity=trace.get("identity"), participation=trace.get("participation"),
            state=trace.get("state"), mode=trace.get("mode", "legacy"),
            weights_version=trace.get("weights_version", "default"),
            outcome=node.metadata.get("outcome") or trace.get("outcome"),
            shadow=node.metadata.get("shadow_decision") or trace.get("shadow"),
        )
        # Only submitted labels and bounded diagnostics, never automatic message collection.
        if getattr(self.plugin, "console_show_message_content", False):
            record["text"] = node.text[:2000]
        async with self.lock:
            # Provenance: a human pressed save, and if the values they saved are
            # the ones the draft proposed, the record says so instead of leaving
            # the learning layer to guess how much of a label is model output.
            record["label_source"] = "human"
            draft = (await self.read_drafts(session)).get("drafts", {}).get(mid)
            if isinstance(draft, dict):
                shared = [key for key in ("expected_reply", "bot_targeted") if key in draft]
                if shared and all(body.get(key) == draft.get(key) for key in shared):
                    record["accepted_from"] = "ai"
                    record["draft_confidence"] = draft.get("confidence")
            rows = (await self.read(session))["records"]
            rows = [row for row in rows if row["msg_id"] != mid]
            rows.append(record)
            await self.plugin.put_kv_data(self.key(session), rows[-2000:])
        return {"saved": True, "record": record}
