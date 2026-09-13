"""What the labelled decisions actually look like, per task and per factor.

Accuracy over a hand-picked sample is a description of the sample, not of the
plugin, so every number here says how many rows produced it. The factor table is
the useful part: it compares the raw evidence value on wrong decisions against
right ones, which is what turns "it was wrong" into "this factor carried it".
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .sample import TASKS, LearningSample

SAMPLE_NOTE = "仅统计已标注样本，不代表真实准确率；权重未经概率校准"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def factor_disagreement(samples: list[LearningSample], *, min_support: int = 4) -> list[dict]:
    """Mean raw factor value on wrong minus right decisions, ranked by magnitude."""
    per_code: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"wrong": [], "right": []})
    for sample in samples:
        side = "right" if sample.correct else "wrong"
        for code, value in sample.features:
            per_code[code][side].append(value)
    rows = []
    for code, sides in per_code.items():
        support = len(sides["wrong"]) + len(sides["right"])
        if support < min_support or not sides["wrong"] or not sides["right"]:
            continue
        wrong, right = _mean(sides["wrong"]), _mean(sides["right"])
        rows.append({"code": code, "support": support, "wrong_n": len(sides["wrong"]),
                     "right_n": len(sides["right"]), "mean_wrong": wrong, "mean_right": right,
                     "delta": wrong - right})
    rows.sort(key=lambda row: abs(row["delta"]), reverse=True)
    return rows


def summarize(samples: list[LearningSample], *, min_support: int = 4) -> dict:
    """Per-task accuracy and error mix plus the factor table, with row counts."""
    tasks: dict[str, dict] = {}
    for task in TASKS:
        rows = [s for s in samples if s.task == task]
        if not rows:
            continue
        wrong = [s for s in rows if not s.correct]
        errors = Counter(s.error_type or "unspecified" for s in wrong)
        tasks[task] = {
            "total": len(rows),
            "correct": len(rows) - len(wrong),
            "accuracy": (len(rows) - len(wrong)) / len(rows),
            "error_counts": dict(errors.most_common()),
            "confidence_when_right": _mean([s.confidence for s in rows if s.correct]),
            "confidence_when_wrong": _mean([s.confidence for s in wrong]),
        }
    return {
        "sample_note": SAMPLE_NOTE,
        "total": len(samples),
        "tasks": tasks,
        "factors": factor_disagreement(samples, min_support=min_support),
    }
