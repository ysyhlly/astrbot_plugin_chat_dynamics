"""The persona axes reach the record as numbers, and only as numbers.

Collecting the projection changes no behaviour -- the decision prompt, the
participation rubric and the gate are all untouched. What changes is that a record
now says *which* persona was judging, in a form a learner can condition on.

That matters because the decision prompt appends the persona verbatim, so two
personas over identical evidence are supposed to disagree. A record that forgets
which persona was loaded turns that disagreement into label noise, and the only
thing left to learn is the average across personas.

These tests hold the projection to its side of the boundary: whatever the card
says stays out, and what crosses is six integers.
"""
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.persona_axes import AXIS_NAMES
from astrbot_plugin_chat_dynamics.core.persona_trace import stage_trace
from astrbot_plugin_chat_dynamics.core.turn_decision import (
    PersonaSnapshot, TurnContext, TurnDecision,
)

MARKER = "zzsecretxx"


def _run(**kwargs) -> dict:
    turn = TurnContext("group", "user", f"{MARKER} conversation", (), (), 1, 1, 0, True)
    decision = TurnDecision("reply", "focused", (), "goal", "normal", "request")
    gate = SimpleNamespace(should_speak=True, reason_code="ok",
                           rhythm=SimpleNamespace(state="awake", action=""))
    return stage_trace({}, persona=PersonaSnapshot("fp", "conv", "quiet", f"{MARKER} persona"),
                       interaction_state="observing", presence="sensible",
                       turn=turn, proposed=decision, final=decision, gate=gate, **kwargs)


def test_axes_cross_as_six_integers():
    axes = {name: 3 for name in AXIS_NAMES}
    record = _run(axes=axes)
    stored = record["review_context"]["persona_axes"]
    assert set(stored) == set(AXIS_NAMES)
    assert all(isinstance(v, int) and 0 <= v <= 4 for v in stored.values())


def test_no_axes_means_no_field_at_all():
    # Presence-gated like everything else in the trace: an absent projection is
    # absent, not zeroed. A persona that was never read is not a flat persona.
    record = _run()
    assert "persona_axes" not in record["review_context"]


def test_out_of_range_and_unknown_keys_never_cross():
    poisoned = {name: 3 for name in AXIS_NAMES}
    poisoned["note"] = MARKER
    poisoned["chattiness"] = 99
    record = _run(axes=poisoned)
    stored = record["review_context"]["persona_axes"]
    assert set(stored) == set(AXIS_NAMES)
    assert 0 <= stored["chattiness"] <= 4
    assert MARKER not in str(stored)


def test_projection_does_not_smuggle_text_into_the_trace():
    record = _run(axes={name: 2 for name in AXIS_NAMES})
    import json
    assert MARKER not in json.dumps(record, ensure_ascii=False)
    # The persona itself is still only a fingerprint -- collecting the projection
    # widened what is known about the persona, never what is quoted from it.
    assert record["review_context"]["persona_fingerprint"] == "fp"
