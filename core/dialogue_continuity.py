"""Continuous dialogue-continuity scoring for the last delivered bot turn.

The previous rule was four hard-coded binaries: a fixed 60 second window, a
boolean answer shape, an all-or-nothing "any intervening message" veto, and the
intended-participant identity check. This module keeps the two structural gates
and replaces the rest with named, tunable weights so the learning layer has
dials to fit instead of constants to guess.

Calibration (checked by tests/test_dialogue_continuity.py): with the default
weights below the previous decisions are reproduced exactly. The old rule
accepted iff the answer shape held, nothing intervened, and 0 < dt <= 60s; the
default score accepts under the same conditions up to dt <= 60.27s, so the only
difference is a 0.27 second sliver. Nothing here changes behaviour on its own.

Deferred dial: "topic 是否保持" is not scored yet because accepting a dialogue
answer forces the dialogue topic onto the node, so a node-level topic comparison
is not an independent signal at this point in the pipeline. It becomes one once
topic assignment precedes recipient resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp
from typing import Any, Sequence

from .evidence import EvidenceEntry
from .message_features import analyze_text

@dataclass(frozen=True)
class DialogueWeights:
    """Every tunable constant of the continuity decision, in one place."""

    time: float = 0.60
    answer: float = 0.40
    time_midpoint: float = 60.0
    time_slope: float = 4.0
    turn_weight: float = 2.0
    competitor_weight: float = 1.0
    # How much a bare reaction stops counting as an answer. The old rule counted
    # "好的" or "？？？" as answering the question; 0.0 keeps that, 1.0 refuses it.
    reaction_penalty: float = 0.0
    threshold: float = 0.69


DEFAULT_WEIGHTS = DialogueWeights()


@dataclass(frozen=True)
class DialogueContinuity:
    """Raw components next to the weighted result; the learner fits the former."""

    question_pending: bool
    intended_speaker: bool
    time_delta: float
    time_decay: float
    answer_shape: float
    answer_credit: float
    # A bare reaction ("好的", "嗯", "？？？") is answer-like under the old
    # predicate. It is scored through reaction_penalty, which defaults to zero.
    reaction_like: bool
    intervening_total: int
    intervening_competitors: int
    turn_factor: float
    competitor_factor: float
    score: float
    threshold: float
    accepted: bool
    weights: DialogueWeights = field(default=DEFAULT_WEIGHTS, repr=False)

    def entries(self) -> tuple[EvidenceEntry, ...]:
        """Ledger rows carrying each raw value beside its actual contribution."""
        weights = self.weights
        return (
            EvidenceEntry("recipient", "dialogue_time_decay", "dialogue_continuity",
                          self.time_decay, weights.time * self.time_decay),
            EvidenceEntry("recipient", "dialogue_answer_shape", "dialogue_continuity",
                          self.answer_shape, weights.answer * self.answer_credit),
            EvidenceEntry("recipient", "dialogue_turn_factor", "dialogue_continuity",
                          self.turn_factor, self.turn_factor),
            EvidenceEntry("recipient", "dialogue_competitor_factor", "dialogue_continuity",
                          self.competitor_factor, self.competitor_factor),
            EvidenceEntry("recipient", "dialogue_continuity_score", "dialogue_continuity",
                          self.score, self.score),
        )


def time_decay(seconds: float, weights: DialogueWeights = DEFAULT_WEIGHTS) -> float:
    """Smooth replacement for the fixed window: ~1 well inside it, 0.5 at 60s.

    Pure in its argument; the "the answer cannot precede the question" gate is
    the caller's, not this curve's.
    """
    return 1.0 / (1.0 + exp((seconds - weights.time_midpoint) / max(0.05, weights.time_slope)))


def answer_shape(text: str) -> float:
    """The shape the old rule required, as a number rather than a branch.

    Deliberately boolean: every elliptical, acknowledgement and filler form is
    already answer-like under the old predicate, so a graded short-form score
    would be an unreachable branch rather than a dial.
    """
    return 1.0 if analyze_text(text).is_answer_like else 0.0


def evaluate(dialogue: Any, node: Any, recent_nodes: Sequence[Any] = (),
             weights: DialogueWeights | None = None) -> DialogueContinuity:
    """Score one candidate answer against the pending bot turn.

    The two structural gates are not learned: a dialogue only exists while the
    bot's last turn asked a question, and only the intended participant's first
    uninterrupted response qualifies. Everything else is weighted.
    """
    weights = weights or DEFAULT_WEIGHTS
    question_pending = bool(getattr(dialogue, "last_bot_was_question", False))
    intended = str(getattr(node, "user_id", "")) == str(getattr(dialogue, "user_id", ""))
    updated_at = float(getattr(dialogue, "updated_at", 0.0) or 0.0)
    stamp = float(getattr(node, "timestamp", 0.0) or 0.0)
    delta = stamp - updated_at
    decay = time_decay(delta, weights) if delta > 0 else 0.0
    features = analyze_text(getattr(node, "text", ""))
    reaction_like = bool(features.is_filler or features.is_ack)
    shape = 1.0 if features.is_answer_like else 0.0
    credit = shape * (1.0 - weights.reaction_penalty * float(reaction_like))

    msg_id = getattr(node, "msg_id", None)
    total = competitors = 0
    if delta > 0:
        for other in recent_nodes:
            if getattr(other, "msg_id", None) == msg_id:
                continue
            other_stamp = float(getattr(other, "timestamp", 0.0) or 0.0)
            if not updated_at < other_stamp <= stamp:
                continue
            total += 1
            if str(getattr(other, "user_id", "")) not in {str(getattr(node, "user_id", "")),
                                                          str(getattr(dialogue, "user_id", ""))}:
                competitors += 1
    turn_factor = 1.0 / (1.0 + weights.turn_weight * total)
    competitor_factor = 1.0 / (1.0 + weights.competitor_weight * competitors)
    base = weights.time * decay + weights.answer * credit
    score = base * turn_factor * competitor_factor
    accepted = bool(question_pending and intended and delta > 0
                    and score >= weights.threshold)
    return DialogueContinuity(question_pending, intended, delta, decay, shape, credit,
                              reaction_like, total, competitors, turn_factor,
                              competitor_factor, score, weights.threshold, accepted, weights)
