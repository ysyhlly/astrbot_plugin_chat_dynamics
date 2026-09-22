"""The decision gate: uncertainty, not confidence, and why the difference is fatal.

`laya` reports `confidence` for a `noul` answer as `max(p, 1-p)`, which is always
at least 0.5. A "low confidence -> fall back" rule built on it is not merely
imprecise, it is inverted past 0.5: the gate opens when the model is unsure and
closes when it is confidently wrong. That is the opposite of a safety gate.

These tests hold the replacement to the properties that matter: one scale across
answer types, the most cautious signal wins, and anything unjudgeable is refused
rather than trusted.
"""
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.integrations.laya import (
    decision_uncertainty, decision_usable,
)


def close(got, want):
    """Float equality with a tolerance.

    `1.0 - 0.99` is 0.010000000000000009 in binary floating point, so comparing
    these against literals with `==` tests the representation rather than the rule.
    """
    assert got is not None, f"expected {want}, got None"
    assert abs(got - want) < 1e-9, f"{got!r} != {want!r}"


def test_noul_uncertainty_comes_from_the_probability():
    # p = 0.5 is maximal hesitation and must read as the worst case.
    close(decision_uncertainty({"type": "noul", "noul": 0.5}), 0.5)
    close(decision_uncertainty({"type": "noul", "noul": 0.9}), 0.1)
    close(decision_uncertainty({"type": "noul", "noul": 0.0}), 0.0)
    # Reported confidence is max(p, 1-p); 1 - that equals min(p, 1-p), so the two
    # routes agree and taking the max cannot double-count.
    close(decision_uncertainty({"type": "noul", "noul": 0.9, "confidence": 0.9}), 0.1)


def test_noul_confidence_cannot_fall_below_half_but_uncertainty_reaches_zero():
    # The whole trap in one assertion: a certain answer has confidence 1.0 and an
    # unsure one 0.5, so confidence never falls under 0.5 -- while uncertainty
    # spans the full 0..0.5 and can therefore be gated meaningfully.
    for p_true, expected in ((1.0, 0.0), (0.5, 0.5), (0.75, 0.25)):
        reported = max(p_true, 1 - p_true)
        assert reported >= 0.5, "the confidence floor that makes gating impossible"
        close(decision_uncertainty({"type": "noul", "noul": p_true, "confidence": reported}),
              expected)
        close(decision_uncertainty({"type": "noul", "noul": p_true}), expected)


def test_choice_uncertainty_comes_from_the_distribution():
    close(decision_uncertainty({"type": "choice", "probabilities": {"a": 0.6, "b": 0.4}}), 0.4)
    close(decision_uncertainty({"type": "choice", "probabilities": {"a": 1.0, "b": 0.0}}), 0.0)
    # Unnormalized distributions are normalised, not trusted at face value.
    close(decision_uncertainty({"type": "choice", "probabilities": {"a": 3.0, "b": 1.0}}), 0.25)


def test_the_most_cautious_signal_wins():
    # A one-hot distribution says "certain" while the reported confidence says
    # "unsure". A safety gate must refuse, so the larger uncertainty is taken.
    close(decision_uncertainty({"type": "choice", "probabilities": {"a": 1.0},
                               "confidence": 0.6}), 0.4)
    # And the reverse ordering must not matter.
    close(decision_uncertainty({"type": "choice", "probabilities": {"a": 1.0},
                               "confidence": 0.4}), 0.6)


def test_unjudgeable_is_refused_not_trusted():
    assert decision_uncertainty({"type": "noul"}) is None
    assert decision_uncertainty({"type": "noul", "noul": "yes"}) is None
    assert decision_uncertainty({"type": "choice"}) is None
    assert decision_uncertainty(None) is None
    # A nonsensical confidence is dropped, and the remaining signal still counts.
    close(decision_uncertainty({"type": "noul", "noul": 0.9, "confidence": 5.0}), 0.1)
    # A malformed answer is unusable, never a silent pass.
    assert decision_usable({"type": "noul"}, max_uncertainty=0.5) is False
    assert decision_usable(None, max_uncertainty=0.5) is False


def test_act_probability_is_believed_only_when_the_head_was_trained():
    opinion = {"type": "noul", "noul": 0.5, "action": {"act_probability": 0.99}}
    # Stock checkpoints leave act_head with zero gradient; believing it would make
    # every answer look half-unsure and defer everything.
    close(decision_uncertainty(opinion, act_trained=False), 0.5)
    # A head this project trained is the priced act/defer call and is taken at its word.
    close(decision_uncertainty(opinion, act_trained=True), 0.01)


def test_wrapper_objects_are_inverted_onto_the_same_scale():
    # A 0.9 confidence wrapper must read as 0.1 uncertainty, or one threshold
    # would mean opposite things on the two call paths.
    close(decision_uncertainty(SimpleNamespace(confidence=0.9)), 0.1)
    close(decision_uncertainty(SimpleNamespace(confidence=0.5)), 0.5)
    assert decision_uncertainty(SimpleNamespace()) is None
    assert decision_uncertainty(SimpleNamespace(confidence=2.0)) is None


def test_the_threshold_is_clamped_and_compared_on_the_uncertainty_scale():
    assert decision_usable({"type": "noul", "noul": 0.9}, max_uncertainty=0.25) is True
    assert decision_usable({"type": "noul", "noul": 0.5}, max_uncertainty=0.25) is False
    # 0.25 on the uncertainty scale is "p outside (0.25, 0.75)", as advertised.
    assert decision_usable({"type": "noul", "noul": 0.76}, max_uncertainty=0.25) is True
    assert decision_usable({"type": "noul", "noul": 0.74}, max_uncertainty=0.25) is False
    # A ceiling above the scale's range is clamped rather than silently widening
    # it. At the top of the range the gate accepts everything -- including the
    # maximally hesitant p = 0.5 -- which is what "accept anything" should mean.
    assert decision_usable({"type": "noul", "noul": 0.5}, max_uncertainty=99) is True
    # And a negative ceiling clamps shut rather than inverting the comparison.
    assert decision_usable({"type": "noul", "noul": 0.9}, max_uncertainty=-3) is False
