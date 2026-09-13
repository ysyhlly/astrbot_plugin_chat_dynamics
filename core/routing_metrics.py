"""Label-aware offline metrics; missing supervision never means a negative."""
from itertools import combinations

from .candidate_metrics import candidate_metrics


def known_label(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip().upper() != "UNKNOWN"


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _accepted_topics(row, index, rows):
    """Topic ids a free-text label accepts, read off earlier labelled members.

    A label like "hardware" is not a topic id. It becomes interpretable only
    through rows that carry both the label and the topic the router chose, so a
    label with no such member yields no truth and the row is unattributable
    rather than wrong.
    """
    label = row.get("expected_topic")
    if not known_label(label):
        return ()
    return tuple(sorted({str(previous.get("topic_id")) for previous in rows[:index]
                         if previous.get("expected_topic") == label
                         and known_label(previous.get("topic_id"))}))


def routing_metrics(sessions):
    """Compare labels only within sessions and only through equality relations."""
    sessions = [list(rows or ()) for rows in sessions]
    tp = merge = fragment = pairs = 0
    parent_total = parent_correct = parent_covered = 0
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
    candidates = candidate_metrics(sessions, truth_of=_accepted_topics)
    return {"topic": {"pairs": pairs, "true_positive": tp, "wrong_merge": merge,
                      "fragmentation": fragment, "precision": ratio(tp, tp + merge),
                      "recall": ratio(tp, tp + fragment),
                      "candidate_recall": candidates["recall"],
                      "candidate_selection": candidates["selection"],
                      "candidate_attribution": candidates["outcomes"],
                      "candidate_coverage": {
                          "rows": candidates["rows"],
                          "recorded": candidates["recorded"],
                          "attributable": candidates["attributable"],
                          "lengths": candidates["candidate_lengths"],
                          "dropped_entries": candidates["dropped_entries"],
                          "notes": candidates["notes"]}},
            "parent": {"labeled": parent_total, "correct": parent_correct,
                       "accuracy": ratio(parent_correct, parent_total),
                       "coverage": ratio(parent_covered, parent_total)}}
