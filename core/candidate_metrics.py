"""Split "the labelled topic was never offered" from "it was offered and lost".

Moved out of the deleted core/learning/ package in v1.7.0. It is not part of
that layer: it is the live metric the offline routing evaluator and the
annotation console read, and it has no dependency on the sample format, the
learner or the store that used to sit beside it.

Topic accuracy records that a decision was wrong. It cannot say which stage
failed, and the two failures need opposite fixes:

* candidate generation never proposed the labelled topic, so no reweighting of
  the scorer can recover it;
* generation proposed it and the ranking discarded it, so the retriever is not
  the problem.

One number that averages those two is not actionable, so they are counted apart
and the two metrics use deliberately different denominators:

* **Candidate Recall@K** — over rows whose labelled topic is identifiable and
  whose candidates were recorded at all. A row that recorded nothing is
  excluded rather than scored as a miss: not knowing is not evidence of
  failing, and the count of such rows is reported separately.
* **Selection Accuracy** — only over the rows where the labelled topic was
  actually offered. Conditioning on recall is what keeps the two from hiding
  each other: a high selection accuracy beside a low recall points at
  generation, and the reverse points at the scorer.

Nothing here scores a row it cannot attribute, and every number carries the
count of rows that produced it.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from .topic_candidates import parse_candidates

# What happened to one labelled topic decision.
SELECTED = "selected"
RANKING_ERROR = "ranking_error"
CANDIDATE_MISS = "candidate_miss"
NOT_RECORDED = "not_recorded"
NEW_TOPIC_EXPECTED = "new_topic_expected"
UNATTRIBUTABLE = "unattributable"
OUTCOMES = (SELECTED, RANKING_ERROR, CANDIDATE_MISS, NOT_RECORDED,
            NEW_TOPIC_EXPECTED, UNATTRIBUTABLE)
SCORED_OUTCOMES = (SELECTED, RANKING_ERROR, CANDIDATE_MISS)

# Labels that name no existing topic, so candidate membership is undefined:
# "was the right topic offered?" has no answer when no topic was right.
NO_TOPIC_LABELS = frozenset({"NEW", "UNKNOWN"})

DEFAULT_RECALL_K = (1, 3, 5)
SAMPLE_NOTE = "仅统计可归属的已标注样本；未记录候选的行不计入召回率"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _row_truth(row, index, rows) -> tuple:
    """Default truth: the row's own label is already a topic id."""
    del index, rows
    return (_text(row.get("expected_topic")),)


def _row_selected(row, index, rows) -> str:
    del index, rows
    return _text(row.get("topic_id"))


def _row_candidates(row, index, rows) -> object:
    del index, rows
    return row.get("topic_candidates")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _notes(recorded: int, lengths: Counter, dropped: int, outcomes: dict) -> list:
    """What a reader must know before trusting the numbers above."""
    lines: list = []
    if not recorded:
        lines.append("没有任何行记录 topic_candidates，候选生成与排序错误无法区分")
    if lengths and max(lengths) <= 3:
        lines.append("路由器最多记录 3 个候选，recall@5 与 recall@3 在截断改变前必然相同")
    if dropped:
        lines.append(f"{dropped} 个候选条目无法解析（缺少 topic_id 或结构不符），已忽略且不计入召回")
    if outcomes.get(NOT_RECORDED):
        lines.append(f"{outcomes[NOT_RECORDED]} 行没有记录候选，既不计入召回也不算候选生成失败")
    if outcomes.get(UNATTRIBUTABLE):
        lines.append(f"{outcomes[UNATTRIBUTABLE]} 行没有可归属的真值话题，未参与任何候选指标")
    return lines


def candidate_metrics(sessions, *, truth_of=_row_truth, selected_of=_row_selected,
                      candidates_of=_row_candidates, recall_k=DEFAULT_RECALL_K,
                      no_topic_labels=NO_TOPIC_LABELS) -> dict:
    """Attribution and both candidate metrics over rows grouped into sessions.

    ``truth_of`` returns every topic id the label accepts: one human label can
    map to several predicted ids, and a candidate matching any of them is a hit.
    An empty tuple means the row cannot be attributed; it is counted as such and
    never guessed at.
    """
    outcomes: dict = dict.fromkeys(OUTCOMES, 0)
    recall: dict = {str(k): {"hits": 0, "eligible": 0, "value": None} for k in sorted(recall_k)}
    selection: dict = {"correct": 0, "eligible": 0, "value": None}
    lengths: Counter = Counter()
    no_topic = {item.upper() for item in no_topic_labels}
    rows_seen = recorded = dropped = 0
    for group in sessions if sessions is not None else ():
        group = list(group or ())
        for index, row in enumerate(group):
            rows_seen += 1
            if not isinstance(row, Mapping):
                outcomes[UNATTRIBUTABLE] += 1
                continue
            label = _text(row.get("expected_topic"))
            if row.get("topic_reviewed") is False or label == "UNREVIEWED":
                outcomes[UNATTRIBUTABLE] += 1
                continue
            if label and label.upper() in no_topic:
                outcomes[NEW_TOPIC_EXPECTED] += 1
                continue
            accepted = {code for code in (_text(item) for item in truth_of(row, index, group)) if code}
            if not accepted:
                outcomes[UNATTRIBUTABLE] += 1
                continue
            parsed = parse_candidates(candidates_of(row, index, group))
            if not parsed.recorded:
                outcomes[NOT_RECORDED] += 1
                continue
            recorded += 1
            dropped += parsed.dropped
            lengths[len(parsed.items)] += 1
            for rank, counts in recall.items():
                counts["eligible"] += 1
                counts["hits"] += bool(accepted.intersection(parsed.top(int(rank))))
            if not accepted.intersection(parsed.ids):
                outcomes[CANDIDATE_MISS] += 1
                continue
            selection["eligible"] += 1
            if _text(selected_of(row, index, group)) in accepted:
                selection["correct"] += 1
                outcomes[SELECTED] += 1
            else:
                outcomes[RANKING_ERROR] += 1
    for counts in recall.values():
        counts["value"] = _ratio(counts["hits"], counts["eligible"])
    selection["value"] = _ratio(selection["correct"], selection["eligible"])
    attributable = sum(outcomes[key] for key in SCORED_OUTCOMES)
    return {
        "sample_note": SAMPLE_NOTE,
        "rows": rows_seen,
        "recorded": recorded,
        "attributable": attributable,
        "outcomes": outcomes,
        "recall": recall,
        "selection": selection,
        "candidate_lengths": {str(key): value for key, value in sorted(lengths.items())},
        "dropped_entries": dropped,
        "notes": _notes(recorded, lengths, dropped, outcomes),
    }


def _annotation_truth(record, index, rows) -> tuple:
    del index, rows
    return (_text(record.get("expected_topic")),)


def _annotation_selected(record, index, rows) -> str:
    del index, rows
    return _text(record.get("predicted_topic"))


def _annotation_candidates(record, index, rows) -> object:
    del index, rows
    routing = record.get("routing")
    return routing.get("topic_candidates") if isinstance(routing, Mapping) else None


def annotation_metrics(records) -> dict:
    """Candidate metrics from saved annotations, where the label is a topic id.

    The replay page refuses to save a label that names no known topic, so a
    stored `expected_topic` is already the truth id and needs no inference from
    earlier members — unlike the evaluation harness, whose fixtures carry free
    text. The two paths therefore differ only in how truth is resolved and
    share the metrics themselves.
    """
    return candidate_metrics([records], truth_of=_annotation_truth,
                             selected_of=_annotation_selected,
                             candidates_of=_annotation_candidates)
