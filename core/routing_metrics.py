"""Label-aware offline metrics; missing supervision never means a negative."""
from itertools import combinations


def known_label(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip().upper() != "UNKNOWN"


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def routing_metrics(sessions):
    """Compare labels only within sessions and only through equality relations."""
    tp = merge = fragment = pairs = 0
    parent_total = parent_correct = parent_covered = 0
    recall = {k: [0, 0] for k in (1, 3, 5)}
    for rows in sessions:
        labeled = [row for row in rows if known_label(row.get("expected_topic"))]
        for left, right in combinations(labeled, 2):
            pairs += 1
            same_truth = left["expected_topic"] == right["expected_topic"]
            same_prediction = (known_label(left.get("topic_id")) and
                               left.get("topic_id") == right.get("topic_id"))
            tp += same_truth and same_prediction
            merge += not same_truth and same_prediction
            fragment += same_truth and not same_prediction
        for i, row in enumerate(rows):
            if "expected_parent" in row and (row["expected_parent"] is None or
                                               known_label(row["expected_parent"])):
                parent_total += 1
                predicted = row.get("parent_message_id") or None
                parent_correct += predicted == row["expected_parent"]
                parent_covered += predicted is not None
            # Candidate topic IDs are interpretable only through earlier labeled members.
            if not known_label(row.get("expected_topic")) or "topic_candidates" not in row:
                continue
            relevant = {previous["topic_id"] for previous in rows[:i]
                        if previous.get("expected_topic") == row["expected_topic"]
                        and known_label(previous.get("topic_id"))}
            if not relevant:
                continue
            candidates = [candidate[1] for candidate in row["topic_candidates"]]
            for k, counts in recall.items():
                counts[0] += bool(relevant.intersection(candidates[:k]))
                counts[1] += 1
    return {"topic": {"pairs": pairs, "true_positive": tp, "wrong_merge": merge,
                      "fragmentation": fragment, "precision": ratio(tp, tp + merge),
                      "recall": ratio(tp, tp + fragment),
                      "candidate_recall": {str(k): {"hits": h, "eligible": n, "value": ratio(h, n)}
                                           for k, (h, n) in recall.items()}},
            "parent": {"labeled": parent_total, "correct": parent_correct,
                       "accuracy": ratio(parent_correct, parent_total),
                       "coverage": ratio(parent_covered, parent_total)}}
