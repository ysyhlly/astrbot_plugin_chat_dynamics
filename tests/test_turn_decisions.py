"""The small-decisions layer: one batch, many readers, never a blocker.

Pinned here are the three properties a decision point relies on: the batch is
asked once for every reader, a reader that cannot be answered reads as "no
opinion" rather than as a negative one, and reading is synchronous so it is safe
under the session lock.
"""

import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityLevel, AddressivityScore
from astrbot_plugin_chat_dynamics.core.arbiter import InterventionArbiter
from astrbot_plugin_chat_dynamics.core.telemetrics import RoomTelemetrics
from astrbot_plugin_chat_dynamics.core.turn_decisions import (
    SCORE_LEVELS,
    SmallDecision,
    TurnDecisions,
    wts_questions,
)
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode


def _telemetrics():
    return RoomTelemetrics(
        mpm=10.0, token_density=20.0, emoji_ratio=0.1,
        punctuation_formality=0.6, sample_size=10, window_duration=60.0,
    )


def _weak_address():
    return AddressivityScore(score=0.2, level=AddressivityLevel.WEAK, is_bot_targeted=False)


def _decide(arbiter, decisions=None, floor=0.6, text="请问这个怎么配置"):
    return arbiter.evaluate(
        session_id="group_1",
        addressivity=_weak_address(),
        telemetrics=_telemetrics(),
        vibe_mode=GroupChatMode.FAST_BANTER,
        user_id="user_1",
        text=text,
        current_time=1050.0,
        decisions=decisions,
        decision_floor=floor,
    )


def opinions(**confidences):
    """A batch that reads every rubric as decisive, at the given confidences."""
    decisions = TurnDecisions.for_wts()
    decisions.ingest({
        slot: {"type": "score", "score": float(len(SCORE_LEVELS) - 1),
               "confidence": confidences.get(slot, 0.9), "probabilities": {}}
        for slot in decisions.slots()
    })
    return decisions


def test_a_confident_reading_replaces_the_regex_heuristic_it_stands_in_for():
    arbiter = InterventionArbiter(base_threshold=0.60, deep_cooling_duration=900.0,
                                  asymmetry_streak_limit=2)
    baseline = _decide(arbiter)
    decided = _decide(arbiter, opinions())
    # Decisive on the rubric normalizes to 1.0, so the three text-shaped sub-scores
    # land there. The reported fields are what the arithmetic used, not what it
    # might have used — the panel and the learning layer read them.
    assert decided.topic_relevance == pytest.approx(1.0)
    assert decided.professionalism == pytest.approx(1.0)
    assert decided.question_value == pytest.approx(1.0)
    assert baseline.topic_relevance < 1.0
    assert decided.willingness_score > baseline.willingness_score


def test_no_opinion_leaves_the_heuristic_exactly_where_it_was():
    """The default is the old behaviour: `decisions=None` must change nothing."""
    arbiter = InterventionArbiter(base_threshold=0.60, deep_cooling_duration=900.0,
                                  asymmetry_streak_limit=2)
    plain = _decide(arbiter)
    explicit_none = _decide(arbiter, None)
    assert (plain.willingness_score, plain.should_speak, plain.topic_relevance) == (
        explicit_none.willingness_score, explicit_none.should_speak,
        explicit_none.topic_relevance)


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.59])
def test_an_unsure_reading_defers_to_the_heuristic_rather_than_overriding_it(confidence):
    """The floor is applied where the weighting is, so "unsure" never becomes "1.0"."""
    arbiter = InterventionArbiter(base_threshold=0.60, deep_cooling_duration=900.0,
                                  asymmetry_streak_limit=2)
    baseline = _decide(arbiter)
    decided = _decide(arbiter, opinions(topic_relevance=confidence), floor=0.6)
    assert decided.topic_relevance == pytest.approx(baseline.topic_relevance)


def test_only_the_confident_slots_are_taken():
    """A slot is judged on its own: one strong reading does not carry the others."""
    arbiter = InterventionArbiter(base_threshold=0.60, deep_cooling_duration=900.0,
                                  asymmetry_streak_limit=2)
    baseline = _decide(arbiter)
    decided = _decide(arbiter, opinions(topic_relevance=0.9, question_value=0.2))
    assert decided.topic_relevance == pytest.approx(1.0)
    assert decided.question_value == pytest.approx(baseline.question_value)


def test_the_floor_is_the_callers_to_choose():
    arbiter = InterventionArbiter(base_threshold=0.60, deep_cooling_duration=900.0,
                                  asymmetry_streak_limit=2)
    reading = opinions(topic_relevance=0.5)
    assert _decide(arbiter, reading, floor=0.6).topic_relevance < 1.0
    assert _decide(arbiter, reading, floor=0.4).topic_relevance == pytest.approx(1.0)


def test_a_model_opinion_never_overrides_a_hard_rule():
    """Deep cooling is not model-shaped; a decisive reading must not clear it."""
    arbiter = InterventionArbiter(base_threshold=0.60, deep_cooling_duration=900.0,
                                  asymmetry_streak_limit=2)
    arbiter.trigger_cooling("group_1", duration_seconds=600.0, current_time=1000.0)
    decided = _decide(arbiter, opinions(), text="随便聊聊今天天气怎么样")
    assert decided.should_speak is False


def score_answer(value=3.0, confidence=0.8):
    return {"type": "score", "score": value, "confidence": confidence,
            "probabilities": {str(i): 0.0 for i in range(len(SCORE_LEVELS))}}


def test_one_batch_carries_every_decision_the_turn_will_ask_for():
    decisions = TurnDecisions.for_wts()
    questions = wts_questions()
    assert decisions.slots() == ("topic_relevance", "question_value", "professionalism")
    # Every reader is an ordinal opinion over the same five levels, so the batch
    # costs one request however many decision points read it.
    for spec in questions.values():
        assert spec["type"] == "score"
        assert spec["criteria"] == list(SCORE_LEVELS)


def test_an_opinion_normalizes_to_the_scale_the_wts_arithmetic_mixes_on():
    """A rubric reading has to land on 0..1 to be weighted with the other terms."""
    decisive = SmallDecision("score", 0.9, expected=float(len(SCORE_LEVELS) - 1))
    assert decisive.normalized() == pytest.approx(1.0)
    none = SmallDecision("score", 0.9, expected=0.0)
    assert none.normalized() == pytest.approx(0.0)
    mid = SmallDecision("score", 0.9, expected=2.0)
    assert 0.0 < mid.normalized() < 1.0

    assert SmallDecision("noul", 0.9, p_true=0.25).normalized() == pytest.approx(0.25)
    assert SmallDecision("choice", 0.9, pick="b", probabilities={"a": 0.2, "b": 0.8}).normalized() == pytest.approx(0.8)


def test_ingest_answers_every_reader_in_one_pass():
    decisions = TurnDecisions.for_wts()
    decisions.ingest({slot: score_answer(value=float(index))
                      for index, slot in enumerate(decisions.slots())})
    assert decisions.filled is True
    assert decisions.peek("topic_relevance").expected == 0.0
    assert decisions.peek("question_value").expected == 1.0
    assert decisions.peek("professionalism").expected == 2.0
    assert decisions.refused == {}


@pytest.mark.parametrize("answer", [
    {"type": "score", "score": 2.0},                    # no confidence at all
    {"type": "score", "score": 2.0, "confidence": None},
    {"type": "score", "score": 2.0, "confidence": True},
    {"type": "score", "score": "two", "confidence": 0.8},
    {"type": "noul", "noul": 0.5},                      # wrong kind for this slot
    {"type": "choice", "choice": "x", "confidence": 0.8},
    {"type": "score", "score": 2.0, "confidence": 1.5},  # confidence out of range
    None,
])
def test_an_opinion_that_cannot_be_used_reads_as_no_opinion(answer):
    """None means the caller keeps its own heuristic. It is never a zero score."""
    decisions = TurnDecisions.for_wts()
    decisions.ingest({"topic_relevance": answer, "question_value": score_answer(),
                      "professionalism": score_answer()})
    assert decisions.peek("topic_relevance") is None
    assert decisions.peek("question_value") is not None


def test_an_unconfident_opinion_is_still_an_opinion():
    """The acceptance floor belongs to the decision point, not to this layer.

    Refusing here would pre-empt a floor the caller has not applied yet and turn
    "the model was unsure" into "the model said nothing" — two different facts for
    anything that later looks at why a decision went the way it did.
    """
    decisions = TurnDecisions.for_wts()
    decisions.ingest({"topic_relevance": score_answer(confidence=0.1)})
    opinion = decisions.peek("topic_relevance")
    assert opinion is not None and opinion.confidence == pytest.approx(0.1)
    assert "topic_relevance" not in decisions.refused


def test_the_two_refusal_reasons_separate_absent_from_malformed():
    decisions = TurnDecisions.for_wts()
    decisions.ingest({"topic_relevance": {"type": "score", "score": "two", "confidence": 0.8}})
    assert decisions.refused == {
        "topic_relevance": "unusable_answer",   # answered, but not a decision
        "question_value": "no_answer",          # never answered
        "professionalism": "no_answer",
    }


def test_a_missing_reply_is_recorded_as_never_answered():
    decisions = TurnDecisions.for_wts()
    decisions.ingest(None)
    assert decisions.filled is True
    assert all(decisions.peek(slot) is None for slot in decisions.slots())
    assert set(decisions.refused.values()) == {"no_answer"}


def test_reading_is_synchronous_lookup_only():
    """The reader runs under the session lock; there must be nothing to wait on."""
    decisions = TurnDecisions.for_wts()
    decisions.ingest({"topic_relevance": score_answer()})
    import inspect

    assert not inspect.iscoroutinefunction(TurnDecisions.peek)
    assert not inspect.iscoroutinefunction(TurnDecisions.ingest)


def test_the_snapshot_keeps_refusals_apart_from_disagreements():
    """"The model disagreed" and "the model was never asked" are different facts."""
    decisions = TurnDecisions.for_wts()
    decisions.ingest({"topic_relevance": score_answer(confidence=0.4)})
    payload = decisions.snapshot()
    assert payload["decisions"]["topic_relevance"]["confidence"] == 0.4
    assert payload["refused"] == {
        "question_value": "no_answer", "professionalism": "no_answer"}
    assert payload["source"] == "laya"
