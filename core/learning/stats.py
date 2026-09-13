"""What the labelled decisions actually look like, per task and per factor.

Accuracy over a hand-picked sample is a description of the sample, not of the
plugin, so every number here says how many rows produced it.

Two things the factor table must not do:

* **Mix tasks.** One annotation produces a topic, a recipient and a participation
  row that share a single feature vector, so a per-task error is only meaningful
  inside that task. A code can be wrong for topic and right for recipient on the
  same message; averaging them yields a number that describes nothing.
* **Count absence as zero.** A feature that was never observed is not in the map,
  and it is left out of the means with its coverage reported instead. Reading a
  missing fact.is_question as False would turn "this group keeps no message
  text" into a behavioural difference.

False positives and false negatives are reported in separate buckets, because
they call for opposite corrections. Collapsing both into "wrong" is what makes a
mean difference uninterpretable.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .sample import TASKS, LearningSample

SAMPLE_NOTE = "仅统计已标注样本，不代表真实准确率；权重未经概率校准"

# Tasks whose decision is a yes/no, with the full vocabulary each side uses.
# Membership is checked against both labels: a true negative names the positive
# class nowhere, so "does the positive class appear?" would misclassify it.
BINARY_TASKS = {"recipient": ("bot", "other"), "participation": ("reply", "silent")}
POSITIVE_CLASS = {task: pair[0] for task, pair in BINARY_TASKS.items()}
OUTCOME_KEYS = ("tp", "fp", "tn", "fn")


def _mean(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def outcome_bucket(sample: LearningSample) -> str:
    """tp/fp/tn/fn for a yes/no task, else right/wrong.

    The first letter is whether the decision was right; the second is which way
    it went. A prediction of the positive class that the label contradicts is a
    false **positive**, not a false negative. Getting that pair the wrong way
    round silently swaps the two error kinds the whole learner is built on.
    """
    vocabulary = BINARY_TASKS.get(sample.task)
    if vocabulary is None or sample.predicted not in vocabulary or sample.expected not in vocabulary:
        return "right" if sample.correct else "wrong"
    positive = vocabulary[0]
    return ("t" if sample.correct else "f") + ("p" if sample.predicted == positive else "n")


def factor_disagreement(samples: list, *, min_support: int = 4) -> list:
    """Per-code means over each outcome bucket, ranked by the wrong/right gap.

    mean_wrong and mean_right stay for the tasks that only have those two
    buckets. Binary tasks additionally carry a "buckets" map so a reader can see
    a code that is high on false positives and low on false negatives — two
    problems with opposite fixes — instead of one averaged number.
    """
    per_code: dict = defaultdict(lambda: defaultdict(list))
    for sample in samples:
        bucket = outcome_bucket(sample)
        for code, value in sample.features:
            per_code[code][bucket].append(value)
    total = len(samples)
    rows: list = []
    for code, buckets in per_code.items():
        wrong = buckets.get("wrong", []) + buckets.get("fp", []) + buckets.get("fn", [])
        right = buckets.get("right", []) + buckets.get("tp", []) + buckets.get("tn", [])
        support = len(wrong) + len(right)
        if support < min_support or not wrong or not right:
            continue
        mean_wrong, mean_right = _mean(wrong), _mean(right)
        rows.append({
            "code": code,
            "support": support,
            "coverage": support / total if total else 0.0,
            "wrong_n": len(wrong),
            "right_n": len(right),
            "mean_wrong": mean_wrong,
            "mean_right": mean_right,
            "delta": mean_wrong - mean_right,
            "buckets": {key: {"n": len(buckets.get(key, [])), "mean": _mean(buckets.get(key, []))}
                        for key in OUTCOME_KEYS + ("right", "wrong")
                        if buckets.get(key)},
        })
    rows.sort(key=lambda row: abs(row["delta"]), reverse=True)
    return rows


def _task_block(rows: list, *, min_support: int) -> dict:
    wrong = [s for s in rows if not s.correct]
    errors = Counter(s.error_type or "unspecified" for s in wrong)
    block = {
        "total": len(rows),
        "correct": len(rows) - len(wrong),
        "accuracy": (len(rows) - len(wrong)) / len(rows),
        "error_counts": dict(errors.most_common()),
        "confidence_when_right": _mean([s.confidence for s in rows if s.correct]),
        "confidence_when_wrong": _mean([s.confidence for s in wrong]),
        "factors": factor_disagreement(rows, min_support=min_support),
    }
    if rows[0].task in BINARY_TASKS:
        counts = Counter(outcome_bucket(s) for s in rows)
        block["outcomes"] = {key: counts.get(key, 0) for key in OUTCOME_KEYS}
    block["policy_versions"] = dict(Counter(s.policy_version or "unknown" for s in rows).most_common())
    return block


def summarize(samples: list, *, min_support: int = 4) -> dict:
    """Per-task accuracy, error mix and factor table, each with its row counts."""
    tasks: dict = {}
    for task in TASKS:
        rows = [s for s in samples if s.task == task]
        if not rows:
            continue
        tasks[task] = _task_block(rows, min_support=min_support)
    return {
        "sample_note": SAMPLE_NOTE,
        "total": len(samples),
        "tasks": tasks,
        "grouping": _grouping_block(samples),
    }


def _grouping_block(samples: list) -> dict:
    """Whether a session-grouped split is even possible for this corpus."""
    groups = {s.group_key for s in samples if s.group_key}
    ungroupable = sum(1 for s in samples if not s.group_key)
    note = ""
    if not groups:
        note = "没有任何样本带 session 标识，无法做按会话分组的验证"
    elif len(groups) < 2:
        note = "样本只来自一个会话，训练集与验证集无法按会话分开"
    elif ungroupable:
        note = f"{ungroupable} 条样本没有 session 标识，分组验证会丢掉它们"
    return {"groups": len(groups), "ungroupable": ungroupable, "note": note}
