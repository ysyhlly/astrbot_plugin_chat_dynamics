"""Dependency-free, conservative held-out decision acceptance reports."""

from __future__ import annotations

import math
from collections import Counter


def wilson_interval(errors, count, z=1.96):
    if not count:
        return [0.0, 1.0]
    p = errors / count
    denom = 1 + z * z / count
    centre = (p + z * z / (2 * count)) / denom
    delta = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denom
    return [max(0.0, centre - delta), min(1.0, centre + delta)]


def evaluate_task(records, *, critical=False, kind="choice", min_samples=500, min_class=50, required_labels=None):
    rows = list(records)
    mae = (
        sum(abs(float(r["prediction"]) - float(r["label"])) for r in rows) / max(len(rows), 1)
        if kind == "score"
        else None
    )
    if kind == "score":
        rows = [
            dict(
                r,
                prediction=r.get("prediction_class", round(float(r["prediction"]))),
                label=round(float(r["label"])),
                **({k: round(float(r[k])) for k in ("human_label", "teacher_prediction") if k in r}),
            )
            for r in rows
        ]
    if any(
        not math.isfinite(float(r.get("confidence", 0))) or not 0 <= float(r.get("confidence", 0)) <= 1 for r in rows
    ):
        raise ValueError("Confidence must be finite and in [0,1]")
    n = len(rows)
    counts = Counter(r["label"] for r in rows)
    for label in required_labels or []:
        counts.setdefault(label, 0)
    labels = set(counts) | {r["prediction"] for r in rows}
    f1 = []
    for label in labels:
        tp = sum(r["label"] == label and r["prediction"] == label for r in rows)
        fp = sum(r["label"] != label and r["prediction"] == label for r in rows)
        fn = sum(r["label"] == label and r["prediction"] != label for r in rows)
        f1.append(2 * tp / max(1, 2 * tp + fp + fn))
    ece = 0.0
    for i in range(10):
        batch = [r for r in rows if min(9, int(float(r.get("confidence", 0)) * 10)) == i]
        if batch:
            ece += abs(
                sum(float(r.get("confidence", 0)) for r in batch) - sum(r["prediction"] == r["label"] for r in batch)
            ) / max(n, 1)
    accepted = [r for r in rows if r.get("accepted", False)]
    errors = sum(r["prediction"] != r["label"] for r in accepted)
    accepted_groups = {}
    for row in accepted:
        if row.get("request_id"):
            key = row["request_id"]
            accepted_groups[key] = accepted_groups.get(key, False) or row["prediction"] != row["label"]
    group_ci = wilson_interval(sum(accepted_groups.values()), len(accepted_groups))
    latency = sorted(float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None)
    if any(not math.isfinite(v) or v < 0 for v in latency):
        raise ValueError("Latency must be finite and nonnegative")
    human = [r for r in rows if "human_label" in r and "teacher_prediction" in r]
    diffs = [int(r["prediction"] != r["human_label"]) - int(r["teacher_prediction"] != r["human_label"]) for r in human]
    delta = sum(diffs) / len(diffs) if diffs else None
    # Paired normal interval, with conservative finite-sample floor at zero variance.
    se = math.sqrt(sum((v - delta) ** 2 for v in diffs) / max(1, len(diffs) - 1) / len(diffs)) if diffs else None
    radius = max(1.96 * se, 3 / len(diffs)) if diffs else None
    result = dict(
        kind=kind,
        critical=critical,
        required_labels=list(required_labels or counts),
        count=n,
        class_counts=dict(counts),
        macro_f1=sum(f1) / max(1, len(f1)),
        mae=mae,
        ece=ece,
        coverage=len(accepted) / max(n, 1),
        accepted_error=errors / max(1, len(accepted)),
        accepted_error_ci=wilson_interval(errors, len(accepted)),
        accepted_request_count=len(accepted_groups),
        accepted_request_error_ci=group_ci,
        p95_ms=latency[max(0, math.ceil(0.95 * len(latency)) - 1)] if latency else None,
        human_count=len(human),
        critical_error_delta=delta,
        critical_error_delta_ci=[max(-1, delta - radius), min(1, delta + radius)] if diffs else None,
    )
    reasons = []
    if n < min_samples:
        reasons.append("insufficient_test_samples")
    if not counts or min(counts.values()) < min_class:
        reasons.append("insufficient_class_samples")
    if kind == "score" and result["mae"] > 0.5:
        reasons.append("mae")
    if kind != "score" and result["macro_f1"] < 0.90:
        reasons.append("macro_f1")
    if ece > 0.05:
        reasons.append("calibration")
    if result["coverage"] < 0.80:
        reasons.append("coverage")
    if not accepted or result["accepted_error"] > 0.05:
        reasons.append("accepted_error")
    if result["accepted_error_ci"][1] > 0.05:
        reasons.append("accepted_error_uncertainty")
    if len(accepted_groups) < 20 or group_ci[1] > 0.05:
        reasons.append("accepted_request_uncertainty")
    if len(latency) != n or result["p95_ms"] is None or result["p95_ms"] > 500:
        reasons.append("latency")
    if critical and (len(human) < min_samples or result["critical_error_delta_ci"][1] > 0.02):
        reasons.append("critical_human_comparison")
    result.update(eligible=not reasons, reasons=reasons)
    return result


def evaluate_target_sets(records):
    """Measure whole target sets on complete requests, not pooled candidates."""
    groups = {}
    for row in records:
        if row.get("request_id"):
            groups.setdefault(row["request_id"], []).append(row)
    count = exact = false_positive = false_negative = predicted = expected = 0
    accepted = accepted_errors = invalid_plans = incomplete = 0
    for group in groups.values():
        targets = [r for r in group if r.get("task_id") == "target"]
        if not targets:
            continue
        ids = [r.get("question_id") for r in group]
        if (any(not q for q in ids) or len(set(ids)) != len(group)
                or any(type(r.get("request_question_count")) is not int
                       or r["request_question_count"] != len(group) for r in group)):
            incomplete += 1
            continue
        if any(r.get("prediction") not in (0, 1) or r.get("label") not in (0, 1) for r in targets):
            incomplete += 1
            continue
        actual = {r["question_id"] for r in targets if r["prediction"] == 1}
        truth = {r["question_id"] for r in targets if r["label"] == 1}
        invalid = not actual and any(r.get("prediction_action") in {"reply", "acknowledge", "clarify", "close"} for r in group)
        correct = actual == truth and not invalid
        count += 1
        exact += actual == truth
        invalid_plans += invalid
        false_positive += len(actual - truth)
        false_negative += len(truth - actual)
        predicted += len(actual)
        expected += len(truth)
        if all(r.get("accepted") is True for r in targets):
            accepted += 1
            accepted_errors += not correct
    result = {"count": count, "incomplete_requests": incomplete,
              "exact_match": exact / max(1, count), "false_positive_targets": false_positive,
              "false_negative_targets": false_negative, "precision": (predicted - false_positive) / max(1, predicted),
              "recall": (expected - false_negative) / max(1, expected), "invalid_plans": invalid_plans,
              "accepted_count": accepted, "coverage": accepted / max(1, count),
              "accepted_error_ci": wilson_interval(accepted_errors, accepted),
              "set_error_ci": wilson_interval(count - exact, count)}
    reasons = []
    if count < 500:
        reasons.append("insufficient_complete_requests")
    if result["set_error_ci"][1] > .05 or invalid_plans:
        reasons.append("target_set_errors")
    if result["coverage"] < .8 or result["accepted_error_ci"][1] > .05:
        reasons.append("target_set_takeover")
    result.update(eligible=not reasons, reasons=reasons)
    return result
