"""The learner must point at the factor that was pulling the wrong way."""
from __future__ import annotations

import pytest

from astrbot_plugin_chat_dynamics.core.learning import (
    LearningSample,
    analyze_recipient,
    fit_logistic,
)
from astrbot_plugin_chat_dynamics.core.learning.recipient_learner import (
    INCONCLUSIVE,
    OVER,
    UNDER,
)


def sample(marker: str, predicted: str, expected: str, *, explicit: float,
           interlocutor: float, task: str = "recipient") -> LearningSample:
    return LearningSample(
        session_id="s", message_id=marker, timestamp=1.0, task=task,
        predicted=predicted, expected=expected, confidence=0.6, source="manual_replay",
        features=(("explicit_reply", explicit), ("active_interlocutor", interlocutor)))


def skewed_corpus() -> list[LearningSample]:
    """The router trusts an interruption cue and ignores an explicit reply."""
    rows = []
    # Right, because the explicit reply was there anyway.
    rows += [sample(f"a{i}", "bot", "bot", explicit=1.0, interlocutor=0.1) for i in range(6)]
    # Wrong: the interruption cue is high, the real signal is absent, truth is another user.
    rows += [sample(f"b{i}", "bot", "other", explicit=0.1, interlocutor=1.0) for i in range(6)]
    # Right, and nothing pointed at the bot.
    rows += [sample(f"c{i}", "other", "other", explicit=0.1, interlocutor=0.2) for i in range(4)]
    return rows


def test_learner_finds_both_directions_and_applies_neither():
    report = analyze_recipient(skewed_corpus(), min_samples=8, min_support=4)
    assert report.ready is True and report.samples == 16
    findings = {item.code: item for item in report.factors}
    assert findings["active_interlocutor"].direction == OVER
    assert findings["active_interlocutor"].weight < 0
    assert findings["explicit_reply"].direction == UNDER
    assert findings["explicit_reply"].weight > 0
    text = report.render()
    assert "权重偏高" in text and "权重不足" in text and "未应用" in text


def test_learner_refuses_to_guess_on_a_thin_sample():
    report = analyze_recipient(skewed_corpus()[:4], min_samples=100)
    assert report.ready is False and report.factors == ()
    assert "样本不足" in report.render()
    assert report.required_samples == 100


def test_other_tasks_do_not_contaminate_the_recipient_fit():
    corpus = skewed_corpus() + [sample(f"t{i}", "t1", "t2", explicit=1.0, interlocutor=1.0,
                                       task="topic") for i in range(10)]
    assert analyze_recipient(corpus, min_samples=8, min_support=4).samples == 16


def test_inconclusive_factors_stay_out_of_the_advice():
    # Every sample agrees, so no factor separates right from wrong decisions.
    rows = [sample(f"a{i}", "bot", "bot", explicit=1.0, interlocutor=1.0) for i in range(5)]
    rows += [sample(f"b{i}", "bot", "other", explicit=1.0, interlocutor=1.0) for i in range(5)]
    report = analyze_recipient(rows, min_samples=8, min_support=4)
    assert all(item.direction == INCONCLUSIVE for item in report.factors)
    assert "可行动因子 0 个" in " ".join(report.notes)


def test_fit_is_deterministic_and_learns_a_separable_sign():
    rows = [[1.0, 0.0], [0.9, 0.0], [0.0, 1.0], [0.1, 1.0]]
    labels = [1.0, 1.0, 0.0, 0.0]
    first = fit_logistic(rows, labels)
    assert first == fit_logistic(rows, labels)
    assert first[1] > 0 > first[2]


def test_fit_handles_an_empty_corpus():
    # No rows means no feature weights: only the intercept slot exists.
    assert fit_logistic([], []) == [0.0]


@pytest.mark.parametrize("count", [0, 1, 7])
def test_short_corpora_never_report_ready(count):
    assert analyze_recipient(skewed_corpus()[:count], min_samples=8).ready is False
