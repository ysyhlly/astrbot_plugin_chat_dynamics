"""Human labels for offline routing evaluation; never mutate live routing."""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter

ERROR_TYPES = {"correct", "topic_merge", "topic_split", "wrong_assignment", "premature_assignment", "reopen_miss", "unknown"}


class TopicAnnotations:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()

    @staticmethod
    def key(session):
        return "topic_annotations_v1_" + hashlib.sha256(session.encode()).hexdigest()

    async def read(self, session):
        rows = await self.plugin.get_kv_data(self.key(session), [])
        rows = rows if isinstance(rows, list) else []
        counts = Counter(row["error_type"] for row in rows)
        matrix = Counter((row["predicted_topic"], row["expected_topic"]) for row in rows)
        # Labels are a selected sample, not an unbiased estimate of accuracy.
        return {"records": rows, "metrics": {"total": len(rows), "error_counts": dict(counts),
                "confusion": [{"predicted": a, "expected": b, "count": n} for (a, b), n in sorted(matrix.items())],
                "sample_note": "仅统计人工标注样本，不代表真实准确率"}}

    async def save(self, body):
        if set(body) != {"session_key", "msg_id", "expected_topic", "error_type"}:
            raise ValueError("invalid annotation fields")
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in body.values()):
            raise ValueError("invalid annotation value")
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
        record = {"msg_id": mid, "predicted_topic": predicted, "expected_topic": expected,
                  "error_type": error, "annotated_at": time.time(),
                  "routing": {key: routing[key] for key in ("topic_confidence", "ambiguous", "topic_ambiguous", "topic_status", "candidates", "topic_candidates", "boundary_score", "evidence") if key in routing}}
        # No message bodies or participant identities are persisted by default.
        if getattr(self.plugin, "console_show_message_content", False):
            record["text"] = node.text[:2000]
        async with self.lock:
            rows = (await self.read(session))["records"]
            rows = [row for row in rows if row["msg_id"] != mid]
            rows.append(record)
            await self.plugin.put_kv_data(self.key(session), rows[-2000:])
        return {"saved": True, "record": record}
