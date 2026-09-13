"""Learn what the current policy is getting wrong, not what the truth looks like.

The previous version fitted "is the bot the addressee" and then read the
coefficients as if they were statements about the router's weights. They are
not. "What predicts the answer" and "which of the router's weights is too large"
are different questions, and a false positive and a false negative call for
opposite corrections — so collapsing both into "wrong" produced a number that
could not be acted on.

This version fits the **residual**. The recorded policy decision enters the model
with a fixed weight, so everything the fit does learn is a *correction* on top of
the policy that already ran. That correction is then replayed against the same
labels on a **session-grouped** holdout and scored as FN / FP / precision /
recall, and only a Pareto improvement over the recorded policy is reported as
one.

Nothing here writes configuration, moves a threshold, or is consulted by a live
decision. A shadow policy is a measurement, not a change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp

from .sample import LearningSample
from .stats import outcome_bucket

BOT = "bot"
UNDER_USED = "under_used"
OVER_USED = "over_used"
INCONCLUSIVE = "inconclusive"

# Admission. A hundred rows can be ninety-five of one class from a single group,
# so both classes, both error directions and several groups are required.
MIN_SAMPLES = 100
MIN_PER_CLASS = 30
MIN_ERRORS = 10
MIN_GROUPS = 3

# Weight on the recorded policy decision. Fixed, never fitted: it is what makes
# the learned weights a correction rather than a re-derivation.
POLICY_OFFSET = 2.0
DEFAULT_THRESHOLD = 0.5

# How much a shadow policy may lose on one metric while gaining on another.
PARETO_SLACK = 0.02


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + exp(-max(-30.0, min(30.0, value))))


@dataclass(frozen=True)
class DecisionOutcome:
    """Confusion counts for one decision rule, with undefined ratios left None."""

    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    @staticmethod
    def _ratio(numerator: int, denominator: int):
        return numerator / denominator if denominator else None

    @property
    def support(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def precision(self):
        return self._ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self):
        return self._ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self):
        precision, recall = self.precision, self.recall
        if not precision or not recall:
            return None
        return 2 * precision * recall / (precision + recall)

    @property
    def accuracy(self):
        return self._ratio(self.tp + self.tn, self.support)

    def as_dict(self) -> dict:
        return {"tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn,
                "support": self.support, "precision": self.precision,
                "recall": self.recall, "f1": self.f1, "accuracy": self.accuracy}


@dataclass(frozen=True)
class FactorFinding:
    code: str
    support: int
    coverage: float
    correction: float
    direction: str
    buckets: dict = field(default_factory=dict)

    def _bucket(self, key: str) -> float:
        return float((self.buckets.get(key) or {}).get("mean", 0.0))

    @property
    def mean_false_positive(self) -> float:
        return self._bucket("fp")

    @property
    def mean_false_negative(self) -> float:
        return self._bucket("fn")

    @property
    def mean_true_positive(self) -> float:
        return self._bucket("tp")

    @property
    def mean_true_negative(self) -> float:
        return self._bucket("tn")

    def as_dict(self) -> dict:
        return {"code": self.code, "support": self.support, "coverage": self.coverage,
                "correction": self.correction, "direction": self.direction,
                "mean_fp": self.mean_false_positive, "mean_fn": self.mean_false_negative,
                "mean_tp": self.mean_true_positive, "mean_tn": self.mean_true_negative,
                "buckets": self.buckets}


@dataclass(frozen=True)
class ShadowPolicy:
    """A candidate correction, measured against the recorded policy. Never applied."""

    columns: tuple
    weights: tuple
    intercept: float
    threshold: float
    train: DecisionOutcome
    holdout: DecisionOutcome
    policy_holdout: DecisionOutcome
    improved: bool
    notes: tuple = ()

    def as_dict(self) -> dict:
        return {"columns": list(self.columns), "weights": list(self.weights),
                "intercept": self.intercept, "threshold": self.threshold,
                "train": self.train.as_dict(), "holdout": self.holdout.as_dict(),
                "policy_holdout": self.policy_holdout.as_dict(),
                "improved": self.improved, "notes": list(self.notes)}


@dataclass(frozen=True)
class Recommendation:
    """A proposal for a human to read; nothing here is ever applied."""

    task: str
    ready: bool
    samples: int
    required_samples: int
    findings: tuple = ()
    shadow: ShadowPolicy | None = None
    reasons: tuple = ()
    notes: tuple = ()

    @staticmethod
    def _fmt(value) -> str:
        return "—" if value is None else f"{value:.3f}"

    def render(self) -> str:
        lines: list = []
        if not self.ready:
            lines.append(f"[{self.task}] 样本不满足准入条件：{self.samples} 条已标注（需要 "
                         f"{self.required_samples}）")
            lines += [f"    - {reason}" for reason in self.reasons]
            return chr(10).join(lines)
        lines.append(f"[{self.task}] 基于 {self.samples} 条标注样本的残差分析（未应用）")
        for item in self.findings:
            if item.direction == INCONCLUSIVE:
                continue
            label = "权重不足" if item.direction == UNDER_USED else "权重偏高"
            lines.append(
                f"    {item.code:32s} {label}  修正权重 {item.correction:+.3f}  "
                f"FP均值 {item.mean_false_positive:.3f}  FN均值 {item.mean_false_negative:.3f}  "
                f"TP均值 {item.mean_true_positive:.3f}  (n={item.support})")
        if self.shadow is not None:
            policy, shadow = self.shadow.policy_holdout, self.shadow.holdout
            lines.append(f"    影子策略留出集（按会话分组）"
                         f"{'优于' if self.shadow.improved else '未优于'}当前策略：")
            lines.append(f"        当前策略  P={self._fmt(policy.precision)} "
                         f"R={self._fmt(policy.recall)} F1={self._fmt(policy.f1)} "
                         f"FP={policy.fp} FN={policy.fn}")
            lines.append(f"        影子策略  P={self._fmt(shadow.precision)} "
                         f"R={self._fmt(shadow.recall)} F1={self._fmt(shadow.f1)} "
                         f"FP={shadow.fp} FN={shadow.fn}")
        lines += [f"    {note}" for note in self.notes]
        return chr(10).join(lines)


def feature_columns(samples: list) -> list:
    """Codes observed somewhere in the corpus, sorted for a stable column order."""
    return sorted({code for sample in samples for code, _ in sample.features})


def build_matrix(samples: list, columns: list):
    """Value column per code, plus a presence column where coverage is partial.

    A code that was never observed is not a zero. Giving the model both the value
    and whether the value was observed lets it tell is_question = False apart
    from "is_question was never collected", which otherwise turns "this group
    keeps no message text" into a behavioural difference. Codes present on every
    row need no presence column: theirs would be constant and is already
    absorbed by the intercept.
    """
    total = len(samples)
    names: list = []
    plan: list = []
    for code in columns:
        covered = sum(1 for sample in samples if code in sample.present)
        plan.append((code, len(names), False))
        names.append(code)
        if 0 < covered < total:
            plan.append((code, len(names), True))
            names.append(f"{code}#present")
    rows: list = []
    for sample in samples:
        present = sample.present
        features = sample.feature_map()
        row = [0.0] * len(names)
        for code, index, is_presence in plan:
            if is_presence:
                row[index] = 1.0 if code in present else 0.0
            else:
                row[index] = float(features.get(code, 0.0))
        rows.append(row)
    return rows, names


def policy_decision(sample: LearningSample, *, positive: str = BOT) -> float:
    """1.0 when the recorded policy chose the positive class, else 0.0."""
    return 1.0 if sample.predicted == positive else 0.0


def fit_logistic(rows: list, labels: list, *, offsets: list | None = None,
                 iterations: int = 400, rate: float = 0.35, l2: float = 1e-3) -> list:
    """Deterministic gradient descent; returns weights plus a leading intercept.

    "offsets" are added to every score with a fixed weight of 1. Passing the
    recorded policy decision here is what turns the fit into a correction.
    """
    width = len(rows[0]) if rows else 0
    weights = [0.0] * (width + 1)
    if not rows:
        return weights
    fixed = list(offsets) if offsets is not None else [0.0] * len(rows)
    for _ in range(max(0, iterations)):
        gradient = [0.0] * len(weights)
        for row, label, offset in zip(rows, labels, fixed):
            score = weights[0] + offset + sum(w * x for w, x in zip(weights[1:], row))
            error = label - _sigmoid(score)
            gradient[0] += error
            for index, value in enumerate(row):
                gradient[index + 1] += error * value
        scale = 1.0 / len(rows)
        weights[0] += rate * gradient[0] * scale
        for index in range(1, len(weights)):
            penalty = l2 * weights[index]
            weights[index] += rate * (gradient[index] * scale - penalty)
    return weights


def evaluate_decisions(decisions: list, labels: list) -> DecisionOutcome:
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for decision, label in zip(decisions, labels):
        predicted = decision >= 0.5
        expected = label >= 0.5
        counts[("t" if predicted == expected else "f") + ("p" if predicted else "n")] += 1
    return DecisionOutcome(**counts)


def split_by_group(samples: list, *, holdout_ratio: float = 0.34):
    """Index split by session, never by row.

    Rows from one conversation are not independent, so a row-level split lets a
    model memorise a group and then look good on its neighbour.
    """
    groups = sorted({sample.group_key for sample in samples})
    if not groups or "" in groups or len(groups) < 2:
        return list(range(len(samples))), []
    wanted = max(1, min(len(groups) - 1, int(round(len(groups) * holdout_ratio))))
    stride = len(groups) / wanted
    holdout_groups = {groups[min(len(groups) - 1, int(index * stride))] for index in range(wanted)}
    train = [index for index, sample in enumerate(samples) if sample.group_key not in holdout_groups]
    holdout = [index for index, sample in enumerate(samples) if sample.group_key in holdout_groups]
    return train, holdout


def choose_threshold(scores: list, labels: list) -> float:
    """The cut maximising F1 on the given rows; ties go to the lower threshold."""
    candidates = sorted({0.0, 1.0, *[round(min(1.0, max(0.0, score)), 4) for score in scores]})
    best = None
    for threshold in candidates:
        outcome = evaluate_decisions([1.0 if score >= threshold else 0.0 for score in scores], labels)
        f1 = outcome.f1
        if f1 is None:
            continue
        if best is None or f1 > best[0] + 1e-9:
            best = (f1, threshold)
    return DEFAULT_THRESHOLD if best is None else best[1]


def _admission(samples: list, *, min_samples: int, min_per_class: int,
               min_errors: int, min_groups: int) -> list:
    problems: list = []
    if len(samples) < min_samples:
        problems.append(f"样本 {len(samples)} < {min_samples}")
    positive = sum(1 for sample in samples if sample.expected == BOT)
    negative = len(samples) - positive
    if positive < min_per_class or negative < min_per_class:
        problems.append(f"类别不平衡：bot {positive} / other {negative}，两侧各需 {min_per_class}")
    counts = {"fp": 0, "fn": 0}
    for sample in samples:
        bucket = outcome_bucket(sample)
        if bucket in counts:
            counts[bucket] += 1
    for key, label in (("fp", "误触发"), ("fn", "漏识别")):
        if counts[key] < min_errors:
            problems.append(f"{label}（{key}）只有 {counts[key]} 条，需要 {min_errors} 条")
    groups = {sample.group_key for sample in samples if sample.group_key}
    if len(groups) < min_groups:
        problems.append(f"只覆盖 {len(groups)} 个会话，需要 {min_groups} 个才能按会话分组验证")
    return problems


def _findings(samples: list, weights: list, columns: list, *, min_support: int) -> tuple:
    """Turn correction weights into statements about the residual."""
    from .stats import factor_disagreement

    stats = {row["code"]: row for row in factor_disagreement(samples, min_support=min_support)}
    total = len(samples) or 1
    findings: list = []
    for index, code in enumerate(columns):
        row = stats.get(code)
        if row is None:
            continue
        correction = weights[index + 1] if index + 1 < len(weights) else 0.0
        buckets = row.get("buckets", {})

        def mean(key: str) -> float:
            return float((buckets.get(key) or {}).get("mean", 0.0))

        direction = INCONCLUSIVE
        # A direction needs the weight *and* the bucket pattern behind it. Both
        # comparisons hold the prediction fixed and let the label vary, which is
        # the only way the factor can be shown to separate the case the policy
        # got wrong from the one it got right:
        #   under-used — both were called "other"; this is high on the missed ones
        #   over-used  — both were called "bot";   this is high on the wrong ones
        if correction > 0 and mean("fn") > mean("tn"):
            direction = UNDER_USED
        elif correction < 0 and mean("fp") > mean("tp"):
            direction = OVER_USED
        findings.append(FactorFinding(code, row["support"], row["support"] / total,
                                      correction, direction, buckets))
    return tuple(sorted(findings, key=lambda item: abs(item.correction), reverse=True))


def _pareto(policy: DecisionOutcome, shadow: DecisionOutcome):
    """A shadow policy must not buy one metric with another."""
    if shadow.f1 is None or policy.f1 is None:
        return False, "F1 未定义，无法比较"
    if shadow.f1 <= policy.f1:
        return False, f"F1 未提升（{shadow.f1:.3f} <= {policy.f1:.3f}）"
    for name, before, after in (("precision", policy.precision, shadow.precision),
                                ("recall", policy.recall, shadow.recall)):
        if before is None or after is None:
            return False, f"{name} 未定义，无法比较"
        if after < before - PARETO_SLACK:
            return False, f"{name} 回退 {before - after:.3f}（允许 {PARETO_SLACK:.2f}）"
    return True, f"F1 {policy.f1:.3f} -> {shadow.f1:.3f}，precision/recall 均未明显回退"


def analyze_recipient(samples: list, *, min_samples: int = MIN_SAMPLES,
                      min_support: int = 6, min_per_class: int = MIN_PER_CLASS,
                      min_errors: int = MIN_ERRORS,
                      min_groups: int = MIN_GROUPS) -> Recommendation:
    """Report the residual correction for "should the bot have been addressed"."""
    rows = [sample for sample in samples if sample.task == "recipient"]
    problems = _admission(rows, min_samples=min_samples, min_per_class=min_per_class,
                          min_errors=min_errors, min_groups=min_groups)
    if problems:
        return Recommendation("recipient", False, len(rows), min_samples, reasons=tuple(problems),
                              notes=("准入条件不满足时不给出方向性建议，"
                                     "避免用手选样本拟合噪声",))

    labels = [1.0 if sample.expected == BOT else 0.0 for sample in rows]
    policy = [policy_decision(sample) for sample in rows]
    columns = feature_columns(rows)
    matrix, names = build_matrix(rows, columns)
    offsets = [POLICY_OFFSET * (2 * value - 1) for value in policy]

    train_index, holdout_index = split_by_group(rows)
    if not holdout_index:
        return Recommendation("recipient", False, len(rows), min_samples,
                              reasons=("没有可用的会话分组，无法切出留出集",),
                              notes=("按行切分会让同一段对话同时出现在训练与验证里",))
    train_labels = [labels[index] for index in train_index]
    weights = fit_logistic([matrix[index] for index in train_index], train_labels,
                           offsets=[offsets[index] for index in train_index])

    def scores_for(indexes: list) -> list:
        return [weights[0] + offsets[index] + sum(w * x for w, x in zip(weights[1:], matrix[index]))
                for index in indexes]

    threshold = choose_threshold(scores_for(train_index), train_labels)
    holdout_labels = [labels[index] for index in holdout_index]
    shadow = ShadowPolicy(
        columns=tuple(names), weights=tuple(weights[1:]), intercept=weights[0],
        threshold=threshold,
        train=evaluate_decisions([score >= threshold for score in scores_for(train_index)],
                                 train_labels),
        holdout=evaluate_decisions([score >= threshold for score in scores_for(holdout_index)],
                                   holdout_labels),
        policy_holdout=evaluate_decisions([policy[index] for index in holdout_index],
                                          holdout_labels),
        improved=False,
        notes=("影子策略是对已记录特征的线性重打分，不是路由器重跑："
               "它不重新做 embedding、不重新检索父消息，因此它是可比较的代理，不是线上准确率",),
    )
    improved, reason = _pareto(shadow.policy_holdout, shadow.holdout)
    shadow = ShadowPolicy(**{**vars(shadow), "improved": improved,
                             "notes": shadow.notes + (reason,)})
    findings = _findings(rows, weights, columns, min_support=min_support)
    actionable = [item for item in findings if item.direction != INCONCLUSIVE]
    notes = (f"可行动因子 {len(actionable)} 个；其余因子方向不明确，不建议调整",
             "影子策略只在留出集上比较；未达到 Pareto 改进时不得作为调参依据")
    return Recommendation("recipient", True, len(rows), min_samples, findings, shadow,
                          notes=notes)
