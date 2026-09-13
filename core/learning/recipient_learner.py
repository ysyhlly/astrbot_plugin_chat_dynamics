"""Which recipient evidence is pulling the decision the wrong way.

This is the shadow half of Dynamics Learning. It fits a small logistic model
over the recorded evidence of labelled recipient decisions and reports which
factors are under- or over-weighted. It returns a recommendation and nothing
else: no configuration is written, no threshold is moved, and a live decision
never consults this module.

The fit is deliberately plain. Records are labelled "should the bot have been
addressed", features are the raw factor values already stored beside each
decision, and the model only has to tell joint effect apart from the marginal
difference the counts already show. That is enough to say "this factor is
systematically high when the router is wrong", which is the question worth
answering before any weight is touched.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import exp

from .sample import LearningSample
from .stats import factor_disagreement

BOT = "bot"
UNDER = "under_weighted"
OVER = "over_weighted"
INCONCLUSIVE = "inconclusive"
MIN_SAMPLES = 100
MIN_SUPPORT = 10


@dataclass(frozen=True)
class FactorFinding:
    code: str
    support: int
    mean_wrong: float
    mean_right: float
    weight: float
    direction: str


@dataclass(frozen=True)
class Recommendation:
    """A proposal for a human to accept or ignore; never applied here."""

    task: str
    ready: bool
    samples: int
    required_samples: int
    factors: tuple[FactorFinding, ...]
    notes: tuple[str, ...]

    def render(self) -> str:
        if not self.ready:
            return (f"[{self.task}] 样本不足：{self.samples}/{self.required_samples}，"
                    f"暂不建议调整权重")
        lines = [f"[{self.task}] 基于 {self.samples} 条标注样本的影子分析（未应用）"]
        for item in self.factors:
            if item.direction == INCONCLUSIVE:
                continue
            label = "权重不足" if item.direction == UNDER else "权重偏高"
            comparison = "<" if item.direction == UNDER else ">"
            lines.append(
                f"    {item.code:32s} {label}  "
                f"判错均值 {item.mean_wrong:.3f} {comparison} 判对均值 {item.mean_right:.3f}  "
                f"拟合权重 {item.weight:+.3f} (n={item.support})")
        for note in self.notes:
            lines.append(f"    {note}")
        return "\n".join(lines)


def _labels(samples: list[LearningSample]) -> list[float]:
    return [1.0 if sample.expected == BOT else 0.0 for sample in samples]


def _matrix(samples: list[LearningSample], codes: list[str]) -> list[list[float]]:
    return [[sample.feature_map().get(code, 0.0) for code in codes] for sample in samples]


def fit_logistic(rows: list[list[float]], labels: list[float], *, iterations: int = 400,
                 rate: float = 0.35, l2: float = 1e-3) -> list[float]:
    """Deterministic gradient descent; returns weights plus a leading intercept."""
    width = len(rows[0]) if rows else 0
    weights = [0.0] * (width + 1)
    if not rows:
        return weights
    for _ in range(max(0, iterations)):
        gradient = [0.0] * len(weights)
        for row, label in zip(rows, labels):
            score = weights[0] + sum(w * x for w, x in zip(weights[1:], row))
            error = label - 1.0 / (1.0 + exp(-max(-30.0, min(30.0, score))))
            gradient[0] += error
            for index, value in enumerate(row):
                gradient[index + 1] += error * value
        scale = 1.0 / len(rows)
        weights[0] += rate * gradient[0] * scale
        for index in range(1, len(weights)):
            penalty = l2 * weights[index]
            weights[index] += rate * (gradient[index] * scale - penalty)
    return weights


def _direction(marginal: float, weight: float, *, support: int, min_support: int) -> str:
    if support < min_support:
        return INCONCLUSIVE
    # A factor is over-weighted when it is higher on the decisions that were
    # wrong and the model gives it a negative joint effect; under-weighted is
    # the mirrored case. Anything else is not worth acting on.
    if marginal > 0 and weight < 0:
        return OVER
    if marginal < 0 and weight > 0:
        return UNDER
    return INCONCLUSIVE


def analyze_recipient(samples: list[LearningSample], *, min_samples: int = MIN_SAMPLES,
                      min_support: int = MIN_SUPPORT) -> Recommendation:
    """Report factor direction for "should the bot have been addressed"."""
    rows = [sample for sample in samples if sample.task == "recipient"]
    marginal = {row["code"]: row for row in factor_disagreement(rows, min_support=min_support)}
    if len(rows) < min_samples:
        return Recommendation("recipient", False, len(rows), min_samples, (), (
            "样本不足时不给出方向性建议，避免用手选样本拟合噪声",))
    codes = sorted(marginal)
    weights = fit_logistic(_matrix(rows, codes), _labels(rows))
    findings = tuple(
        FactorFinding(code, marginal[code]["support"], marginal[code]["mean_wrong"],
                      marginal[code]["mean_right"], weights[index + 1],
                      _direction(marginal[code]["delta"], weights[index + 1],
                                 support=marginal[code]["support"], min_support=min_support))
        for index, code in enumerate(codes))
    ordered = tuple(sorted(findings, key=lambda item: abs(item.weight), reverse=True))
    actionable = [item for item in ordered if item.direction != INCONCLUSIVE]
    notes = (f"可行动因子 {len(actionable)} 个；其余因子方向不明确，不建议调整",)
    return Recommendation("recipient", True, len(rows), min_samples, ordered, notes)
