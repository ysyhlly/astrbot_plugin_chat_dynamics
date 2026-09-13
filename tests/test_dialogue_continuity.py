"""The continuous score must reproduce the rule it replaced, then move on demand."""
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.dialogue_continuity import (
    DEFAULT_WEIGHTS,
    DialogueWeights,
    answer_shape,
    evaluate,
    time_decay,
)
from astrbot_plugin_chat_dynamics.core.message_features import analyze_text


def node(msg_id, user_id, text, timestamp):
    return NS(msg_id=msg_id, user_id=user_id, text=text, timestamp=timestamp)


def dialogue(user_id="alice", updated_at=2.0, question=True):
    return NS(user_id=user_id, updated_at=updated_at, last_bot_was_question=question,
              topic_id="t", last_bot_message_id="b")


def old_rule(dlg, candidate, recent):
    """The literal pre-P2-b rule, kept here as the equivalence reference."""
    if not dlg.last_bot_was_question or str(candidate.user_id) != dlg.user_id:
        return False
    if not 0 < candidate.timestamp - dlg.updated_at <= 60:
        return False
    if not analyze_text(candidate.text).is_answer_like:
        return False
    return not any(n.msg_id != candidate.msg_id and dlg.updated_at < n.timestamp <= candidate.timestamp
                   for n in recent)


DELTAS = [0.5, 1.0, 5.0, 30.0, 45.0, 55.0, 59.0, 60.0, 61.0, 75.0, 120.0, 300.0]
SHAPES = ["1.21.4", "好的", "对了，今晚有人打游戏吗", "那怎么办？"]
INTERRUPTIONS = [(), ("other",), ("alice",), ("other", "other"), ("alice", "other")]


@pytest.mark.parametrize("delta", DELTAS)
@pytest.mark.parametrize("text", SHAPES)
@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_defaults_reproduce_the_rule_they_replaced(delta, text, interruption):
    dlg = dialogue()
    candidate = node("a", "alice", text, 2.0 + delta)
    recent = [node(f"x{i}", user, "插话", 2.0 + 0.1 * (i + 1))
              for i, user in enumerate(interruption)]
    assert evaluate(dlg, candidate, recent).accepted == old_rule(dlg, candidate, recent)


@pytest.mark.parametrize("user,question", [("bystander", True), ("alice", False)])
def test_structural_gates_are_not_weighted(user, question):
    dlg = dialogue()
    candidate = node("a", user, "1.21.4", 3.0)
    if user == "bystander":
        assert not evaluate(dlg, candidate).accepted
    else:
        dlg = dialogue(question=question)
        assert not evaluate(dlg, candidate).accepted


def test_time_decay_is_continuous_and_centred_on_the_old_window():
    assert time_decay(60.0) == pytest.approx(0.5)
    samples = [time_decay(s) for s in range(1, 181, 2)]
    assert samples == sorted(samples, reverse=True)
    # The old rule was a cliff; the new one still separates 1s from 61s clearly.
    assert time_decay(1.0) > 0.99 and time_decay(61.0) < 0.5
    # No special case at zero: the "answer cannot precede the question" gate
    # belongs to evaluate(), not to the curve.
    assert time_decay(0.0) > 0.99


def test_answer_shape_is_the_old_gate_and_reactions_are_recorded():
    assert answer_shape("1.21.4") == 1.0
    # Every short form the old predicate accepts is already answer-like, so the
    # shape stays boolean; the reachable distinction is reaction versus content.
    assert answer_shape("好的") == 1.0
    assert answer_shape("？？？") == 1.0
    assert answer_shape("对了，今晚有人打游戏吗") == 0.0
    dlg = dialogue()
    assert evaluate(dlg, node("a", "alice", "好的", 3.0)).reaction_like is True
    assert evaluate(dlg, node("a", "alice", "？？？", 3.0)).reaction_like is True
    assert evaluate(dlg, node("a", "alice", "1.21.4", 3.0)).reaction_like is False
    # Unpenalised by default, exactly as before.
    assert evaluate(dlg, node("a", "alice", "好的", 3.0)).accepted


def test_dials_are_real_and_move_the_decision():
    dlg = dialogue()
    late = node("a", "alice", "1.21.4", 92.0)
    assert not evaluate(dlg, late).accepted
    # A learner widening the window can make the same message pass.
    wider = DialogueWeights(time_midpoint=120.0)
    assert evaluate(dlg, late, weights=wider).accepted
    # So can spending the short-form credit instead of gating on it.
    # Spending the reaction distinction stops a bare acknowledgement from
    # closing the bot's question, which the old rule could never express.
    harsher = DialogueWeights(reaction_penalty=1.0)
    assert evaluate(dlg, node("a", "alice", "好的", 3.0), weights=harsher).accepted is False
    assert evaluate(dlg, node("a", "alice", "1.21.4", 3.0), weights=harsher).accepted is True
    # Interruption stops being all-or-nothing: enough of them still refuse.
    crowded = [node(f"x{i}", "other", "插话", 2.2 + 0.1 * i) for i in range(3)]
    assert not evaluate(dlg, node("a", "alice", "1.21.4", 3.0), crowded, weights=harsher).accepted


def test_components_are_recorded_with_raw_value_and_contribution():
    dlg = dialogue()
    crowded = [node("x", "other", "插话", 2.5)]
    result = evaluate(dlg, node("a", "alice", "1.21.4", 4.0), crowded)
    entries = {entry.code: entry for entry in result.entries()}
    assert set(entries) == {"dialogue_time_decay", "dialogue_answer_shape",
                            "dialogue_turn_factor", "dialogue_competitor_factor",
                            "dialogue_continuity_score"}
    assert entries["dialogue_turn_factor"].raw_value == pytest.approx(1 / 3)
    assert entries["dialogue_competitor_factor"].raw_value == pytest.approx(0.5)
    assert result.intervening_total == 1 and result.intervening_competitors == 1
    assert result.score == pytest.approx(result.entries()[-1].raw_value)
    assert result.weights is DEFAULT_WEIGHTS
    # A weighted term reports its share of the score including the factors it
    # survived; a multiplicative factor reports how far it moved the score.
    assert entries["dialogue_answer_shape"].contribution == pytest.approx(0.4 / 6)
    assert entries["dialogue_turn_factor"].contribution == pytest.approx(1 / 3, abs=1e-3)
    assert entries["dialogue_competitor_factor"].contribution == pytest.approx(1 / 6, abs=1e-3)


def test_neutral_factors_report_no_movement():
    result = evaluate(dialogue(), node("a", "alice", "1.21.4", 3.0))
    entries = {entry.code: entry for entry in result.entries()}
    assert entries["dialogue_turn_factor"].contribution == pytest.approx(0.0)
    assert entries["dialogue_competitor_factor"].contribution == pytest.approx(0.0)
    assert entries["dialogue_answer_shape"].contribution == pytest.approx(0.4, abs=1e-3)


def test_contributions_do_not_claim_an_additive_decomposition():
    result = evaluate(dialogue(), node("a", "alice", "1.21.4", 4.0),
                      [node("x", "other", "插话", 2.5)])
    parts = [entry.contribution for entry in result.entries()
             if entry.code != "dialogue_continuity_score"]
    # The score is a product of factors, so the rows must not sum to it; if they
    # ever did, one of them would be a share of a sum that does not exist.
    assert abs(sum(parts) - result.score) > 0.1
