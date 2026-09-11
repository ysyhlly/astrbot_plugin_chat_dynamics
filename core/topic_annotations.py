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
RECIPIENT_FIELDS = {"recipient_correct", "bot_targeted", "recipient_ids", "subject_ids", "expected_reply", "recipient_error_type"}


class TopicAnnotations:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()

    @staticmethod
    def key(session):
        return "topic_annotations_v1_" + hashlib.sha256(session.encode()).hexdigest()

    async def read(self, session):
        rows = await self.plugin.get_kv_data(self.key(session), [])
        rows = deepcopy(rows) if isinstance(rows, list) else []
        counts = Counter(row["error_type"] for row in rows)
        matrix = Counter((row["predicted_topic"], row["expected_topic"]) for row in rows)
        recipient_rows = [row for row in rows if RECIPIENT_FIELDS.intersection(row)]
        recipient_counts = Counter(row["recipient_error_type"] for row in recipient_rows if "recipient_error_type" in row)
        # Labels are a selected sample, not an unbiased estimate of accuracy.
        return {"records": rows, "metrics": {"total": len(rows), "error_counts": dict(counts),
                "confusion": [{"predicted": a, "expected": b, "count": n} for (a, b), n in sorted(matrix.items())],
                "sample_note": "仅统计人工标注样本，不代表真实准确率"},
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
                  "routing": {key: deepcopy(routing[key]) for key in ("topic_confidence", "ambiguous", "topic_ambiguous", "topic_status", "candidates", "topic_candidates", "boundary_score", "evidence") if key in routing}}
        record.update({key: deepcopy(body[key]) for key in RECIPIENT_FIELDS if key in body})
        trace = node.metadata.get("routing_trace", node.metadata.get("decision_trace", {}))
        trace = trace if isinstance(trace, dict) else {}
        record["decision_trace"] = build_routing_trace(
            routing=routing, identity=trace.get("identity"), participation=trace.get("participation"),
            state=trace.get("state"), mode=trace.get("mode", "legacy"),
            weights_version=trace.get("weights_version", "default"),
        )
        # Only submitted labels and bounded diagnostics, never automatic message collection.
        if getattr(self.plugin, "console_show_message_content", False):
            record["text"] = node.text[:2000]
        async with self.lock:
            rows = (await self.read(session))["records"]
            rows = [row for row in rows if row["msg_id"] != mid]
            rows.append(record)
            await self.plugin.put_kv_data(self.key(session), rows[-2000:])
        return {"saved": True, "record": record}
