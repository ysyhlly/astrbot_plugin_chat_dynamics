"""Read-only request-level latency and adoption report for collected decisions.

Only aggregate counts leave this process. The source SQLite database is opened
read-only and the output contains no session IDs, message text, or provider keys.
This measures the plugin's decision path; it does not claim to time reply
generation or network delivery.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time


def _percentiles(values: list[float]) -> dict:
    finite = sorted(value for value in values if math.isfinite(value) and value >= 0)
    if not finite:
        return {"n": 0, "p50": None, "p95": None, "max": None}

    def nearest_rank(fraction: float) -> float:
        return round(finite[max(0, math.ceil(len(finite) * fraction) - 1)], 1)

    return {"n": len(finite), "p50": round(statistics.median(finite), 1),
            "p95": nearest_rank(.95), "max": round(finite[-1], 1)}


def _window_start(value: str) -> float:
    if value.isdigit():
        return float(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--since must include a timezone")
    return parsed.timestamp()


def _load(path: Path, since: float) -> list[dict]:
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = database.execute("SELECT payload FROM samples WHERE created >= ? ORDER BY created, id",
                                (since,)).fetchall()
    finally:
        database.close()
    return [json.loads(row[0]) for row in rows]


def report(records: list[dict], *, since: float, until: float) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        metadata = record.get("metadata") or {}
        request_id = metadata.get("request_id")
        if isinstance(request_id, str) and request_id:
            groups[request_id].append(record)

    statuses = Counter()
    sources = Counter()
    outcomes = Counter()
    action_choices = Counter()
    latencies: dict[str, list[float]] = defaultdict(list)
    by_question_count: dict[int, list[float]] = defaultdict(list)
    complete = adopted = model_only = 0
    join_positive = selected_targets = 0
    for rows in groups.values():
        head = rows[0]
        metadata = head.get("metadata") or {}
        requested = metadata.get("request_question_count")
        actual_ids = {row.get("metadata", {}).get("question_id") for row in rows}
        is_complete = (type(requested) is int and requested == len(rows) == len(actual_ids))
        complete += is_complete
        model_only += metadata.get("model_only") is True
        statuses[str(metadata.get("student_status") or "unknown")] += 1
        request_sources = {str(row.get("execution_source") or "unknown") for row in rows}
        for source in request_sources:
            sources[source] += 1
        is_adopted = is_complete and request_sources == {"kev"}
        adopted += is_adopted
        outcome = head.get("outcome") or {}
        outcomes[str(outcome.get("final_outcome") or "not_recorded")] += 1
        student_input = metadata.get("student_input") or {}
        for field, value in (("state_bytes", student_input.get("state_bytes")),
                             ("question_count", student_input.get("question_count")),
                             ("input_tokens", student_input.get("input_tokens"))):
            if type(value) is int and value >= 0:
                latencies[field].append(float(value))
        student_ms = metadata.get("student_latency_ms")
        question_count = student_input.get("question_count")
        if (type(question_count) is int and question_count > 0 and
                type(student_ms) in (int, float) and math.isfinite(student_ms) and student_ms >= 0):
            by_question_count[question_count].append(float(student_ms))
        for field, value in (
            ("preparation_ms", metadata.get("preparation_latency_ms")),
            ("student_http_ms", metadata.get("student_latency_ms")),
            ("identity_check_ms", student_input.get("identity_check_ms")),
            ("server_model_ms", student_input.get("server_model_ms")),
            ("decision_ms", outcome.get("latency_ms")),
        ):
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                latencies[field].append(float(value))
        for row in rows:
            qid = (row.get("metadata") or {}).get("question_id")
            answer = row.get("student_prediction") or {}
            probability = answer.get("noul")
            selected = (type(probability) in (int, float) and
                        math.isfinite(probability) and probability >= .5)
            if qid == "join" and selected:
                join_positive += 1
            elif qid == "action" and isinstance(answer.get("choice"), str):
                action_choices[answer["choice"]] += 1
            elif isinstance(qid, str) and qid.startswith("target.") and selected:
                selected_targets += 1

    return {
        "window": {"since_utc": datetime.fromtimestamp(since, timezone.utc).isoformat(),
                   "until_utc": datetime.fromtimestamp(until, timezone.utc).isoformat()},
        "scope": "collected plugin requests only; labels are not ground truth",
        "sample_rows": len(records), "requests": len(groups),
        "complete_request_records": complete, "model_only_requests": model_only,
        "kev_adopted_full_requests": adopted,
        "student_status_requests": dict(sorted(statuses.items())),
        "execution_source_requests": dict(sorted(sources.items())),
        "final_outcome_requests": dict(sorted(outcomes.items())),
        "join_positive_predictions": join_positive,
        "action_predictions": dict(sorted(action_choices.items())),
        "selected_target_predictions": selected_targets,
        "latency_ms": {field: _percentiles(latencies[field]) for field in
                       ("preparation_ms", "identity_check_ms", "server_model_ms",
                        "student_http_ms", "decision_ms")},
        "input": {field: _percentiles(latencies[field]) for field in
                  ("state_bytes", "question_count", "input_tokens")},
        "student_http_ms_by_question_count": {
            str(count): _percentiles(values) for count, values in sorted(by_question_count.items())},
        "unobserved_segments": ["service_queue", "state_encoding",
                                "reply_generation", "message_delivery"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--since", required=True,
                        help="UTC ISO 8601 with timezone, or Unix seconds")
    parser.add_argument("--out", type=Path,
                        help="optional aggregate JSON destination")
    args = parser.parse_args()
    since, until = _window_start(args.since), time.time()
    if not 0 <= since <= until:
        parser.error("invalid --since")
    result = report(_load(args.db, since), since=since, until=until)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
