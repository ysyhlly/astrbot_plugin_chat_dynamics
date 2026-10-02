"""Human labels for offline routing evaluation; never mutate live routing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from copy import deepcopy

from .routing_trace import build_routing_trace, trace_with_updates

ERROR_TYPES = {"correct", "topic_merge", "topic_split", "wrong_assignment", "premature_assignment", "reopen_miss", "unknown", "unreviewed"}

RECIPIENT_ERROR_TYPES = {"correct", "missed_bot", "false_bot", "wrong_recipient", "missing_recipient", "subject_confusion", "unknown"}
REQUIRED_FIELDS = {"session_key", "msg_id", "expected_topic", "error_type"}
# Keep the legacy session index key to recover existing manual labels.
ANNOTATION_INDEX_KEY = "annotation_drafts_index_v1"
RECIPIENT_FIELDS = {"recipient_correct", "bot_targeted", "recipient_ids", "subject_ids", "expected_reply", "recipient_error_type"}


class TopicAnnotations:
    @staticmethod
    def revision(record):
        return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()

    @classmethod
    def state_key(cls, session):
        return "annotation_review_state_v2_" + cls.digest(session)


    async def _state(self, session):
        state = await self.plugin.get_kv_data(self.state_key(session), None)
        if isinstance(state, dict):
            return deepcopy(state)
        rows = await self.plugin.get_kv_data(self.key(session), [])
        return {"labels": deepcopy(rows) if isinstance(rows, list) else []}


    async def reconcile(self):
        async with self.lock:
            sessions = await self.known_sessions()
            repaired = 0
            for session in sessions:
                state = await self.plugin.get_kv_data(self.state_key(session), None)
                if isinstance(state, dict) and state.get("projection_pending"):
                    await self.plugin.put_kv_data(self.key(session), deepcopy(state["labels"]))
                    state = deepcopy(state)
                    state["projection_pending"] = False
                    await self.plugin.put_kv_data(self.state_key(session), state)
                    repaired += 1
            return {"sessions": len(sessions), "repaired": repaired}

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


    async def _index_session(self, session, *, remove: bool = False) -> None:
        """Keep review state discoverable for crash recovery. Caller holds the lock.

        Never truncate this index: sessions can have a pending
        label projection or a committed request that must survive a restart.
        """
        if remove:
            return  # Retain tombstones so interrupted writes remain discoverable.
        key = ANNOTATION_INDEX_KEY + "_shard_" + self.digest(session)[:2]
        rows = await self.plugin.get_kv_data(key, [])
        rows = [str(row) for row in rows if isinstance(row, str)] if isinstance(rows, list) else []
        rows = [row for row in rows if row != session]
        if not remove:
            rows.insert(0, session)
        await self.plugin.put_kv_data(key, rows)

    async def known_sessions(self) -> list:
        """Sessions with stored manual annotations, whether or not they are live."""
        rows = await self.plugin.get_kv_data(ANNOTATION_INDEX_KEY, [])
        result = [row for row in rows if isinstance(row, str)] if isinstance(rows, list) else []
        for shard in range(256):
            rows = await self.plugin.get_kv_data(ANNOTATION_INDEX_KEY + "_shard_" + format(shard, "02x"), [])
            if isinstance(rows, list):
                result.extend(row for row in rows if isinstance(row, str))
        return list(dict.fromkeys(result))


    async def read(self, session):
        rows = (await self._state(session))["labels"]
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
        topic_rows = [row for row in rows if row.get("topic_reviewed", True)]
        counts = Counter(row["error_type"] for row in topic_rows)
        matrix = Counter((row["predicted_topic"], row["expected_topic"]) for row in topic_rows)
        recipient_rows = [row for row in rows if RECIPIENT_FIELDS.intersection(row)]
        recipient_counts = Counter(row["recipient_error_type"] for row in recipient_rows if "recipient_error_type" in row)
        assisted = sum(1 for row in rows if row.get("accepted_from") == "ai")
        # Labels are a selected sample, not an unbiased estimate of accuracy.
        return {"records": rows, "metrics": {"total": len(topic_rows), "unreviewed": len(rows) - len(topic_rows), "ai_assisted": assisted,
                "error_counts": dict(counts),
                "confusion": [{"predicted": a, "expected": b, "count": n} for (a, b), n in sorted(matrix.items())],
                "sample_note": ("仅统计人工标注样本，不代表真实准确率"
                    + ("；历史记录中 %d 条曾采纳模型草稿" % assisted if assisted else ""))},
                "recipient_metrics": {"total": len(recipient_rows), "error_counts": dict(recipient_counts),
                    "correct": sum(row.get("recipient_correct") is True for row in recipient_rows),
                    "incorrect": sum(row.get("recipient_correct") is False for row in recipient_rows),
                    "expected_reply": sum(row.get("expected_reply") is True for row in recipient_rows),
                    "sample_note": "仅统计人工标注样本，不代表真实准确率"}}

    async def save(self, body):
        """Write one human label for a message still available in replay."""
        if not isinstance(body, dict):
            raise ValueError("invalid annotation fields")
        if not REQUIRED_FIELDS <= set(body) or set(body) - REQUIRED_FIELDS - RECIPIENT_FIELDS - {"expected_revision"}:
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
        frozen_trace = node.metadata.get("decision_trace") or node.metadata.get("trace_inputs") or {}
        if isinstance(frozen_trace, dict) and frozen_trace.get("trace_schema_version"):
            predicted = str(frozen_trace.get("routing", {}).get("selected_topic")
                            or frozen_trace.get("topic", {}).get("topic_id") or predicted)
        expected = body["expected_topic"]
        # Validate against live and archived topics before writing.
        known = ({str(n.metadata.get("routing", {}).get("topic_id")) for n in dag.nodes.values()}
                 if dag is not None else set())
        runtime = getattr(self.plugin, "_sessions", {}).get(session)
        state = getattr(runtime, "routing_state", None)
        known.update(getattr(state, "topics", {}).keys())
        known.update(getattr(state, "archived_topics", {}).keys())
        known.update(getattr(getattr(state, "archive", None), "entries", {}).keys())
        if expected not in known | {"NEW", "UNKNOWN", "CORRECT", "KEEP", "UNREVIEWED"}:
            raise ValueError("unknown target topic")
        if expected == "CORRECT":
            expected, error = predicted, "correct"
        elif expected not in {"KEEP", "UNREVIEWED"} and error == "correct" and expected != predicted:
            raise ValueError("correct label must match prediction")
        record = {"annotation_schema_version": 2, "msg_id": mid, "predicted_topic": predicted, "expected_topic": expected,
                  "error_type": error, "annotated_at": time.time(),
                  "session_hash": self.digest(session),
                  "routing": {key: deepcopy(routing[key]) for key in ("topic_confidence", "ambiguous", "topic_ambiguous", "topic_status", "candidates", "topic_candidates", "topic_candidate_evidence", "boundary_score", "evidence") if key in routing}}
        record.update({key: deepcopy(body[key]) for key in RECIPIENT_FIELDS if key in body})
        # `decision_trace` is the live frozen snapshot; `trace_inputs` is the bounded
        # copy that survives a restart. Both carry the same key names by construction.
        trace = node.metadata.get("decision_trace") or node.metadata.get("trace_inputs") or {}
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
        if trace.get("trace_schema_version"):
            record["decision_trace"] = trace_with_updates(
                trace, outcome=node.metadata.get("outcome") or trace.get("outcome"),
                shadow=node.metadata.get("shadow_decision") or trace.get("shadow"))
        # Only submitted labels and bounded diagnostics, never automatic message collection.
        if getattr(self.plugin, "console_show_message_content", False):
            record["text"] = node.text[:2000]
        async with self.lock:
            state = await self._state(session)
            rows = (await self.read(session))["records"]
            previous = next((row for row in rows if row["msg_id"] == mid), None)
            if "expected_revision" in body and body["expected_revision"] != self.revision(previous):
                raise ValueError("标注已被其他页面修改，请刷新后重新确认")
            record["label_source"] = "human"
            if body["expected_topic"] in {"KEEP", "UNREVIEWED"}:
                # Older learning consumers already treat an empty topic as absent
                # supervision; a new sentinel would be mistaken for a real topic.
                record.update(expected_topic="", error_type="unreviewed", topic_reviewed=False)
                if previous and body["expected_topic"] == "KEEP":
                    for key in ("expected_topic", "error_type", "topic_reviewed"):
                        record[key] = previous.get(key, True if key == "topic_reviewed" else record[key])
            else:
                record["topic_reviewed"] = True
            rows = [row for row in rows if row["msg_id"] != mid]
            rows.append(record)
            state["labels"] = rows[-2000:]
            result = {"saved": True, "record": record}
            state["projection_pending"] = True
            await self._index_session(session)
            await self.plugin.put_kv_data(self.state_key(session), state)
            await self.plugin.put_kv_data(self.key(session), state["labels"])
        return result
