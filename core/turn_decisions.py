"""Small decisions asked together, outside the lock, read individually inside it.

One turn's synchronous decision points each want a model opinion, and none of them
can await: `_finish_turn_locked` runs under the session lock and a network call
there would stall the whole session. The turn pipeline already splits the work
that way on purpose — `_prepare_turn_locked` builds, `_enrich_turn` awaits models
"outside it", `_finish_turn_locked` decides — so this module fills the gap the
middle step leaves: hold every question the last step will ask, ask them in one
call while the lock is free, and hand each decision point only its own answer.

Asking them together is the point rather than a convenience. Laya answers typed
questions in a single forward pass and batches the rows, so one `/predict` covers
a turn's whole set for the cost of one request; asking per decision point would
multiply round trips to save nothing.

Three properties keep a model opinion from becoming a silent override:

* An opinion is only ever an **input** to a decision point's own weighting. The
  hard rules in front of the arithmetic — deep cooling, energy asymmetry, private
  topics — are never model-shaped and stay where they are.
* A slot with no usable answer is read as `None`, meaning "no model opinion". The
  decision point keeps the heuristic it already had. A missing answer is never
  approximated into one.
* Everything here is pure computation and pure lookup. The only `await` in the
  picture is the caller's, and it is bounded by the caller's own enrichment budget.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# The rubric length for the ordinal questions. Five levels keeps each step wide
# enough that a one-level disagreement between runs is noise rather than a
# different reading, while still resolving the ordering the WTS arithmetic needs.
SCORE_LEVELS = ("none", "slight", "moderate", "strong", "decisive")

SLOT_WTS = "wts"


@dataclass(frozen=True)
class SmallDecision:
    """One model opinion over one small decision.

    Normalized across the three typed answer shapes so a decision point reads the
    same fields whichever kind of question it asked. `confidence` is what an
    acceptance floor compares against; without one the decision is unusable and the
    caller keeps its own rules.
    """

    kind: str
    confidence: float
    pick: str = ""
    probabilities: Mapping[str, float] = field(default_factory=dict)
    expected: float = 0.0
    p_true: float = 0.0
    source: str = "laya"

    def normalized(self) -> float:
        """The opinion on a 0..1 scale, whatever the question kind was.

        A `score` runs on its rubric's own scale, so it is divided back down. That
        is what lets one WTS expression mix an ordinal reading with the 0..1
        probabilities of the other kinds without each call site re-deriving it.
        """
        if self.kind == "choice":
            return float(self.probabilities.get(self.pick, 0.0))
        if self.kind == "score":
            top = len(SCORE_LEVELS) - 1
            return max(0.0, min(1.0, self.expected / top)) if top else 0.0
        return self.p_true


def _decision_of(answer: Any, kind: str, levels: int = len(SCORE_LEVELS)) -> Optional[SmallDecision]:
    """One validated answer into a `SmallDecision`, or None when unusable.

    The transport has already checked the published shapes; what is checked here is
    that the answer can carry a decision at all. An unknown `choice` label or a
    missing confidence is not a decision this plugin can act on.
    """
    if not isinstance(answer, Mapping) or answer.get("type") != kind:
        return None
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    score = float(confidence)
    if score != score or not 0.0 <= score <= 1.0:
        return None
    if kind == "choice":
        pick = answer.get("choice")
        if not isinstance(pick, str) or not pick:
            return None
        probabilities = {
            str(key): float(value)
            for key, value in (answer.get("probabilities") or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        return SmallDecision("choice", score, pick=pick, probabilities=probabilities)
    if kind == "score":
        expected = answer.get("score")
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            return None
        return SmallDecision("score", score, expected=float(expected))
    probability = answer.get("noul")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        return None
    value = float(probability)
    if value != value or not 0.0 <= value <= 1.0:
        return None
    return SmallDecision("noul", score, p_true=value)


def wts_questions() -> dict[str, dict]:
    """The opinions the willingness-to-speak arithmetic consumes.

    Only the sub-scores that are **about the text** are asked for. Participation
    and fatigue come from the plugin's own runtime state and stay rule-derived: a
    model cannot see how often the bot has spoken, and pretending it can would
    launder that guess into a measurement.
    """
    def score(instructions: str) -> dict:
        return {"type": "score", "instructions": instructions, "criteria": list(SCORE_LEVELS)}

    return {
        "topic_relevance": score(
            "How much does this message continue or advance the topic the assistant "
            "is already part of?"
        ),
        "question_value": score(
            "How much does this message contain a question or request that is worth "
            "answering rather than letting pass?"
        ),
        "professionalism": score(
            "How much does answering this well call for a careful, substantive reply "
            "rather than light banter?"
        ),
    }


def gate_questions() -> dict[str, dict]:
    """How loudly the bot should lean into this particular situation.

    Only the two dials are asked for. The situation label itself is deliberately
    left to `OccasionClassifier`: its keyword marks and the per-kind profile are
    hand-tuned together (`conflict` reads "stay out" at 0.15, `banter` at 1.1), so
    swapping the label without the profile it was tuned against would make one
    reading contradict itself. The dials are what actually gate behaviour, and they
    can be judged on their own.
    """
    return {
        "silence_bias": {
            "type": "score",
            "instructions": "How much should the assistant prefer to stay quiet here?",
            "criteria": list(SCORE_LEVELS),
        },
        "force_scale": {
            "type": "score",
            "instructions": "How strongly should the assistant lean into this conversation?",
            "criteria": list(SCORE_LEVELS),
        },
    }


def completeness_question() -> dict[str, dict]:
    """Whether the message is finished. It is asked per message, not per turn.

    This one decides how long the debounce buffer waits for the rest of a sentence,
    so it sits on the plugin's quietest, most synchronous path: a wrong "finished"
    cuts a thought in half and answers a fragment. The regex detector stays the
    primary read; the model is asked to disagree with it, not to replace it.
    """
    return {
        "completeness": {
            "type": "noul",
            "instructions": "Has this message finished its thought, or is the sender about to continue?",
            "criteria": {
                "true": "the thought is complete and nothing further is implied",
                "false": "the sender is clearly about to add more",
            },
        }
    }


def turn_questions() -> dict[str, dict]:
    """Every small decision `_finish_turn_locked` raises, in one batch."""
    return {**wts_questions(), **gate_questions()}


class MessageOpinions:
    """Per-message opinions, warmed from the async path and read synchronously.

    The debounce buffer decides how long to wait for the rest of a sentence while it
    is holding its own state, so it cannot await. Each message's question is asked
    the moment the message arrives — the answer has to be ready inside the debounce
    window anyway — and read here by plain lookup. A miss reads as "no model
    opinion" and the regex detector stands alone.

    Keyed by the message text because that is the identity: the same words asked
    twice have the same answer, and a turn's worth of fragments is a handful of
    entries rather than one per session.
    """

    def __init__(self, *, max_entries: int = 256, ttl: float = 60.0, clock: Any = None) -> None:
        self.max_entries = max(8, int(max_entries))
        self.ttl = max(0.0, float(ttl))
        self._clock = clock or time.monotonic
        self._entries: "OrderedDict[str, tuple[float, Optional[SmallDecision]]]" = OrderedDict()

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha1((text or "").strip().encode("utf-8", "replace")).hexdigest()

    def warm(self, text: str, decision: Optional["SmallDecision"]) -> None:
        """Record what the model said about one message. Never blocks."""
        key = self.key(text)
        self._entries[key] = (self._clock(), decision)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def peek(self, text: str) -> Optional["SmallDecision"]:
        """The warmed opinion on one message, or None. Pure lookup."""
        key = self.key(text)
        entry = self._entries.get(key)
        if entry is None:
            return None
        stamp, decision = entry
        if self.ttl and (self._clock() - stamp) > self.ttl:
            self._entries.pop(key, None)
            return None
        return decision

    def clear(self) -> None:
        self._entries.clear()


@dataclass
class TurnDecisions:
    """One turn's small decisions, asked together and read one at a time.

    Built while the turn is prepared, filled while the session lock is free, and
    read from inside it. A slot that was never filled or whose answer was unusable
    reads as `None`, which is the same thing to a decision point as a model that
    had nothing to say.
    """

    questions: dict[str, dict] = field(default_factory=dict)
    source: str = "laya"
    answers: dict[str, SmallDecision] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    filled: bool = False

    @classmethod
    def for_wts(cls) -> "TurnDecisions":
        return cls(questions=wts_questions())

    @classmethod
    def for_turn(cls) -> "TurnDecisions":
        """The full batch one turn needs: the WTS terms and the gate's dials."""
        return cls(questions=turn_questions())

    def slots(self) -> tuple[str, ...]:
        return tuple(self.questions)

    def ingest(self, raw: Mapping[str, Any] | None) -> None:
        """Store what the model answered, and why the rest could not be used.

        `raw` is the transport's already-validated answer table. Anything that does
        not resolve to a decision is recorded as refused rather than silently
        dropped, so the panel can tell "the model disagreed with the heuristic" from
        "the model was never asked".
        """
        self.filled = True
        self.answers.clear()
        self.refused.clear()
        for slot, spec in self.questions.items():
            raw_answer = (raw or {}).get(slot)
            decision = _decision_of(raw_answer, str(spec.get("type") or ""))
            if decision is None:
                # Two different facts: the slot was answered but could not carry a
                # decision, versus it was never answered at all. A panel reading
                # "the model disagreed with the heuristic" needs to tell them apart.
                self.refused[slot] = "unusable_answer" if raw_answer is not None else "no_answer"
                continue
            self.answers[slot] = decision

    def peek(self, slot: str) -> Optional[SmallDecision]:
        """The model's opinion on one decision. Synchronous, and never blocks."""
        return self.answers.get(slot)

    def snapshot(self) -> dict[str, Any]:
        """Panel-safe evidence: what was asked, what came back, what was refused."""
        return {
            "source": self.source,
            "filled": self.filled,
            "decisions": {
                slot: {
                    "kind": decision.kind,
                    "confidence": round(decision.confidence, 4),
                    "pick": decision.pick,
                    "expected": round(decision.expected, 4),
                    "p_true": round(decision.p_true, 4),
                }
                for slot, decision in self.answers.items()
            },
            "refused": dict(self.refused),
        }
