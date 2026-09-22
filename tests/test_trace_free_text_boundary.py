"""The trace boundary: bounded facts cross it, free text never does.

A turn decision now carries prose -- `rationale`, and `why_rejected` on each
alternative -- because a decision nobody can follow is not auditable. But the
serialized `decision_trace` is a record of *what* was decided, and the learning
store is documented as samples "without message text". Letting reasoning prose
into it would quietly turn a bounded-facts record into a transcript.

These tests plant distinctive words in every free-text field and require that
none of them reach the serialized trace, while the enumerable record beside them
survives intact. That is the whole contract: more detail is welcome, prose is not.

Message ids are the one list that crosses -- "who is this reply for" is one of the
few things the judge decides, and an id is not prose. But an id that was not part
of the turn is dropped rather than stored: `TurnDecision.parse` checks this and
the dataclass constructor does not, so the writer has to check again.
"""
import json
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.persona_trace import stage_trace
from astrbot_plugin_chat_dynamics.core.turn_decision import (
    PersonaSnapshot, TurnDecision,
)

MARKER = "zzsecretxx"


def _turn():
    # A stub rather than a real TurnContext: `stage_trace` reads only `truncated`
    # and `allowed_ids` from it, and naming the allowed ids here is what makes the
    # id filtering observable.
    return SimpleNamespace(truncated=False, allowed_ids=frozenset({"m1", "m2"}))


def _proposed() -> TurnDecision:
    return TurnDecision(
        "reply", "focused", ("m1", "m2", MARKER), f"{MARKER} goal", "normal", "request",
        "direct_address",
        f"{MARKER} rationale",
        (("action", 0.8), ("state", 0.6), ("length", 0.7)),
        (("m1", ("mention", "bogus_cue"), 0.9),),
        (("ignore", f"{MARKER} why"),),
        (("addressee", "bot"), ("topic", "999719346"),
         ("completeness", "complete"), ("ambiguity", "low")),
    )


def _trace(proposed=None) -> dict:
    proposed = proposed or _proposed()
    gate = SimpleNamespace(should_speak=True, reason_code="ok",
                           rhythm=SimpleNamespace(state="awake", action=""))
    return stage_trace({"participation": {"level": "strong", "score": 1}},
                       persona=PersonaSnapshot("fp", "conv", "quiet", f"{MARKER} persona"),
                       interaction_state="observing", presence="sensible",
                       turn=_turn(), proposed=proposed, final=proposed, gate=gate)


def test_no_free_text_reaches_the_trace():
    trace = _trace()
    persona = trace["decision_stages"]["persona"]
    for field in ("rationale", "response_goal", "why_rejected"):
        assert field not in persona
    assert MARKER not in json.dumps(trace, ensure_ascii=False)


def test_the_enumerable_record_survives():
    persona = _trace()["decision_stages"]["persona"]
    assert persona["reason_category"] == "direct_address"
    assert persona["confidence"] == [{"field": "action", "value": 0.8},
                                    {"field": "state", "value": 0.6},
                                    {"field": "length", "value": 0.7}]
    assert persona["assessment"] == {"addressee": "bot", "topic": "999719346",
                                    "completeness": "complete", "ambiguity": "low"}
    # Which options were set aside is a fact and is kept; the prose is not.
    assert persona["alternatives"] == [{"action": "ignore"}]
    # Unknown cue tags are dropped rather than passed through -- a tag is a label
    # from a closed set, and free text must not ride in on one.
    assert persona["evidence"] == [{"message_id": "m1", "cues": ["mention"], "weight": 0.9}]


def test_target_ids_cross_but_only_the_ones_from_this_turn():
    persona = _trace()["decision_stages"]["persona"]
    # All candidates cross, negatives included -- dropping the ones that were not
    # aimed at would throw away exactly the hard examples worth learning from.
    assert persona["target_message_ids"] == ["m1", "m2"]
    # But an id that was never part of the turn is dropped at the boundary too,
    # so a stray string cannot ride in on this field.
    assert MARKER not in json.dumps(persona, ensure_ascii=False)


def test_prose_in_a_bounded_field_degrades_instead_of_leaking():
    proposed = TurnDecision(
        _proposed().action, _proposed().state, _proposed().target_message_ids,
        _proposed().response_goal, _proposed().length, _proposed().reason_code,
        f"{MARKER} not a category", _proposed().rationale, _proposed().confidence,
        _proposed().evidence, _proposed().alternatives, _proposed().assessment,
    )
    persona = _trace(proposed)["decision_stages"]["persona"]
    assert persona["reason_category"] == ""
    assert MARKER not in json.dumps(_trace(proposed), ensure_ascii=False)
