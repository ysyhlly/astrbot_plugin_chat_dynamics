"""The learner must fit what the policy got wrong, and prove it on unseen groups.

Three properties matter and none of them is "the coefficient looks plausible":

* the fit is a **correction** on top of the recorded policy, so the weights say
  what to change rather than re-deriving what is already in force;
* a direction needs the weight **and** the bucket pattern behind it — a factor
  high on false negatives is a different finding from one high on false
  positives, and the two call for opposite corrections;
* the correction is only worth reading if it survives a **session-grouped**
  replay and improves F1 without buying it with precision or recall.
"""
from __future__ import annotations

import pytest

from astrbot_plugin_chat_dynamics.core.learning import (
    DecisionOutcome,
    LearningSample,
    analyze_recipient,
    build_matrix,
    evaluate_decisions,
    feature_columns,
    fit_logistic,
    split_by_group,
)
from astrbot_plugin_chat_dynamics.core.learning.recipient_learner import (
    INCONCLUSIVE,
    OVER_USED,
    POLICY_OFFSET,
    UNDER_USED,
    _pareto,
    policy_decision,
)

RELAXED = {"min_samples": 16, "min_per_class": 6, "min_errors": 4, "min_groups": 3,
           "min_support": 3}


def sample(marker, predicted, expected, *, explicit, interlocutor, group="s",
           task="recipient") -> LearningSample:
    return LearningSample(
        session_id=group, message_id=marker, timestamp=1.0, task=task,
        predicted=predicted, expected=expected, confidence=0.6, source="manual_replay",
        features=(("explicit_reply", explicit), ("active_interlocutor", interlocutor)))


def skewed_corpus(*, groups=4, per_type=3):
    """The router trusts an interruption cue and ignores an explicit reply.

    a — truly addressed, router said another user (false negative)
    b — truly another user, router said the bot (false positive)
    c — truly another user, router agreed (true negative)
    d — truly addressed, router agreed (true positive)
    """
    rows = []
    for index in range(groups):
        group = f"g{index}"
        for position in range(per_type):
            rows.append(sample(f"a{index}-{position}", "other", "bot",
                               explicit=1.0, interlocutor=0.1, group=group))
            rows.append(sample(f"b{index}-{position}", "bot", "other",
                               explicit=0.1, interlocutor=1.0, group=group))
            rows.append(sample(f"c{index}-{position}", "other", "other",
                               explicit=0.1, interlocutor=0.2, group=group))
            rows.append(sample(f"d{index}-{position}", "bot", "bot",
                               explicit=1.0, interlocutor=0.3, group=group))
    return rows


def test_learner_separates_the_direction_of_each_error():
    report = analyze_recipient(skewed_corpus(), **RELAXED)
    assert report.ready is True and report.samples == 48
    findings = {item.code: item for item in report.findings}
    # High on the turns the router missed, low on the ones it correctly ignored.
    missed = findings["explicit_reply"]
    assert missed.direction == UNDER_USED and missed.correction > 0
    assert missed.mean_false_negative > missed.mean_true_negative
    # High on the turns the router fired wrongly, lower on the ones it got right.
    wrong = findings["active_interlocutor"]
    assert wrong.direction == OVER_USED and wrong.correction < 0
    assert wrong.mean_false_positive > wrong.mean_true_positive
    text = report.render()
    assert "权重偏高" in text and "权重不足" in text and "未应用" in text


def test_false_positives_and_false_negatives_are_never_averaged_together():
    report = analyze_recipient(skewed_corpus(), **RELAXED)
    finding = {item.code: item for item in report.findings}["explicit_reply"]
    # The two buckets point in opposite directions on the same feature; a single
    # "wrong" mean would hide exactly that.
    assert finding.mean_false_negative == pytest.approx(1.0)
    assert finding.mean_false_positive == pytest.approx(0.1)
    assert finding.buckets["fp"]["n"] == 12 and finding.buckets["fn"]["n"] == 12


def test_the_shadow_policy_is_measured_on_held_out_sessions_only():
    report = analyze_recipient(skewed_corpus(), **RELAXED)
    shadow = report.shadow
    assert shadow is not None
    assert shadow.holdout.support + shadow.train.support == 48
    assert shadow.policy_holdout.fn > 0 and shadow.policy_holdout.fp > 0
    assert shadow.holdout.fn < shadow.policy_holdout.fn
    assert shadow.holdout.fp < shadow.policy_holdout.fp
    assert shadow.improved is True


def test_the_pareto_gate_refuses_a_gain_bought_with_a_regression():
    policy = DecisionOutcome(tp=10, fp=1, tn=30, fn=10)
    # F1 rises, but only by converting true negatives into false positives.
    bought = DecisionOutcome(tp=16, fp=6, tn=0, fn=4)
    improved, reason = _pareto(policy, bought)
    assert improved is False and "precision 回退" in reason
    good = DecisionOutcome(tp=18, fp=1, tn=30, fn=2)
    improved, reason = _pareto(policy, good)
    assert improved is True and "F1" in reason
    # A shadow policy that is merely different is not an improvement.
    assert _pareto(policy, DecisionOutcome(tp=8, fp=2, tn=29, fn=12))[0] is False


def test_admission_refuses_a_corpus_that_could_not_be_validated():
    thin = analyze_recipient(skewed_corpus()[:4], min_samples=100)
    assert thin.ready is False and thin.findings == ()
    assert "不满足准入条件" in thin.render()
    assert thin.required_samples == 100

    # A hundred rows of one class is still a corpus that says nothing.
    one_sided = [sample(f"s{i}", "bot", "bot", explicit=1.0, interlocutor=0.5, group=f"g{i % 4}")
                 for i in range(120)]
    report = analyze_recipient(one_sided, min_samples=100)
    assert report.ready is False
    assert any("类别不平衡" in reason for reason in report.reasons)

    # No false positives means there is nothing to correct on that side.
    no_fp = [row for row in skewed_corpus() if not row.message_id.startswith("b")]
    report = analyze_recipient(no_fp, **RELAXED)
    assert report.ready is False
    assert any("误触发" in reason for reason in report.reasons)

    # One session cannot be split into train and validation.
    single = skewed_corpus(groups=1, per_type=12)
    report = analyze_recipient(single, **{**RELAXED, "min_groups": 1})
    assert report.ready is False
    assert any("会话" in reason for reason in report.reasons)


def test_other_tasks_do_not_contaminate_the_recipient_fit():
    corpus = skewed_corpus() + [sample(f"t{i}", "t1", "t2", explicit=1.0, interlocutor=1.0,
                                       group="tg", task="topic") for i in range(10)]
    assert analyze_recipient(corpus, **RELAXED).samples == 48


def test_inconclusive_factors_stay_out_of_the_advice():
    # Every row agrees with the policy, so there is no residual to correct.
    rows = []
    for index in range(4):
        for position in range(6):
            rows.append(sample(f"a{index}-{position}", "bot", "bot", explicit=1.0,
                               interlocutor=1.0, group=f"g{index}"))
            rows.append(sample(f"b{index}-{position}", "other", "other", explicit=1.0,
                               interlocutor=1.0, group=f"g{index}"))
    report = analyze_recipient(rows, min_samples=16, min_per_class=6, min_errors=0,
                               min_groups=3, min_support=3)
    assert report.ready is True
    assert all(item.direction == INCONCLUSIVE for item in report.findings)
    assert "可行动因子 0 个" in " ".join(report.notes)


def test_the_policy_decision_enters_the_fit_with_a_fixed_weight():
    assert policy_decision(sample("x", "bot", "bot", explicit=1.0, interlocutor=1.0)) == 1.0
    assert policy_decision(sample("y", "other", "bot", explicit=1.0, interlocutor=1.0)) == 0.0
    rows, labels = [[1.0, 1.0]], [1.0]
    without = fit_logistic(rows, labels, iterations=200)
    with_offset = fit_logistic(rows, labels, offsets=[-POLICY_OFFSET], iterations=200)
    # A row the policy declared negative needs a larger positive correction.
    assert with_offset[1] > without[1]


def test_the_matrix_marks_absence_instead_of_calling_it_zero():
    rows = [
        LearningSample("s", "m1", 1.0, "recipient", "bot", "bot", 0.5, "t",
                       (("fact.is_question", 0.0), ("semantic", 0.4))),
        LearningSample("s", "m2", 1.0, "recipient", "bot", "bot", 0.5, "t",
                       (("semantic", 0.6),)),
    ]
    columns = feature_columns(rows)
    matrix, names = build_matrix(rows, columns)
    # fact.is_question is present on one row and absent on the other, so it gets
    # a presence column; semantic is on both and does not.
    assert "fact.is_question#present" in names
    assert "semantic#present" not in names
    question = names.index("fact.is_question")
    presence = names.index("fact.is_question#present")
    # Observed-and-false is distinguishable from never-observed.
    assert matrix[0][question] == 0.0 and matrix[0][presence] == 1.0
    assert matrix[1][question] == 0.0 and matrix[1][presence] == 0.0


def test_grouped_split_never_separates_rows_of_one_conversation():
    rows = skewed_corpus()
    train, holdout = split_by_group(rows)
    assert train and holdout
    assert {rows[i].group_key for i in train}.isdisjoint({rows[i].group_key for i in holdout})
    assert sorted(train + holdout) == list(range(len(rows)))


def test_a_corpus_without_grouping_cannot_be_split():
    rows = [sample(f"m{i}", "bot", "bot", explicit=1.0, interlocutor=1.0, group="unknown")
            for i in range(4)]
    assert split_by_group(rows)[1] == []
    assert analyze_recipient(rows, **RELAXED).ready is False


def test_confusion_counts_name_the_error_direction():
    outcome = evaluate_decisions([1.0, 1.0, 0.0, 0.0], [1.0, 0.0, 1.0, 0.0])
    assert (outcome.tp, outcome.fp, outcome.fn, outcome.tn) == (1, 1, 1, 1)
    assert outcome.precision == pytest.approx(0.5)
    assert outcome.recall == pytest.approx(0.5)
    empty = evaluate_decisions([], [])
    assert empty.support == 0 and empty.f1 is None and empty.accuracy is None


def test_fit_is_deterministic_and_learns_a_separable_sign():
    rows = [[1.0, 0.0], [0.9, 0.0], [0.0, 1.0], [0.1, 1.0]]
    labels = [1.0, 1.0, 0.0, 0.0]
    first = fit_logistic(rows, labels)
    assert first == fit_logistic(rows, labels)
    assert first[1] > 0 > first[2]


def test_fit_handles_an_empty_corpus():
    assert fit_logistic([], []) == [0.0]


@pytest.mark.parametrize("count", [0, 1, 7])
def test_short_corpora_never_report_ready(count):
    assert analyze_recipient(skewed_corpus()[:count], min_samples=8).ready is False


def test_nothing_in_the_learner_writes_anything():
    """A shadow policy is a measurement; this asserts it stays one."""
    import inspect

    from astrbot_plugin_chat_dynamics.core.learning import recipient_learner

    source = inspect.getsource(recipient_learner)
    for forbidden in ("put_kv_data", "save_config", "open(", "write_text", "json.dump"):
        assert forbidden not in source
