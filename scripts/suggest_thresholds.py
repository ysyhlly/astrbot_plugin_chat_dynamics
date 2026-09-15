"""Read exported annotations and print conservative offline threshold advice.

No plugin imports, configuration writes, or network access. Only recorded
ambient shadow decisions have enough evidence to replay a threshold here.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 1 and math.isfinite(value))


def records_from(payload):
    """Accept the replay page's annotation export or the API response wrapper."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    rows = payload.get("records", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("expected a records list or annotation export object")
    return rows


def assess(rows, threshold):
    counts = dict(tp=0, fp=0, tn=0, fn=0)
    for row in rows:
        predicted = row["score"] >= threshold
        counts[("t" if predicted == row["expected"] else "f")
               + ("p" if predicted else "n")] += 1
    counts["samples"] = len(rows)
    counts["balanced_accuracy"] = (
        counts["tp"] / (counts["tp"] + counts["fn"])
        + counts["tn"] / (counts["tn"] + counts["fp"])) / 2
    return counts


def suggest(records):
    skipped = Counter()
    eligible = []
    seen = {}
    conflicts = set()
    for row in records:
        if not isinstance(row, dict):
            skipped["invalid_record"] += 1
            continue
        session, mid = row.get("session_hash"), row.get("msg_id")
        if not isinstance(session, str) or not session or not isinstance(mid, str) or not mid:
            skipped["missing_identity"] += 1
            continue
        key = (session, mid)
        if key in seen:
            skipped["duplicate"] += 1
            if seen[key] != row:
                conflicts.add(key)
            continue
        seen[key] = row
    for key, row in seen.items():
        if key in conflicts:
            skipped["conflicting_identity"] += 1
            continue
        if row.get("label_source") != "human" or not isinstance(row.get("expected_reply"), bool):
            skipped["missing_human_reply_label"] += 1
            continue
        trace = row.get("decision_trace")
        shadow = trace.get("shadow") if isinstance(trace, dict) else None
        if not isinstance(shadow, dict) or shadow.get("reason") != "ambient" or trace.get("mode") != "legacy":
            skipped["not_replayable_ambient"] += 1
            continue
        score, baseline = shadow.get("score"), shadow.get("baseline_threshold")
        if (not number(score) or not number(baseline)
                or not isinstance(shadow.get("baseline_reply"), bool)
                or shadow["baseline_reply"] != (score >= baseline)):
            skipped["invalid_or_inconsistent_score"] += 1
            continue
        version = trace.get("weights_version")
        if not isinstance(version, str) or not version:
            skipped["missing_weights_version"] += 1
            continue
        eligible.append(dict(session=key[0], score=score, baseline=baseline,
                             version=version, expected=row["expected_reply"]))
    report = {"schema_version": 1, "input_samples": len(records),
              "eligible_samples": len(eligible), "excluded": dict(skipped),
              "suggestions": [], "scope": "legacy ambient admission only; not delivery or full conversation replay"}
    def stop(reason):
        report["reason"] = reason
        return report
    if len(eligible) < 60:
        return stop("insufficient_samples: require at least 60 eligible labels")
    if len({(r["version"], r["baseline"]) for r in eligible}) != 1:
        return stop("mixed_weights_or_baselines: export a homogeneous cohort")
    sessions = sorted({r["session"] for r in eligible},
                      key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    report["sessions"] = len(sessions)
    if len(sessions) < 4:
        return stop("insufficient_sessions: require at least 4")
    validation_ids = set(sessions[::2])
    train = [r for r in eligible if r["session"] not in validation_ids]
    validation = [r for r in eligible if r["session"] in validation_ids]
    report["split"] = {"train": len(train), "validation": len(validation)}
    for split in (train, validation):
        if min(sum(r["expected"] is label for r in split) for label in (False, True)) < 10:
            return stop("insufficient_class_support: require 10 of each class in each session-isolated split")
    baseline = eligible[0]["baseline"]
    candidates = sorted({baseline, *(step / 100 for step in range(50, 91))})
    chosen = max(candidates, key=lambda t: (assess(train, t)["balanced_accuracy"], -abs(t - baseline)))
    before, after = assess(validation, baseline), assess(validation, chosen)
    report["validation"] = {"baseline": before, "candidate": after}
    report["baseline_threshold"] = baseline
    if (after["balanced_accuracy"] - before["balanced_accuracy"] < 0.05
            or after["fp"] > before["fp"] or after["fn"] > before["fn"]):
        return stop("no_validated_improvement: require 0.05 balanced accuracy gain with no FP/FN increase")
    report["suggestions"] = [{"parameter": "strong_addressivity_threshold", "value": chosen,
                              "baseline": baseline, "samples": len(eligible)}]
    return stop("offline_candidate_requires_full_replay_before_adoption")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exports", nargs="+", type=Path)
    args = parser.parse_args()
    records = []
    try:
        for path in args.exports:
            records.extend(records_from(json.loads(path.read_text(encoding="utf-8-sig"))))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(suggest(records), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
